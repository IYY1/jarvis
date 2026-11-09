"""Legacy Python replay: gpu-screen-recorder keeps the last seconds in RAM.

Nothing reaches the disk until a save is requested. The child is owned by the
service (it stops with SIGINT if the service dies, which discards the buffer)
and runs only while the user has turned replay on. Saves go through
gpu-screen-recorder's private IPC socket, whose reply names the saved file.
"""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading

from .client import socket_path
from .encoding import expected_codec, replay_video, with_auto_bitrate
from .recorder import (RecorderError, _parent_death_prefix, _spawn, capture_color,
                       gpu_video_args, sources, verify_video)

BACKEND = "gpu-screen-recorder"


def valid_app_name(name: str) -> bool:
    """A name gpu-screen-recorder can match as app:NAME (its syntax uses | and :)."""
    return bool(name.strip()) and len(name) <= 64 and all(c.isalnum() or c in " ._-" for c in name)


# Apps with their own replay track (mirrors audio_apps.rs): the ones named in
# settings, those playing sound when replay starts and those heard before.
MAX_APPS = 8
REMEMBERED = 16
OWN_STREAMS = ("jarvis-ui", "pw-play", "pw-cat")   # Jarvis's own sound stays in "Everything else"


def heard_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(state) / "jarvis" / "heard-apps.json"


def heard() -> list[dict]:
    """Apps heard before, newest first: [{"name", "label"}]."""
    try:
        apps = json.loads(heard_path().read_text())["apps"]
        return [{"name": a["name"], "label": a.get("label") or a["name"]} for a in apps if isinstance(a.get("name"), str)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def remember(apps: list[dict]) -> None:
    if not apps:
        return
    kept = list(apps) + [a for a in heard() if a["name"].casefold() not in {b["name"].casefold() for b in apps}]
    path = heard_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps({"apps": kept[:REMEMBERED]}))
        os.replace(temporary, path)
    except OSError:
        pass


def label(name: str, program: str | None) -> str:
    """Discord's voice plays as "WEBRTC VoiceEngine": show the program instead."""
    lower = name.casefold()
    if program and program.strip() and ("webrtc" in lower or "voiceengine" in lower):
        return program.strip()[:1].upper() + program.strip()[1:]
    return name


def _wanted(name: str) -> bool:
    return valid_app_name(name) and name.casefold() not in OWN_STREAMS


def playing() -> list[dict]:
    """Apps playing sound now, as gpu-screen-recorder lists them. Best effort."""
    try:
        listing = subprocess.run([BACKEND, "--list-application-audio"], capture_output=True, text=True, timeout=5)
        names = [line.strip() for line in listing.stdout.splitlines() if _wanted(line.strip())]
    except (OSError, subprocess.SubprocessError):
        return []
    programs = {}
    if names:
        try:
            dump = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout or "[]")
            for item in dump:
                props = item.get("info", {}).get("props", {}) if isinstance(item, dict) else {}
                if props.get("media.class") == "Stream/Output/Audio" and props.get("application.name"):
                    programs[props["application.name"]] = props.get("application.process.binary")
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            pass
    return [{"name": name, "label": label(name, programs.get(name))} for name in names]


def choose(replay: dict, playing_now: list[dict], heard_before: list[dict]) -> list[dict]:
    """Named first, then playing, then heard before; at most MAX_APPS, each once."""
    known = {a["name"].casefold(): a["label"] for a in [*playing_now, *heard_before]}
    named = [{"name": n, "label": known.get(n.casefold(), n)} for n in replay.get("separate_apps", []) if isinstance(n, str)]
    apps: list[dict] = []
    for app in [*named, *playing_now, *heard_before]:
        if len(apps) == MAX_APPS:
            break
        if _wanted(app["name"]) and app["name"].casefold() not in {a["name"].casefold() for a in apps}:
            apps.append(app)
    return apps


def replay_apps(replay: dict) -> list[dict]:
    """The apps for a replay starting now; apps playing are remembered."""
    if replay.get("audio_tracks", "mixed") != "separate":
        return []
    if not replay.get("game_audio", True):
        return choose(replay, [], [])   # no desktop sound: only the apps you named
    now = playing()
    remember(now)
    return choose(replay, now, heard())


def audio_tracks(replay: dict, apps: list[str] | None = None) -> list[str]:
    """Replay audio sources, one -a value per track; mirrors capture.rs.

    Mixed (default): game sound and microphone share one track. Separate: each
    app in `apps` (default: the ones named in settings), everything else, and
    the microphone get their own tracks. Everything else uses app-inverse:,
    never default_output, which would also contain the apps."""
    game, mic = replay.get("game_audio", True), replay.get("microphone", True)
    if replay.get("audio_tracks", "mixed") != "separate":
        mixed = [name for name, on in (("default_output", game), ("default_input", mic)) if on]
        return ["|".join(mixed)] if mixed else []
    if apps is None:
        apps = [a for a in replay.get("separate_apps", []) if valid_app_name(a)]
    tracks = []
    if game:
        tracks.append("|".join(f"app-inverse:{a}" for a in apps) if apps else "default_output")
    tracks += [f"app:{a}" for a in apps]
    if mic:
        tracks.append("default_input")
    return tracks


