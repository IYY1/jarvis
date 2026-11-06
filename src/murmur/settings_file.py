"""Export settings to a file and import them back, on this or another computer.

An import goes through the same repair as loading: settings another version
wrote keep every value that is still valid. The current settings are copied
to backups/ first, and a running service applies the import at once
(restarting a running capture whose settings changed)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from . import config


def current() -> dict:
    """The settings in use: the running service's, else the file's."""
    from .client import request
    try:
        reply = request("config", timeout=3)
        if reply.get("ok"):
            return reply["config"]
    except (OSError, ConnectionError, ValueError):
        pass
    return config.load()


def export_settings(path: str | Path) -> Path:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # settings can name private paths
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(current(), f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def read_settings(path: str | Path) -> tuple[dict, list[str]]:
    """Validated settings from a file, and the names reset because this
    version rejects their values."""
    path = Path(path).expanduser()
    if path.stat().st_size > 65536:
        raise ValueError("That file is too large to be Jarvis settings")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("That file does not contain Jarvis settings")
    data = config.without_unknown(data)
    try:
        return config.validate(data), []
    except (ValueError, TypeError, KeyError, AttributeError):
        return config.repair(data)


def import_settings(path: str | Path) -> str:
    """Apply settings from a file; returns what happened, in a sentence."""
    from .client import request, restart_note, save_config_restarting
    settings, reset = read_settings(path)
    kept = config.backup("before-import-" + time.strftime("%Y-%m-%d_%H-%M-%S"))
    try:
        running = request("status", timeout=3).get("ok")
    except (OSError, ConnectionError, ValueError):
        running = False
    note = ""
    if running:
        result = save_config_restarting(settings)
        if not result.get("ok"):
            raise ValueError(result.get("error", "The service did not accept these settings"))
        note = restart_note(result)
    else:
        config.save(settings)
    message = f"Imported settings from {Path(path).name}."
    if reset:
        message += f" Not valid in this version, so left at their defaults: {', '.join(reset)}."
    if kept:
        message += f" Your previous settings are kept at {kept}."
    return message + note
