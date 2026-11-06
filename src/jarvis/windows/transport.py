"""Authenticated loopback transport. Endpoint secret lives in a user-only ACL.

Windows asyncio cannot serve Unix sockets. A random per-run token protects the
local TCP endpoint; requests are bounded and compared in constant time. No LAN
interface is bound, and clients never accept an endpoint-supplied hostname.
"""

import hmac
import json
import os
from pathlib import Path
import secrets
import socket
from ..host import runtime_dir


def endpoint_path():
    return Path(os.environ.get("JARVIS_ENDPOINT", str(runtime_dir() / "control.json")))


def publish(port, token):
    from .system import private_directory

    path = endpoint_path()
    private_directory(path.parent)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def connect(timeout):
    path = endpoint_path()
    with path.open("rb") as f:
        raw = f.read(1025)
    if len(raw) > 1024:
        raise ConnectionError("Invalid Jarvis endpoint")
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or type(data.get("port")) is not int
        or not 1 <= data["port"] <= 65535
        or not isinstance(data.get("token"), str)
        or len(data["token"]) != 64
    ):
        raise ConnectionError("Invalid Jarvis endpoint")
    return socket.create_connection(("127.0.0.1", data["port"]), timeout), data["token"]


def authenticate(payload, token):
    auth = payload.pop("auth", None)
    return isinstance(auth, str) and auth.isascii() and hmac.compare_digest(auth, token)


def new_token():
    return secrets.token_hex(32)
