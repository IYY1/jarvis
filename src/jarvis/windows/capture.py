"""Owned FFmpeg recording with capped RAM or opt-in Rust disk replay storage."""

from __future__ import annotations
from collections import deque
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from ..host import ENCODER_NAME, child_options, command as worker_command, grouped, tool
from ..recorder import RecorderError, verify_video
from ..replay import write_track_names
from ..encoding import replay_fit_seconds, replay_video, with_auto_bitrate
from . import encoding


READY = "ready"   # the first line a capture audio worker prints (windows/audio.py)
SEGMENT_SECONDS = 1   # replay segment length; saving joins them at exactly this
AUDIO_START_SECONDS = 10
TRIM_AFTER_SECONDS = (3, 30)   # once started, and once settled
TRIM_EVERY_SECONDS = 300


def audio_sources():
    from .audio import recording_sources

    return recording_sources()


def sources():
    from .displays import sources as discover

    return discover()


def chosen_display(displays, name):
    """The named display. No name means "Primary display", as the window calls it:
    the one Windows places at the desktop's origin."""
    if name:
        return next((d for d in displays if d["name"] == name), None)
    return next((d for d in displays if (d.get("left"), d.get("top")) == (0, 0)), displays[0] if displays else None)


def replay_budget_warning(cfg, replay, audio_tracks):
    """Status text when the budget can't hold the chosen length at this bitrate."""
    fit = replay_fit_seconds(cfg, replay["max_mb"], audio_tracks)
    if fit is None or fit >= replay["seconds"]:
        return ""
    return (f" · About {fit} of {replay['seconds']} seconds fit in this budget;"
            " raise the budget or lower the bitrate")


def replay_shortfall_warning(wanted, retained, captured):
    """Status text when a clip lost history to the budget rather than to warm-up.

    Two seconds of margin: more segments can arrive while a save prepares its files.
    """
    if retained >= wanted or captured <= wanted + 2:
        return ""
    return f" · The budget kept only {retained} of {wanted} seconds; raise it or lower the bitrate"


class ReplayBuffer:
    """One-second independent Matroska segments; caps both time and total bytes."""

    def __init__(self, seconds, max_bytes=256 * 1024 * 1024):
        self.seconds, self.max_bytes = seconds, max_bytes
        self.segments, self.pending = deque(), {}
        self.size, self.next = 0, 0
        self.changed = threading.Condition()
        self.last = 0.0

    def append(self, data, sequence):
        if len(data) > self.max_bytes:
            raise RecorderError(
                "Replay segment exceeds the RAM budget; lower the bitrate"
            )
        with self.changed:
            if (
                sequence < self.next
                or sequence in self.pending
                or sequence > self.next + 2
            ):
                raise RecorderError("Replay segment sequence is invalid")
            self.pending[sequence] = (data, time.monotonic())
            if len(self.pending) > 3:
                raise RecorderError(
                    "Replay segments arrived out of order beyond the allowed window"
                )
            while self.next in self.pending:
                block, end = self.pending.pop(self.next)
                self.next += 1
                self.segments.append(block)
                self.size += len(block)
                self.last = max(self.last, end)
                while self.size > self.max_bytes or len(self.segments) > self.seconds:
                    self.size -= len(self.segments.popleft())
            self.changed.notify_all()

    def snapshot(self, *, after=0, timeout=0):
        with self.changed:
            if after:
                self.changed.wait_for(lambda: self.last >= after, timeout)
            if after and self.last < after:
                raise RecorderError(
                    "Replay could not finalize the current second; check capture status and try again"
                )
            if not self.segments:
                raise RecorderError("Replay is warming up; wait for one full second")
            # Immutable bytes are shared, not duplicated while a clip is saved.
            return tuple(self.segments)

    @contextmanager
    def files(self, *, after=0, timeout=0):
        blocks = self.snapshot(after=after, timeout=timeout)
        # The RAM privacy promise permits temporary files only after Save.
        with tempfile.TemporaryDirectory(prefix=".replay-save-") as directory:
            if os.name == "nt":
                from .system import private_directory
                private_directory(Path(directory))
            paths = []
            for index, block in enumerate(blocks):
                path = Path(directory) / f"segment-{index:04d}.mkv"
                path.write_bytes(block)
                paths.append(path)
            yield paths

    def close(self):
        pass


