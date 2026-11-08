"""Read-only Windows checks. Never opens an audio stream or captures a frame."""

import importlib.util
import platform
import subprocess
import sys
from ..host import app_dir, child_options, grouped, tool


def report():
    from ..doctor import _voice_engine
    from ..process_memory import service_snapshot
    from ..native_worker import vosk_library

    tools = {}
    for name in ("ffmpeg", "ffprobe", "jarvis-replay-store", "jarvis-voice"):
        if name == "jarvis-replay-store":
            from ..replay_store import binary as replay_binary
            try:
                binary = replay_binary()
            except RuntimeError as exc:
                tools[name] = {"path": None, "ok": False, "error": str(exc)}
                continue
        elif name == "jarvis-voice":
            from ..native_worker import binary_path
            binary = binary_path()
            binary = str(binary) if binary else None
        else:
            binary = tool(name)
        try:
            result = (
                subprocess.run(
                    [str(grouped(binary)), "--version" if name.startswith("jarvis-") else "-version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    **child_options(),
                )
                if binary
                else None
            )
            tools[name] = {
                "path": binary,
                "ok": bool(result and result.returncode == 0),
                "version": result.stdout.splitlines()[:1] if result else [],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            tools[name] = {"path": binary, "ok": False, "error": str(exc)}
    return {
        "system": platform.platform(),
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "windows_build": sys.getwindowsversion().build,
        "app_dir": str(app_dir()),
        "frozen": bool(getattr(sys, "frozen", False)),
        "voice_engine": _voice_engine(),
        "memory": service_snapshot(),
        "tools": tools,
        "dependencies": {
            name: importlib.util.find_spec(name) is not None
            for name in (
                "win32api",
                "pyaudiowpatch",
                "pycaw",
                "pyvda",
                "tkinter",
            )
        },
        "vosk_library": str(vosk_library() or "") or None,
        "note": "Presence checks only. Test microphone, desktop control, GPU encoding and HDR on Windows hardware before publishing.",
    }
