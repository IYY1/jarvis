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


GROUP_NAME = "JarvisConsole.exe"


def grouped(path):
    """Windows release: ``path`` under the service's own file name, so Task Manager
    lists it inside Jarvis's row. Task Manager groups a process with its parent only
    when both run a file of the same name (tested on Windows 11; descriptions and
    AppUserModelIDs don't count). The name is a hard link in the data folder: the
    same file, no extra space, remade whenever the program's file is replaced, and
    used only while it is still that very file. Anything else: ``path`` itself."""
    if path is None or not is_windows() or not getattr(sys, "frozen", False):
        return path
    path = Path(path)
    if path.name.lower() == GROUP_NAME.lower():
        return path
    link = data_dir() / "bin" / path.stem / GROUP_NAME
    try:
        if link.is_file() and os.path.samefile(link, path):
            return link
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        os.link(path, link)
        return link if os.path.samefile(link, path) else path
    except OSError:
        return path


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