def audio_failure(process, title):
    """The reason an audio worker ended, from the last line it wrote. The packaged
    worker ends with PyInstaller's own "[PYI-...] Failed to execute script" line,
    which says nothing; the exception is the line before it."""
    try:
        lines = process.stderr.read(8192).decode(errors="replace").strip().splitlines()
    except (OSError, ValueError):
        lines = []
    lines = [line for line in lines if line.strip() and not line.startswith("[PYI-")]
    reason = lines[-1].split(": ", 1)[-1] if lines else ""  # drop "RuntimeError: "
    try:
        reason = json.loads(lines[-1])["message"]   # the native worker's {"event":"error",...}
    except (IndexError, ValueError, KeyError, TypeError):
        pass
    track = f"The {title} track" if title else "Recording audio"
    return f"{track} stopped: {reason}" if reason else f"{track} stopped unexpectedly; check Windows audio permissions"


def screen_failure(process):
    """The reason the worker's screen mode ended, from its last line."""
    try:
        lines = process.stderr.read(8192).decode(errors="replace").strip().splitlines()
    except (OSError, ValueError):
        lines = []
    for line in reversed(lines):
        try:
            message = json.loads(line).get("message")
        except (ValueError, AttributeError):
            continue
        if message:
            return f"Screen capture stopped: {message}"
    return "Screen capture stopped unexpectedly; check the graphics driver"


