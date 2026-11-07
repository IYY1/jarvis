"""A small clip editor on FFmpeg: trim, split, remove sections, join clips, pick audio tracks.

The original files are never modified. Edits are a list of kept segments
(source, start, end); export writes a new file beside the first clip.

Two export modes, because a lossless cut can only start on a keyframe:
- Fast keeps the original video stream (no quality loss, seconds to export).
  Each segment starts at the keyframe at or before the chosen start, so a
  segment may begin up to one keyframe interval early (gpu-screen-recorder
  places one every 2 s). Joined clips must share codec, size, pixel format
  and audio layout.
- Exact cuts on the chosen frame and converts differing clips, by re-encoding
  the video (on the GPU when it can) and the audio.
Audio tracks can be kept separately or mixed into one track; mixing re-encodes
audio only. Already-mixed audio cannot be separated: removing one app's sound
needs the separate-track replay setting before recording.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from typing import Callable

MAX_SEGMENTS = 200


class EditError(ValueError):
    pass


@dataclass
class Source:
    path: str
    duration: float
    video: dict
    audio: list[dict]
    keyframes: list[float]

    def compatible(self, other: "Source") -> list[str]:
        """Why the two can't be joined without conversion (empty: they can)."""
        problems = []
        for key in ("codec_name", "width", "height", "pix_fmt"):
            if self.video.get(key) != other.video.get(key):
                problems.append(f"video {key.replace('_name', '')} differs")
        if [(a["codec_name"], a.get("channels"), a.get("sample_rate")) for a in self.audio] != \
                [(a["codec_name"], a.get("channels"), a.get("sample_rate")) for a in other.audio]:
            problems.append("audio tracks differ")
        return problems


@dataclass
class Segment:
    source: int
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


def _executable(argv: list[str]) -> list[str]:
    from .host import is_windows, tool
    if is_windows():
        resolved = tool(argv[0])
        if not resolved:
            raise EditError(f"{argv[0]} is missing. Reinstall the complete Jarvis app.")
        return [resolved, *argv[1:]]
    return argv


def _run(argv: list[str], timeout: float = 60) -> str:
    from .host import child_options
    result = subprocess.run(_executable(argv), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **child_options())
    if result.returncode:
        raise EditError((result.stderr.strip().splitlines() or ["FFmpeg failed"])[-1][:300])
    return result.stdout


def track_names(path: str | Path, count: int) -> list[str]:
    """Names Jarvis stored when it saved the replay, else "Audio 1", "Audio 2"…"""
    path = Path(path)
    try:
        names = json.loads((path.parent / f".{path.name}.tracks.json").read_text(encoding="utf-8"))["tracks"]
        if len(names) == count and all(isinstance(n, str) for n in names):
            return names
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return [f"Audio {i + 1}" for i in range(count)]


SILENT_DB = -90.0   # volumedetect reports digital silence as -91 dB


def loudness(path: str | Path, count: int, timeout: float = 120) -> list[float | None]:
    """The loudest moment of each audio track in dB, from one pass that
    decodes only the sound (a few seconds for a long clip). None: unknown."""
    if count <= 0:
        return []
    graph = ";".join(f"[0:a:{i}]volumedetect[o{i}]" for i in range(count))
    maps = [arg for i in range(count) for arg in ("-map", f"[o{i}]")]
    from .host import child_options
    result = subprocess.run(_executable(["ffmpeg", "-nostats", "-hide_banner", "-vn", "-i", str(path), "-filter_complex", graph,
                             *maps, "-f", "null", "-"]), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **child_options())
    peaks: dict[int, float] = {}
    for line in result.stderr.splitlines():
        found = re.search(r"Parsed_volumedetect_(\d+) .*max_volume: (-?[\d.]+|-inf) dB", line)
        if found:
            peaks[int(found.group(1))] = float("-inf") if found.group(2) == "-inf" else float(found.group(2))
    return [peaks.get(i) for i in range(count)]


def probe(path: str | Path) -> Source:
    path = str(Path(path).resolve())
    data = json.loads(_run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=index,codec_type,codec_name,width,height,pix_fmt,"
                            "r_frame_rate,channels,sample_rate,color_transfer,color_primaries,color_space,color_range",
                            "-of", "json", path]))
    duration = float(data.get("format", {}).get("duration") or 0)
    video = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not video or duration <= 0:
        raise EditError(f"{Path(path).name} has no video to edit")
    for name, stream in zip(track_names(path, len(audio)), audio):
        stream["name"] = name
    packets = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
                    "-of", "csv=p=0", path], timeout=300)
    keyframes = sorted(float(t) for t, _, flags in (line.partition(",") for line in packets.splitlines())
                       if "K" in flags and t not in ("", "N/A"))
    return Source(path, duration, video[0], audio, keyframes or [0.0])


