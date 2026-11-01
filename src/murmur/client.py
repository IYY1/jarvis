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


def watch(callback, stop_event):
    with connection(None, {"op": "watch"}) as sock:
        with sock.makefile("rb") as f:
            while not stop_event.is_set():
                line = f.readline(65537)
                if not line or len(line) > 65536:
                    break
                callback(json.loads(line))