def start_screen(request):
    """The worker's screen mode, for a display in HDR mode. Waits until it can capture."""
    from ..native_worker import binary_path

    worker = binary_path()
    if worker is None:
        raise RecorderError("The Windows voice worker is missing; reinstall the complete Jarvis package")
    process = subprocess.Popen(
        [str(grouped(worker)), "--screen"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **child_options(),
    )
    process.stdin.write(json.dumps(request).encode() + b"\n")
    process.stdin.close()
    return process


def wait_for_screen(process, seconds=AUDIO_START_SECONDS):
    outcome = _wait_ready(process, seconds)
    if outcome == "late":
        raise RecorderError("Screen capture did not start; check the graphics driver")
    if outcome == "ended":
        process.wait(timeout=5)
        raise RecorderError(screen_failure(process))


def _wait_ready(process, seconds):
    """Read the worker's "ready" line: "ready", "late", or "ended" without one."""
    ready = threading.Event()

    def watch():
        # A driver may print its own lines first.
        while line := process.stdout.readline():
            if line.strip() == READY.encode():
                ready.set()
                return

    reader = threading.Thread(target=watch, daemon=True)
    reader.start()
    reader.join(seconds)
    if reader.is_alive():
        return "late"
    if ready.is_set():
        process.stdout.close()
        return "ready"
    return "ended"


def wait_for_audio(process, title, seconds=AUDIO_START_SECONDS):
    """Hold FFmpeg back until the audio worker's sound is flowing.

    Sound that starts after video leaves video permanently ahead of it, and FFmpeg
    then discards most of the frames. Also, the worker's pipe must exist by now.
    """
    outcome = _wait_ready(process, seconds)
    if outcome == "late":
        track = f"The {title} track" if title else "Recording audio"
        raise RecorderError(f"{track} did not start; check Windows audio devices and permissions")
    if outcome == "ended":
        process.wait(timeout=5)
        raise RecorderError(audio_failure(process, title))


class _Capture:
    replay = False

    def __init__(self, config, on_change=None):
        self.config, self.on_change = config, on_change
        self._lock = threading.RLock()
        self._process, self._segments, self._buffer = None, None, None
        self._audio = []  # [(audio worker process, track title or None)]
        self._mixed = False  # a first track adds up the others
        self._titles = []    # the saved clip's track names, in order
        self._screen = None  # the worker's screen mode, for a display in HDR mode
        self._done = threading.Event()
        self._done.set()
        self._requested, self._forced, self._error = False, False, None
        self._status = {
            "state": "off" if self.replay else "idle",
            "file": None,
            "message": "Replay off" if self.replay else "Recorder ready",
        }
        self._color, self._codec, self._folder = "sdr", "h264", None
        self._saving = threading.Lock()

    def status(self):
        with self._lock:
            return dict(self._status)

    def _set(self, state, message, file=None):
        with self._lock:
            self._status = {
                "state": state,
                "message": message,
                "file": str(file) if file else None,
            }
        if self.on_change:
            try:
                self.on_change(self.status())
            except Exception:
                pass

    def sources(self):
        return sources()

    def inspect_previous(self):
        folder = Path(self.config["recording"]["output_dir"]).expanduser()
        partials = sorted(folder.glob(".Jarvis_*.part.mkv"))
        if partials:
            self._set(
                "idle",
                f"Found {len(partials)} unfinished recordings. Partial files are retained in {folder}.",
                partials[-1],
            )

    def _abort(self, message):
        self._error = message
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def _validated(self):
        cfg = dict(replay_video(self.config) if self.replay else self.config["recording"])
        if not cfg.get("configured"):
            raise RecorderError("Complete and review recording setup first")
        display = chosen_display(sources(), cfg["monitor"])
        if display is None:
            raise RecorderError("Choose a connected recording display")
        cfg = encoding.resolve_codec(cfg, display)   # "auto": the best format this GPU encodes
        color = encoding.windows_color(cfg, display)
        cfg["fps"] = encoding.capped_fps(cfg["fps"], display)
        cfg = dict(with_auto_bitrate(cfg, display))
        if self.replay:
            cfg.update(
                system_audio=self.config["replay"]["game_audio"],
                microphone=self.config["replay"]["microphone"],
                audio_device="",
            )
        codec = encoding.format_for(cfg, color)
        binary, family = encoding.select_encoder(cfg, display, codec)
        folder = Path(cfg["output_dir"]).expanduser().resolve()
        if cfg.get("game_folders") and not self.replay:
            from ..games import focused_game
            folder = folder / (focused_game() or "")   # the game focused when recording starts
        folder.mkdir(parents=True, exist_ok=True)
        return cfg, display, binary, family, folder

    def start(self):
        with self._lock:
            if self._process is not None:
                if self._status["state"] == "stopping":
                    raise RecorderError("The previous capture is still being finalized")
                return self.status()
            partial, final = None, None
            try:
                cfg, display, binary, family, folder = self._validated()
                tag = (
                    datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    + "_"
                    + uuid.uuid4().hex[:10]
                )
                prefix = r"\\.\pipe\Jarvis-" + uuid.uuid4().hex
                tracks = self.config["replay"]
                labels = {}
                if tracks.get("sources") == "specific" and tracks.get("separate_apps"):
                    from .audio import apps_with_sound
                    labels = {a["name"]: a["label"] for a in apps_with_sound()}
                plan = encoding.audio_plan(
                    cfg, tracks, sys.getwindowsversion().build if hasattr(sys, "getwindowsversion") else 0,
                    labels, recording=not self.replay)
                pipes = [(f"{prefix}-audio{i}", title) for i, (title, _) in enumerate(plan)]
                mix = encoding.mixed_inputs(plan)
                separate = tracks.get("audio_tracks") == "separate"
                self._mixed = bool(mix)
                # The track names a saved clip keeps, in order.
                self._titles = ([encoding.MIXED_TITLE] if mix else []) + (
                    [title for title, _ in plan if title] if separate or not mix else [])
                screen_pipe = f"{prefix}-video" if encoding.in_hdr_mode(display) else None
                argv, self._color, self._codec = encoding.command(
                    cfg, display, binary, family, audio_pipes=pipes, screen_pipe=screen_pipe, mix=mix,
                    volumes=encoding.plan_volumes(plan), separate=separate
                )
                self._folder = folder
                self._error, self._requested, self._forced = None, False, False
                if self.replay:
                    from .pipes import SegmentReader

                    replay = self.config["replay"]
                    if replay["storage"] == "disk":
                        from ..replay_store import DiskReplayBuffer
                        self._buffer = DiskReplayBuffer(replay["seconds"], replay["max_mb"] * 1024**2, replay["buffer_dir"])
                    else:
                        self._buffer = ReplayBuffer(replay["seconds"], replay["max_mb"] * 1024**2)
                    self._segments = SegmentReader(
                        prefix + "-", self._buffer.append, self._abort
                    )
                    argv += [
                        "-f",
                        "segment",
                        "-segment_time",
                        str(SEGMENT_SECONDS),
                        "-segment_wrap",
                        "3",
                        "-reset_timestamps",
                        "1",
                        "-segment_format",
                        "matroska",
                        prefix + "-%03d",
                    ]
                else:
                    partial = folder / f".Jarvis_{tag}.part.mkv"
                    final = folder / f"Jarvis_{tag}.mkv"
                    # Reserve without overwriting; FFmpeg receives an explicitly
                    # reserved staging file and only that file may be replaced.
                    with partial.open("xb"):
                        pass
                    argv[argv.index("-n")] = "-y"
                    argv += ["-f", "matroska", str(partial)]
                for (pipe, title), (_, worker_cfg) in zip(pipes, plan):
                    track = worker_cfg.get("track") or {}
                    if track.get("app") and track.get("kind") in ("app", "game"):
                        # A program's sound goes from the native worker straight to FFmpeg.
                        from .audio import start_app_audio
                        self._audio.append((start_app_audio(track["app"], track["kind"] == "app", pipe), title))
                        continue
                    audio = subprocess.Popen(
                        worker_command("audio", pipe),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        **child_options(),
                    )
                    self._audio.append((audio, title))
                    audio.stdin.write(json.dumps(worker_cfg).encode() + b"\n")
                    audio.stdin.close()
                if screen_pipe:
                    self._screen = start_screen(
                        encoding.screen_request(cfg, display, screen_pipe, self._color)
                    )
                    wait_for_screen(self._screen)
                for audio, title in self._audio:
                    wait_for_audio(audio, title)
                self._process = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    **child_options(),
                )
                self._done.clear()
                self._set(
                    "on" if self.replay else "recording",
                    f"{'Replay' if self.replay else 'Recorder'} running · {self._codec.upper()} · {self._color.upper()} · {family}"
                    + (f" · {self.config['replay']['storage']} · {self.config['replay']['max_mb']} MiB budget"
                       + replay_budget_warning(cfg, self.config["replay"], len(self._titles)) if self.replay else ""),
                    partial,
                )
                threading.Thread(
                    target=self._watch,
                    args=(self._process, partial, final),
                    daemon=True,
                    name="jarvis-capture",
                ).start()
                return self.status()
            except Exception as exc:
                if self._process is not None:
                    self._process.kill()
                    self._process.wait(timeout=5)
                    self._process = None
                self._done.set()
                self._cleanup()
                if partial and partial.exists() and partial.stat().st_size == 0:
                    partial.unlink()
                self._set(
                    "error", str(exc), partial if partial and partial.exists() else None
                )
                raise RecorderError(str(exc)) from exc

    def _watch(self, process, partial, final):
        tail = deque(maxlen=4)

        def drain(stream):
            try:
                while chunk := stream.read(4096):
                    tail.append(chunk)
            except (OSError, ValueError):
                pass

        diagnostics = threading.Thread(
            target=drain, args=(process.stderr,), daemon=True
        )
        diagnostics.start()
        started = time.monotonic()
        trims = iter(TRIM_AFTER_SECONDS)
        next_trim = next(trims)
        try:
            while process.poll() is None:
                # This wait is a process wait, not a frame/desktop polling loop.
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    if time.monotonic() - started >= next_trim:
                        self._release_memory(process)
                        next_trim = next(trims, next_trim + TRIM_EVERY_SECONDS)
                    stopped = next((a for a in self._audio if a[0].poll() is not None), None)
                    if not self._requested and stopped is not None:
                        self._abort(audio_failure(*stopped))
                    if not self._requested and self._screen is not None and self._screen.poll() is not None:
                        self._abort(screen_failure(self._screen))
            diagnostics.join(timeout=2)
            if self._error:
                raise RecorderError(self._error)
            if not self._requested or self._forced or process.returncode != 0:
                detail = b"".join(tail).decode(errors="replace")[-1600:]
                raise RecorderError(
                    f"Capture stopped (exit {process.returncode}). {detail}"
                )
            if self.replay:
                self._set("off", "Replay off; buffer released")
            else:
                verify_video(partial, color=self._color, codec=self._codec)
                with partial.open("r+b") as f:
                    os.fsync(f.fileno())
                os.rename(partial, final)
                self._set("idle", f"Saved recording: {final.name}", final)
        except (OSError, RuntimeError) as exc:
            if partial and partial.exists() and partial.stat().st_size == 0:
                partial.unlink()
            self._set(
                "error",
                str(exc)
                + (" Partial video retained." if partial and partial.exists() else ""),
                partial if partial and partial.exists() else None,
            )
        finally:
            if process.stdin:
                process.stdin.close()
            if process.stderr:
                process.stderr.close()
            self._cleanup()
            with self._lock:
                self._process = None
            self._done.set()

    def _release_memory(self, process):
        """FFmpeg and the workers touch far more memory starting up than they use."""
        from .system import release_unused_memory

        children = [process] + [audio for audio, _ in self._audio]
        if self._screen is not None:
            children.append(self._screen)
        for child in children:
            try:
                release_unused_memory(int(child._handle))
            except (AttributeError, OSError, ValueError):
                pass

    def _cleanup(self):
        if self._segments:
            self._segments.close()
            self._segments = None
        for audio, _ in self._audio:
            if audio.poll() is None:
                audio.terminate()
            try:
                audio.wait(timeout=3)
            except subprocess.TimeoutExpired:
                audio.kill()
                audio.wait()
            if audio.stderr:
                audio.stderr.close()
        self._audio = []
        if self._screen is not None:
            if self._screen.poll() is None:
                self._screen.terminate()
            try:
                self._screen.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._screen.kill()
                self._screen.wait()
            if self._screen.stderr:
                self._screen.stderr.close()
            self._screen = None
        if self._buffer:
            self._buffer.close()
            self._buffer = None

    def stop(self, timeout=15):
        with self._lock:
            process = self._process
            if process is None:
                return self.status()
            if not self._requested:
                self._requested = True
                self._set("stopping", "Finishing capture…", self._status["file"])
                try:
                    process.stdin.write(b"q\n")
                    process.stdin.flush()
                except (OSError, ValueError):
                    pass
        if not self._done.wait(timeout):
            self._forced = True
            process.terminate()
            if not self._done.wait(3):
                process.kill()
                self._done.wait(3)
        result = self.status()
        if result["state"] in ("error", "stopping"):
            raise RecorderError(result["message"])
        return result

    def close(self):
        if self._process is not None:
            self.stop()