@dataclass
class Edit:
    sources: list[Source]
    segments: list[Segment]
    history: list[list[Segment]] = field(default_factory=list)

    @classmethod
    def of(cls, source: Source) -> "Edit":
        return cls([source], [Segment(0, 0.0, source.duration)])

    @property
    def duration(self) -> float:
        return sum(s.length for s in self.segments)

    def _save(self) -> None:
        self.history.append([Segment(s.source, s.start, s.end) for s in self.segments])
        del self.history[:-50]

    def undo(self) -> bool:
        if not self.history:
            return False
        self.segments = self.history.pop()
        return True

    def locate(self, t: float) -> tuple[int, float]:
        """Segment index and source time for a timeline time."""
        for i, segment in enumerate(self.segments):
            if t < segment.length or i == len(self.segments) - 1:
                return i, segment.start + min(max(t, 0.0), segment.length)
            t -= segment.length
        raise EditError("Nothing left to edit")

    def boundaries(self) -> list[float]:
        """Timeline times where segments meet (for drawing the timeline)."""
        out, t = [], 0.0
        for segment in self.segments[:-1]:
            t += segment.length
            out.append(t)
        return out

    def split(self, t: float) -> None:
        index, at = self.locate(t)
        segment = self.segments[index]
        if not segment.start + 0.05 < at < segment.end - 0.05:
            return
        if len(self.segments) >= MAX_SEGMENTS:
            raise EditError(f"Keep at most {MAX_SEGMENTS} segments")
        self._save()
        self.segments[index:index + 1] = [Segment(segment.source, segment.start, at),
                                          Segment(segment.source, at, segment.end)]

    def remove_range(self, start: float, end: float) -> None:
        """Remove timeline [start, end): cut out a section, or trim an end."""
        start, end = sorted((max(0.0, start), min(self.duration, end)))
        if end - start < 0.05:
            raise EditError("Mark a longer section first")
        if end - start >= self.duration - 0.05:
            raise EditError("That would remove the whole clip")
        self._save()
        kept, t = [], 0.0
        for s in self.segments:
            a, b = t, t + s.length
            t = b
            if b <= start or a >= end:
                kept.append(s)
                continue
            if a < start:
                kept.append(Segment(s.source, s.start, s.start + (start - a)))
            if b > end:
                kept.append(Segment(s.source, s.start + (end - a), s.end))
        self.segments = [s for s in kept if s.length > 0.01]

    def trim(self, start: float, end: float) -> None:
        """Keep only timeline [start, end]."""
        if start > 0.01:
            self.remove_range(0.0, start)
            end -= start
        if end < self.duration - 0.01:
            self.remove_range(end, self.duration)

    def remove_segment(self, index: int) -> None:
        if len(self.segments) < 2:
            raise EditError("That would remove the whole clip")
        self._save()
        del self.segments[index]

    def append(self, source: Source) -> None:
        """Join another clip onto the end."""
        if len(self.sources) >= 16:
            raise EditError("Join at most 16 clips")
        self._save()
        self.sources.append(source)
        self.segments.append(Segment(len(self.sources) - 1, 0.0, source.duration))

    def fast_starts(self) -> list[float]:
        """Where each segment really starts in Fast mode: the keyframe at or before it."""
        starts = []
        for s in self.segments:
            keys = [k for k in self.sources[s.source].keyframes if k <= s.start + 1e-3]
            starts.append(keys[-1] if keys else 0.0)
        return starts

    def fast_duration(self) -> float:
        """Length after Fast export, which starts each segment on a keyframe."""
        return sum(s.end - start for start, s in zip(self.fast_starts(), self.segments))

    def fast_problems(self, tracks: list[int]) -> list[str]:
        problems = []
        used = sorted({s.source for s in self.segments})
        first = self.sources[used[0]]
        for i in used[1:]:
            problems += [f"{Path(self.sources[i].path).name}: {p}" for p in first.compatible(self.sources[i])]
        if any(t >= len(self.sources[i].audio) for i in used for t in tracks):
            problems.append("an audio track is missing from one of the clips")
        return problems


def output_path(first: str | Path) -> Path:
    first = Path(first)
    for n in range(1, 1000):
        candidate = first.with_name(f"{first.stem} edit {n}{first.suffix}")
        if not candidate.exists():
            return candidate
    raise EditError("Too many edited copies already")


def _vaapi_device(codec: str) -> str | None:
    from .encoder_probe import render_nodes, probe_node
    for node in render_nodes():
        if probe_node(node).get(codec):
            return node
    return None


