"""Host paths and launch rules shared by the service, workers and both UIs.

Importing this module never probes hardware or imports a desktop/audio library.
"""

from __future__ import annotations
import os
from pathlib import Path
import shutil
import subprocess
import sys


def is_windows():
    return sys.platform == "win32"


def app_dir():
    return (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2]
    )


def data_dir():
    if is_windows():
        return (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
            / "Jarvis"
        )
    return (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        / "jarvis"
    )


def settings_dir():
    if is_windows():
        return data_dir()
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "jarvis"
    )


def runtime_dir():
    if is_windows():
        return data_dir() / "run"
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) / "jarvis" if base else Path(f"/tmp/jarvis-{os.getuid()}")


def tool(name):
    if is_windows():
        bundled = app_dir() / "tools" / (name + ".exe")
        if bundled.is_file():
            return str(bundled)
    return shutil.which(name)


def command(mode, *args):
    """Use the console worker in frozen Windows builds; never spawn the GUI recursively."""
    if getattr(sys, "frozen", False):
        binary = app_dir() / "JarvisConsole.exe"
        if not binary.is_file():
            raise RuntimeError(
                "JarvisConsole.exe is missing; extract/install the complete Jarvis folder"
            )
        return [str(binary), "--internal", mode, *map(str, args)]
    return [sys.executable, "-m", "jarvis", "--internal", mode, *map(str, args)]


def child_options(*, independent=False):
    if not is_windows():
        return {"start_new_session": True} if independent else {}
    flags = subprocess.CREATE_NO_WINDOW
    if independent:
        flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    return {"creationflags": flags}


def sync_directory(path):
    if not is_windows():
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
