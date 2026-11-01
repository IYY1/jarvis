"""Legacy Python replay: gpu-screen-recorder keeps the last seconds in RAM.

Nothing reaches the disk until a save is requested. The child is owned by the
service (it stops with SIGINT if the service dies, which discards the buffer)
and runs only while the user has turned replay on. Saves go through
gpu-screen-recorder's private IPC socket, whose reply names the saved file.
"""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import threading

from .client import socket_path
from .encoding import expected_codec
from .recorder import (RecorderError, _parent_death_prefix, _spawn, capture_color,
                       gpu_video_args, sources, verify_video)

BACKEND = "gpu-screen-recorder"


def command(cfg: dict, replay: dict, folder: Path, ipc: Path, *, color: str = "sdr") -> list[str]:
    """gpu-screen-recorder replay mode for the configured monitor. Game sound (the
    default output) and the default microphone are mixed into one audio track."""
    argv = [BACKEND, "-w", cfg["monitor"], "-f", str(cfg.get("fps", 30)), "-r", str(replay.get("seconds", 120)),
            "-replay-storage", "ram", "-c", "mp4", "-o", str(folder), "-ipc", str(ipc), "-v", "no"] + gpu_video_args(color, cfg)
    audio = [name for name, wanted in (("default_output", replay.get("game_audio", True)),
                                       ("default_input", replay.get("microphone", True))) if wanted]
    return argv + (["-a", "|".join(audio)] if audio else [])


class Replay:
    """At most one replay child; idle instances have no thread or timer."""

    def __init__(self, config: dict, on_change=None, ipc: Path | None = None):
        self.config = config
        self.on_change = on_change
        self.ipc = ipc or Path(socket_path()).with_name("replay.sock")
        self._lock = threading.Lock()
        self._process = None
        self._color = "sdr"
        self._codec = None
        self._done = threading.Event()
        self._done.set()
        self._status = {"state": "off", "message": "Instant replay is off.", "file": None}

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _set(self, state, message, file=None):
        self._status = {"state": state, "message": message, "file": str(file) if file else None}

    def _notify(self):
        if self.on_change:
            try:
                self.on_change(self.status())
            except Exception:
                pass  # a disconnected client must not stop the replay buffer

    def start(self) -> dict:
        with self._lock:
            if self._process is not None:
                return dict(self._status)
            try:
                backend = shutil.which(BACKEND)
                if not backend:
                    raise RecorderError("Instant replay needs gpu-screen-recorder. Install it with: sudo pacman -S gpu-screen-recorder")
                cfg = self.config.get("recording", self.config)
                replay = self.config.get("replay", {})
                if replay.get("storage", "ram") == "disk":
                    raise RecorderError("Disk replay requires the Rust controller on Linux. Build scripts/build_controller.py and select MURMUR_CONTROLLER=rust.")
                monitor = str(cfg.get("monitor", "")).strip()
                if not monitor:
                    raise RecorderError("Choose a display in Recorder settings first.")
                display = next((item for item in sources() if item["name"] == monitor), None)
                if display is None:
                    raise RecorderError(f"Display {monitor!r} is unavailable. Choose a connected display.")
                color = capture_color(cfg, display)
                if not shutil.which("ffprobe"):
                    raise RecorderError("ffprobe is missing. Install FFmpeg to validate saved replays.")
                folder = Path(str(cfg.get("output_dir", "~/Videos/Murmur"))).expanduser().resolve()
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.ipc.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                argv = command({**cfg, "monitor": monitor}, replay, folder, self.ipc, color=color)
                argv[0] = backend
                process = _spawn(_parent_death_prefix() + argv, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True,
                                 umask=0o077)
            except (RecorderError, OSError, ValueError) as exc:
                self._set("error", str(exc))
                error = RecorderError(str(exc))
            else:
                error = None
                self._process = process
                self._color = color
                self._codec = expected_codec(cfg, color)
                self._done.clear()
                self._set("on", f"Instant replay on · {color.upper()} · keeping the last {replay.get('seconds', 120)} seconds in memory.")
                threading.Thread(target=self._watch, args=(process,), name="murmur-replay", daemon=True).start()
        self._notify()
        if error:
            raise error
        return self.status()

    def _watch(self, process):
        """Block on the child; keep at most 16 KiB of its diagnostics."""
        tail = deque(maxlen=4)
        while chunk := process.stderr.read(4096):
            tail.append(chunk)
        process.stderr.close()
        code = process.wait()
        with self._lock:
            requested = self._status["state"] == "stopping"
            self._process = None
            if requested:
                self._set("off", "Instant replay is off.")
            else:
                detail = b"".join(tail).decode("utf-8", errors="replace").strip()[-800:]
                self._set("error", f"Instant replay stopped (status {code}). {detail}".strip())
            self._done.set()
        self._notify()

    def _request(self, name: str, timeout: float = 30) -> str:
        """One request on gpu-screen-recorder's socket; replies once the file is written."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(str(self.ipc))
                connection.sendall((json.dumps({"id": 1, "name": name}) + "\n").encode())
                with connection.makefile("rb") as replies:
                    reply = json.loads(replies.readline(65536) or b"{}")
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise RecorderError("Instant replay is still starting; try again in a moment.") from exc
        except (OSError, ValueError) as exc:
            raise RecorderError(f"Instant replay did not answer: {exc}") from exc
        if not isinstance(reply, dict):
            raise RecorderError("Instant replay returned an invalid response")
        if reply.get("result") != "ok" or not isinstance(reply.get("data"), str):
            raise RecorderError(f"Replay not saved: {reply.get('data') or 'no reply'}")
        return reply["data"]

    def save(self) -> dict:
        with self._lock:
            if self._process is None:
                raise RecorderError("Instant replay is off. Say “start replay” first.")
            self._set("on", "Saving the replay…", self._status.get("file"))
            color, codec = self._color, self._codec
        self._notify()
        path = None
        try:
            path = Path(self._request("save-replay"))
            verify_video(path, color=color, codec=codec)
        except RecorderError as exc:
            detail = f"{exc} File retained at {path}." if path is not None else str(exc)
            with self._lock:
                if self._process is not None:
                    self._set("on", detail, self._status.get("file"))
            self._notify()
            raise RecorderError(detail) from exc
        with self._lock:
            if self._process is not None:
                self._set("on", f"Saved replay: {path.name}", path)
        self._notify()
        return self.status()

    def stop(self, timeout: float = 5) -> dict:
        with self._lock:
            process = self._process
            if process is None:
                return dict(self._status)
            self._set("stopping", "Stopping instant replay…")
            try:
                process.send_signal(signal.SIGINT)   # replay mode discards the buffer
            except ProcessLookupError:
                pass  # the watcher still owns exit handling and cleanup
        self._notify()
        if not self._done.wait(timeout):
            try:
                process.kill()
            except ProcessLookupError:
                pass
            self._done.wait(2)
        return self.status()

    def close(self):
        self.stop()