def _windows_encoder(codec: str, hdr: bool, kbps: int) -> list[str] | None:
    """Encoder arguments for the graphics card recording uses (NVIDIA, AMD or Intel,
    tested the same way), in the source's own format; None when no card can encode it.
    The bundled Windows FFmpeg has no CPU AV1 or HEVC encoder."""
    from .recorder import RecorderError
    from .windows import encoding
    from .windows.capture import chosen_display
    from .windows.displays import sources

    display = chosen_display(sources(), "")
    if display is None:
        return None
    cfg = {"codec": codec + ("_10bit" if hdr else ""), "encoder": "auto", "color_mode": "sdr"}
    try:
        _, family = encoding.select_encoder(cfg, display, codec)
    except RecorderError:
        return None
    args = encoding.encoder_options({"rate_control": "bitrate", "bitrate_mbps": max(1, round(kbps / 1000)),
                                     "_ten_bit": hdr}, family, codec)
    if family == "qsv":
        args = ["-init_hw_device", f"qsv=encoder:hw,child_device={display['adapter']},child_device_type=d3d11va",
                *args]
    return args


def command(edit: Edit, output: Path, mode: str, tracks: list[int], mix: bool, workdir: Path) -> list[str]:
    """The FFmpeg command for an export (see the module docstring)."""
    if mode not in ("fast", "exact"):
        raise EditError("Export mode must be fast or exact")
    if not edit.segments:
        raise EditError("Nothing left to export")
    first = edit.sources[edit.segments[0].source]
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-progress", "pipe:1", "-nostats", "-n"]
    from .host import is_windows
    # The bundled Windows FFmpeg includes AAC, but does not include libopus.
    audio_codec = ["-c:a", "aac", "-b:a", "192k"] if is_windows() or any(a["codec_name"] == "aac" for a in first.audio) \
        else ["-c:a", "libopus", "-b:a", "160k"]
    if mode == "fast":
        problems = edit.fast_problems(tracks)
        if problems:
            raise EditError("Fast export needs matching clips (" + "; ".join(problems[:3]) + "). Choose Exact to convert them.")
        listing = workdir / "segments.ffconcat"
        lines = ["ffconcat version 1.0"]
        for start, segment in zip(edit.fast_starts(), edit.segments):
            path = edit.sources[segment.source].path.replace("\\", "/").replace("'", "'\\''")
            lines += [f"file '{path}'", f"inpoint {start:.6f}", f"outpoint {segment.end:.6f}"]
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        argv += ["-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0:v:0"]
        if mix and len(tracks) > 1:
            inputs = "".join(f"[0:a:{t}]" for t in tracks)
            argv += ["-filter_complex", f"{inputs}amix=inputs={len(tracks)}:normalize=0[mix]", "-map", "[mix]",
                     "-c:v", "copy", *audio_codec]
        else:
            for t in tracks:
                argv += ["-map", f"0:a:{t}"]
            argv += ["-c", "copy"]
        return argv + ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(output)]

    # Exact: decode, trim on the chosen times, scale joined clips to the first size, re-encode.
    used = sorted({s.source for s in edit.segments})
    index = {source: i for i, source in enumerate(used)}
    for source in used:
        # The Windows FFmpeg has no software AV1 decoder, and AV1 is what Jarvis
        # records by default: decode on the graphics card (FFmpeg falls back to
        # software for other formats if the card can't).
        argv += [*(["-hwaccel", "d3d11va"] if is_windows() else []), "-i", edit.sources[source].path]
    width, height = first.video["width"], first.video["height"]
    hdr = first.video.get("color_transfer") in ("smpte2084", "arib-std-b67")
    codec = first.video.get("codec_name") if first.video.get("codec_name") in ("h264", "hevc", "av1") else "h264"
    windows_encoder = None
    if is_windows():
        # Exact export used VA-API (Linux AMD/Intel) or the CPU; on Windows that meant
        # slow H.264 even on NVIDIA, and HDR clips failed: there is no libsvtav1 here.
        kbps_hint = max(1000, int(os.path.getsize(first.path) * 8 / first.duration / 1000 * 1.1))
        windows_encoder = _windows_encoder(codec, hdr, kbps_hint)
        if windows_encoder is None and hdr:
            raise EditError("No graphics card encoder can make HDR video here; choose Fast, or update the graphics driver")
    device = None if is_windows() else _vaapi_device(codec + ("_10bit" if hdr else ""))
    pixel = "p010" if hdr else "nv12"
    graph, labels = [], []
    for i, s in enumerate(edit.segments):
        n = index[s.source]
        graph.append(f"[{n}:v:0]trim=start={s.start:.6f}:end={s.end:.6f},setpts=PTS-STARTPTS,"
                     f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:-1:-1,"
                     f"fps={first.video.get('r_frame_rate', '60/1')},format={'p010le' if hdr else 'yuv420p'}[v{i}]")
        labels.append(f"[v{i}]")
        for t in tracks:
            if t >= len(edit.sources[s.source].audio):
                graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={s.length:.6f}[a{i}_{t}]")
            else:
                graph.append(f"[{n}:a:{t}]atrim=start={s.start:.6f}:end={s.end:.6f},asetpts=PTS-STARTPTS,"
                             f"aresample=48000,aformat=channel_layouts=stereo[a{i}_{t}]")
            labels.append(f"[a{i}_{t}]")
    outs = "[v]" + "".join(f"[t{t}]" for t in tracks)
    graph.append("".join(labels) + f"concat=n={len(edit.segments)}:v=1:a={len(tracks)}{outs}")
    final_audio = [f"[t{t}]" for t in tracks]
    if mix and len(tracks) > 1:
        graph.append("".join(final_audio) + f"amix=inputs={len(tracks)}:normalize=0[mix]")
        final_audio = ["[mix]"]
    # Aim for the source's own bitrate, so an edit is about as large per
    # second as the original rather than re-encoded at an arbitrary quality.
    kbps = max(1000, int(os.path.getsize(first.path) * 8 / first.duration / 1000 * 1.1))
    if windows_encoder:
        if windows_encoder[0] == "-init_hw_device":   # Intel needs its device before the inputs
            argv[1:1] = windows_encoder[:2]
            windows_encoder = windows_encoder[2:]
        video_out = "[v]"
        encoder = windows_encoder
    elif device:
        graph.append(f"[v]format={pixel},hwupload[vout]")
        video_out = "[vout]"
        encoder = ["-c:v", f"{codec}_vaapi", "-rc_mode", "VBR", "-b:v", f"{kbps}k", "-maxrate", f"{kbps * 2}k"]
        argv[1:1] = ["-vaapi_device", device]
        if hdr and codec == "hevc":
            encoder += ["-profile:v", "main10"]
    else:
        video_out = "[v]"
        if hdr:
            encoder = ["-c:v", "libsvtav1", "-preset", "8", "-b:v", f"{kbps}k", "-pix_fmt", "yuv420p10le"]
        else:
            encoder = ["-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{kbps}k", "-maxrate", f"{kbps * 2}k",
                       "-bufsize", f"{kbps * 2}k"]
    argv += ["-filter_complex", ";".join(graph), "-map", video_out]
    for label in final_audio:
        argv += ["-map", label]
    argv += encoder
    if hdr:
        argv += ["-color_primaries", "bt2020", "-color_trc", first.video.get("color_transfer"),
                 "-colorspace", "bt2020nc", "-color_range", "tv"]
    if tracks:
        argv += audio_codec
    return argv + ["-movflags", "+faststart", str(output)]


