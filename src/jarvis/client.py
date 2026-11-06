"""Private local JSON IPC: Unix socket on Linux, authenticated loopback on Windows."""
import json
import os
from pathlib import Path
import socket
from contextlib import contextmanager

from .host import is_windows


def socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) / "jarvis" if runtime else Path(f"/tmp/jarvis-{os.getuid()}")
    return os.environ.get("JARVIS_SOCKET", str(base / "control.sock"))


@contextmanager
def connection(timeout, payload):
    if is_windows():
        from .windows.transport import connect
        sock, token = connect(timeout)
        payload = {**payload, "auth": token}
    else:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(socket_path())
        except BaseException:
            sock.close()
            raise
    with sock:
        sock.sendall((json.dumps(payload) + "\n").encode())
        yield sock


# Operations that can legitimately take longer than a status query. Stopping a
# recording waits up to ~16 s for wf-recorder, then validates the file with ffprobe.
TIMEOUTS = {"record_stop": 45, "record_start": 20, "execute": 45, "save_config": 110,
            "replay_save": 45, "replay_start": 20, "set_startup": 15, "apps": 20}


def request(op: str, timeout: float | None = None, **kwargs) -> dict:
    with connection(timeout or TIMEOUTS.get(op, 12), {"op": op, **kwargs}) as sock:
        with sock.makefile("rb") as f:
            line = f.readline(1048577)
        if not line or len(line) > 1048576:
            raise ConnectionError("Invalid response from Jarvis service")
        response = json.loads(line)
        if not isinstance(response, dict):
            raise ConnectionError("Invalid response from Jarvis service")
        return response


# Replay settings a running replay can't change in place; with any recording
# setting they make a save restart the replay (mirrors service.rs).
REPLAY_CAPTURE_KEYS = ("seconds", "game_audio", "microphone", "storage", "max_mb", "buffer_dir", "audio_tracks",
                       "separate_apps", "own_video", "codec", "fps", "resolution", "rate_control",
                       "quality_preset", "bitrate_mbps", "bitrate_auto")


def save_config_restarting(config: dict) -> dict:
    """Save settings. The service stops a running recording or replay whose
    settings changed (a recording is saved as its file), saves, and starts it
    again; the reply lists what restarted ("restarted", "restart_errors")."""
    return request("save_config", config=config)


def restart_note(result: dict) -> str:
    """What save_config_restarting did, in a sentence ("" when nothing ran)."""
    parts = {"record": "the recording was saved and a new one started",
             "replay": "instant replay restarted (its earlier history was cleared)"}
    done = [parts[kind] for kind in result.get("restarted", [])]
    note = (" With the new settings, " + " and ".join(done) + ".") if done else ""
    if result.get("restart_errors"):
        note += " Could not start again: " + "; ".join(result["restart_errors"])
    return note


def watch(callback, stop_event):
    with connection(None, {"op": "watch"}) as sock:
        with sock.makefile("rb") as f:
            while not stop_event.is_set():
                line = f.readline(65537)
                if not line or len(line) > 65536:
                    break
                callback(json.loads(line))
