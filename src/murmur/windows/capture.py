"""Owned FFmpeg recording with capped RAM or opt-in Rust disk replay storage."""

from __future__ import annotations
from collections import deque
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid

from ..host import child_options, command as worker_command, tool
from ..recorder import RecorderError, capture_color, verify_video
from . import encoding


def audio_sources():
    from .audio import recording_sources

    return recording_sources()


def sources():
    from .displays import sources as discover

    return discover()


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


class _Capture:
    replay = False

    def __init__(self, config, on_change=None):
        self.config, self.on_change = config, on_change
        self._lock = threading.RLock()
        self._process, self._audio, self._segments, self._buffer = (
            None,
            None,
            None,
            None,
        )
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
        partials = sorted(folder.glob(".Murmur_*.part.mkv"))
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
        cfg = dict(self.config["recording"])
        if not cfg.get("configured"):
            raise RecorderError("Complete and review recording setup first")
        display = next((d for d in sources() if d["name"] == cfg["monitor"]), None)
        if display is None:
            raise RecorderError("Choose a connected recording display")
        color = capture_color(cfg, display)
        if self.replay:
            cfg.update(
                system_audio=self.config["replay"]["game_audio"],
                microphone=self.config["replay"]["microphone"],
                audio_device="",
            )
        codec = encoding.format_for(cfg, color)
        binary, family = encoding.select_encoder(cfg, display, codec)
        folder = Path(cfg["output_dir"]).expanduser().resolve()
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
                audio_pipe = (
                    prefix + "-audio"
                    if cfg["system_audio"] or cfg["microphone"]
                    else None
                )
                argv, self._color, self._codec = encoding.command(
                    cfg, display, binary, family, audio_pipe=audio_pipe
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
                        "1",
                        "-segment_wrap",
                        "3",
                        "-reset_timestamps",
                        "1",
                        "-segment_format",
                        "matroska",
                        prefix + "-%03d",
                    ]
                else:
                    partial = folder / f".Murmur_{tag}.part.mkv"
                    final = folder / f"Murmur_{tag}.mkv"
                    # Reserve without overwriting; FFmpeg receives an explicitly
                    # reserved staging file and only that file may be replaced.
                    with partial.open("xb"):
                        pass
                    argv[argv.index("-n")] = "-y"
                    argv += ["-f", "matroska", str(partial)]
                if audio_pipe:
                    self._audio = subprocess.Popen(
                        worker_command("audio", audio_pipe),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        **child_options(),
                    )
                    self._audio.stdin.write(json.dumps(cfg).encode() + b"\n")
                    self._audio.stdin.close()
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
                    + (f" · {self.config['replay']['storage']} · {self.config['replay']['max_mb']} MiB budget" if self.replay else ""),
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
        try:
            while process.poll() is None:
                # This wait is a process wait, not a frame/desktop polling loop.
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    if (
                        not self._requested
                        and self._audio is not None
                        and self._audio.poll() is not None
                    ):
                        self._abort(
                            "Recording audio stopped unexpectedly; check Windows audio permissions"
                        )
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

    def _cleanup(self):
        if self._segments:
            self._segments.close()
            self._segments = None
        if self._audio:
            if self._audio.poll() is None:
                self._audio.terminate()
            try:
                self._audio.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._audio.kill()
                self._audio.wait()
            if self._audio.stderr:
                self._audio.stderr.close()
            self._audio = None
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

    def save(self):
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
            # Wait for the current independent segment so the requested moment
            # is included. This may add up to one second of later video.
            tag = (
                datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                + "_"
                + uuid.uuid4().hex[:10]
            )
            partial = folder / f".Murmur_replay_{tag}.part.mkv"
            final = folder / f"Replay_{tag}.mkv"
            with buffer.files(after=time.monotonic(), timeout=2) as paths:
                # The listing sits beside the segments. Use simple relative
                # names so FFmpeg's safe concat mode accepts paths on Windows.
                root = paths[0].parent
                lines = []
                for path in paths:
                    lines.append(f"file '{path.name}'")
                listing = root / "segments.txt"
                listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
                result = subprocess.run(
                    [
                        tool("ffmpeg"),
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
            if self._error:
                self._set("error", f"{self._error} Clip saved: {final.name}", final)
            else:
                self._set(
                    "on" if self._process is not None and not self._requested else "off",
                    f"Saved replay: {final.name} · {len(paths)} seconds retained ({self.config['replay']['max_mb']} MiB {self.config['replay']['storage']} budget)",
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