class Export:
    """One running export with progress, cancellation and a never-overwritten result."""

    def __init__(self, edit: Edit, mode: str, tracks: list[int], mix: bool,
                 progress: Callable[[float], None] | None = None):
        self.edit, self.mode, self.tracks, self.mix = edit, mode, tracks, mix
        self.progress = progress or (lambda _f: None)
        self.output = output_path(edit.sources[edit.segments[0].source].path)
        self._process: subprocess.Popen | None = None
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def run(self) -> Path:
        from .host import child_options
        if self._cancelled.is_set():
            raise EditError("Export cancelled; the original is unchanged")
        total = max(self.edit.duration, 0.1)
        with tempfile.TemporaryDirectory(prefix="jarvis-edit-") as work:
            work = Path(work)
            staging = self.output.with_name(f".{self.output.stem}.part{self.output.suffix}")
            argv = command(self.edit, staging, self.mode, self.tracks, self.mix, work)
            errors = work / "ffmpeg.log"
            with errors.open("w", encoding="utf-8") as log, \
                    subprocess.Popen(_executable(argv), stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8", errors="replace", **child_options()) as process:
                self._process = process
                if self._cancelled.is_set():
                    process.terminate()
                for line in process.stdout:
                    key, _, value = line.strip().partition("=")
                    if key == "out_time_us" and value.isdigit():
                        self.progress(min(1.0, int(value) / 1e6 / total))
                code = process.wait()
            if self._cancelled.is_set() or code:
                staging.unlink(missing_ok=True)
                if self._cancelled.is_set():
                    raise EditError("Export cancelled; the original is unchanged")
                detail = errors.read_text(encoding="utf-8", errors="replace")[-400:].strip().splitlines()
                raise EditError("Export failed: " + (detail[-1] if detail else f"FFmpeg exit {code}"))
            # Publish without replacing a file that appeared meanwhile.
            try:
                os.link(staging, self.output)
            except FileExistsError:
                self.output = output_path(self.output)
                os.link(staging, self.output)
            staging.unlink()
            self.progress(1.0)
            names = [self.edit.sources[0].audio[t]["name"] for t in self.tracks if t < len(self.edit.sources[0].audio)]
            if not self.mix and len(names) > 1:
                (self.output.parent / f".{self.output.name}.tracks.json").write_text(json.dumps({"tracks": names}), encoding="utf-8")
            return self.output
