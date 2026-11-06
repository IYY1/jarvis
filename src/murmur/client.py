"""Private local JSON IPC: Unix socket on Linux, authenticated loopback on Windows."""
import json
import os
from pathlib import Path
import socket
from contextlib import contextmanager

from .host import is_windows


def socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) / "murmur" if runtime else Path(f"/tmp/murmur-{os.getuid()}")
    return os.environ.get("MURMUR_SOCKET", str(base / "control.sock"))


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
TIMEOUTS = {"record_stop": 45, "record_start": 20, "execute": 45, "save_config": 30,
            "replay_save": 45, "replay_start": 20, "set_startup": 15, "apps": 20}


def request(op: str, timeout: float | None = None, **kwargs) -> dict:
    with connection(timeout or TIMEOUTS.get(op, 12), {"op": op, **kwargs}) as sock:
        with sock.makefile("rb") as f:
            line = f.readline(1048577)
        if not line or len(line) > 1048576:
            raise ConnectionError("Invalid response from Murmur service")
        response = json.loads(line)
        if not isinstance(response, dict):
            raise ConnectionError("Invalid response from Murmur service")
        return response


# Replay settings a running replay can't change (services refuse); with any
# recording setting they make save_config_restarting restart the replay.
REPLAY_CAPTURE_KEYS = ("seconds", "game_audio", "microphone", "storage", "max_mb", "buffer_dir", "audio_tracks",
                       "separate_apps", "own_video", "codec", "fps", "resolution", "rate_control",
                       "quality_preset", "bitrate_mbps")


def save_config_restarting(config: dict) -> dict:
    """Save settings. A running recording or replay whose settings changed is
    stopped (a recording is saved as its file), then started again with the
    new settings. The reply lists what was restarted and any start error."""
    current = request("config")
    if not current.get("ok"):
        return current
    old, status = current["config"], request("status").get("status", {})
    recording_changed = config["recording"] != old["recording"]
    restart = []
    if status.get("recording", {}).get("state") == "recording" and recording_changed:
        restart.append("record")
    if status.get("replay", {}).get("state") == "on" and (recording_changed or any(
            config["replay"].get(key) != old["replay"].get(key) for key in REPLAY_CAPTURE_KEYS)):
        restart.append("replay")
    for kind in restart:
        stopped = request(f"{kind}_stop")
        if not stopped.get("ok"):
            return stopped
    result = request("save_config", config=config)
    errors = []
    for kind in restart if result.get("ok") else ():
        started = request(f"{kind}_start")
        if not started.get("ok"):
            errors.append(started.get("error", f"{kind} did not start"))
    return dict(result, restarted=restart, restart_errors=errors)


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