def track_names(replay: dict, apps: list[dict] | None = None) -> list[str]:
    """Human names for audio_tracks, in the same order (mirrors capture.rs)."""
    game, mic = replay.get("game_audio", True), replay.get("microphone", True)
    if replay.get("audio_tracks", "mixed") != "separate":
        return {(True, True): ["Game sound and microphone"], (True, False): ["Game sound"],
                (False, True): ["Microphone"]}.get((bool(game), bool(mic)), [])
    if apps is None:
        apps = [{"name": a, "label": a} for a in replay.get("separate_apps", []) if valid_app_name(a)]
    names = (["Everything else" if apps else "Game and desktop sound"] if game else []) + [a["label"] for a in apps]
    return names + (["Microphone"] if mic else [])


def write_track_names(clip: Path, names: list[str]) -> None:
    """MP4 replays lose the recorder's track titles; keep them in a hidden
    `.NAME.tracks.json` beside the clip. Best effort. An MKV keeps its track
    titles itself (the clip editor reads them), so it gets no extra file."""
    if len(names) < 2 or clip.suffix.lower() == ".mkv":
        return
    try:
        names_file = clip.parent / f".{clip.name}.tracks.json"
        names_file.write_text(json.dumps({"tracks": names}))
        hide(names_file)
    except OSError:
        pass


def hide(path: Path) -> None:
    """On Windows a leading dot doesn't hide a file: mark it hidden. Best effort."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)   # FILE_ATTRIBUTE_HIDDEN


def command(cfg: dict, replay: dict, folder: Path, ipc: Path, *, color: str = "sdr",
            apps: list[str] | None = None) -> list[str]:
    """gpu-screen-recorder replay mode for the configured monitor (see audio_tracks)."""
    argv = [BACKEND, "-w", cfg["monitor"], "-f", str(cfg.get("fps", 30)), "-r", str(replay.get("seconds", 120)),
            "-replay-storage", "ram", "-c", "mp4", "-o", str(folder), "-ipc", str(ipc), "-v", "no"] + gpu_video_args(color, cfg)
    for track in audio_tracks(replay, apps):
        argv += ["-a", track]
    return argv


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
        self._apps: list[dict] = []   # the apps with their own track in the running replay

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
                cfg = replay_video(self.config) if "recording" in self.config else self.config
                replay = self.config.get("replay", {})
                if replay.get("storage", "ram") == "disk":
                    raise RecorderError("Disk replay requires the Rust controller on Linux. Build scripts/build_controller.py and select JARVIS_CONTROLLER=rust.")
                monitor = str(cfg.get("monitor", "")).strip()
                if not monitor:
                    raise RecorderError("Choose a display in Recorder settings first.")
                display = next((item for item in sources() if item["name"] == monitor), None)
                if display is None:
                    raise RecorderError(f"Display {monitor!r} is unavailable. Choose a connected display.")
                color = capture_color(cfg, display)
                cfg = with_auto_bitrate(cfg, display)
                if not shutil.which("ffprobe"):
                    raise RecorderError("ffprobe is missing. Install FFmpeg to validate saved replays.")
                folder = Path(str(cfg.get("output_dir", "~/Videos/Jarvis"))).expanduser().resolve()
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.ipc.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._apps = replay_apps(replay)
                argv = command({**cfg, "monitor": monitor}, replay, folder, self.ipc, color=color,
                               apps=[a["name"] for a in self._apps])
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
                threading.Thread(target=self._watch, args=(process,), name="jarvis-replay", daemon=True).start()
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

    def _request(self, name: str, timeout: float = 30, data=None) -> str:
        """One request on gpu-screen-recorder's socket; replies once the file is written."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(str(self.ipc))
                request = {"id": 1, "name": name} if data is None else {"id": 1, "name": name, "data": data}
                connection.sendall((json.dumps(request) + "\n").encode())
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

    def save(self, seconds: int | None = None) -> dict:
        """Save the whole buffer, or only its last ``seconds``."""
        with self._lock:
            if self._process is None:
                raise RecorderError("Instant replay is off. Say “start replay” first.")
            self._set("on", "Saving the replay…", self._status.get("file"))
            color, codec = self._color, self._codec
        self._notify()
        path = None
        try:
            path = Path(self._request("save-replay", data=None if seconds is None else {"seconds": seconds}))
            verify_video(path, color=color, codec=codec)
            if self.config.get("recording", {}).get("game_folders"):
                from .games import into_game_folder
                path = into_game_folder(path)   # the game focused when the clip is saved
            names = track_names(self.config.get("replay", {}), self._apps)
            write_track_names(path, names)
            if len(names) > 2:
                threading.Thread(target=lambda: remember(playing()), daemon=True).start()   # their own track next time
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