class Recorder(_Capture):
    pass


class Replay(_Capture):
    replay = True

    def save(self, seconds=None):
        """Save the whole buffer, or only its last ``seconds``."""
        if not self._saving.acquire(blocking=False):
            raise RecorderError("A replay is already being saved")
        partial = None
        try:
            with self._lock:
                if self._process is None or self._buffer is None:
                    raise RecorderError("Start instant replay first")
                buffer, folder, color, codec = (
                    self._buffer,
                    self._folder,
                    self._color,
                    self._codec,
                )
                titles = list(self._titles)
            length = self.config["replay"]["seconds"]
            # Wait for the current independent segment so the requested moment
            # is included. This may add up to one second of later video.
            tag = (
                datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                + "_"
                + uuid.uuid4().hex[:10]
            )
            partial = folder / f".Jarvis_replay_{tag}.part.mkv"
            final = folder / f"Replay_{tag}.mkv"
            with buffer.files(after=time.monotonic(), timeout=2) as paths:
                captured = buffer.next
                if seconds is not None:
                    # One-second segments, plus the one finishing at the request.
                    paths = paths[-(seconds + 1):]
                # The listing sits beside the segments. Use simple relative
                # names so FFmpeg's safe concat mode accepts paths on Windows.
                root = paths[0].parent
                lines = []
                for path in paths:
                    lines.append(f"file '{path.name}'")
                    # Each segment's picture is exactly one second (key frames are
                    # forced on every second, frames are stamped by count). Left to
                    # itself, concat steps by the longest stream, and the sound runs
                    # past the picture's end: picture slipped ~25 ms a segment
                    # behind sound, a second per minute, with a stutter at each join.
                    lines.append(f"duration {SEGMENT_SECONDS}")
                listing = root / "segments.txt"
                listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
                result = subprocess.run(
                    [
                        grouped(tool("ffmpeg"), ENCODER_NAME),   # in Jarvis's row
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostdin",
                        "-n",
                        "-f",
                        "concat",
                        "-safe",
                        "1",
                        "-i",
                        listing.name,
                        "-map",
                        "0",
                        "-c",
                        "copy",
                        str(partial),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=45,
                    cwd=root,
                    **child_options(),
                )
                if result.returncode:
                    raise RecorderError(
                        "Replay remux failed: "
                        + result.stderr.decode(errors="replace")[-800:]
                    )
            verify_video(partial, color=color, codec=codec)
            with partial.open("r+b") as f:
                os.fsync(f.fileno())
            os.rename(partial, final)
            if self.config["recording"].get("game_folders"):
                from ..games import into_game_folder
                final = into_game_folder(final)   # the game focused when the clip is saved
            write_track_names(final, titles)
            if self._error:
                self._set("error", f"{self._error} Clip saved: {final.name}", final)
            else:
                self._set(
                    "on" if self._process is not None and not self._requested else "off",
                    f"Saved replay: {final.name} · {len(paths)} seconds retained ({self.config['replay']['max_mb']} MiB {self.config['replay']['storage']} budget)"
                    + replay_shortfall_warning(
                        min(seconds or length, length), len(paths), captured),
                    final,
                )
            return self.status()
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            retained = partial if partial and partial.is_file() else self._status.get("file")
            detail = f" Partial video retained at {retained}." if retained == partial and partial else ""
            self._set(
                "on" if self._process is not None and not self._error else "error",
                str(exc) + detail,
                retained,
            )
            raise RecorderError(str(exc)) from exc
        finally:
            self._saving.release()
