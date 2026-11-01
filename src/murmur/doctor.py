"""Read-only diagnostics; never opens the microphone or starts a capture."""
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def _voice_engine() -> dict:
    """Which recognizer the next voice session will use, and whether its model is ready."""
    try:
        from . import config
        from .models import describe
        return describe(config.load())
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}


def report():
    from .host import is_windows
    if is_windows():
        from .windows.diagnostics import report as windows_report
        return windows_report()
    from .process_memory import service_snapshot
    commands = {}
    # Every external program Murmur actually runs, not just the main ones.
    for name, args in {"hyprctl": ["version"], "pw-cli": ["--version"], "pw-record": ["--version"],
                       "pw-play": ["--version"], "pw-dump": ["--version"], "pactl": ["--version"],
                       "wf-recorder": ["--version"], "gpu-screen-recorder": ["--version"], "wpctl": ["status"],
                       "ffprobe": ["-version"], "gio": ["version"], "setpriv": ["--version"],
                       "systemd-run": ["--version"]}.items():
        path = shutil.which(name)
        try:
            r = subprocess.run([path, *args], capture_output=True, text=True, timeout=3) if path else None
            commands[name] = {"path": path, "ok": r is not None and r.returncode == 0,
                              "version": (r.stdout or r.stderr).splitlines()[:1] if r else []}
        except (OSError, subprocess.TimeoutExpired) as exc:
            commands[name] = {"path": path, "ok": False, "error": str(exc)}
    in_venv = sys.prefix != sys.base_prefix
    return {"python": platform.python_version(), "system": platform.platform(),
            "interpreter": sys.executable, "virtualenv": in_venv,
            # After a distro Python upgrade (e.g. 3.14 -> 3.15) a venv keeps pointing at
            # /usr/bin/python3 but its packages sit in the old lib/python3.X directory.
            "venv_matches_python": (not in_venv) or (Path(sys.prefix) / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}").is_dir(),
            "hyprland_session": bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")),
            "wayland_session": bool(os.environ.get("WAYLAND_DISPLAY")),
            "pocketsphinx": importlib.util.find_spec("pocketsphinx") is not None,
            "voice_engine": _voice_engine(),
            "gtk_python": importlib.util.find_spec("gi") is not None, "tools": commands,
            "memory": service_snapshot(),
            "note": "Dependency presence is not microphone, capture permission, or recognition verification."}
