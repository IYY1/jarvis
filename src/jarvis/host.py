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
ENCODER_NAME = "Jarvis video encoder"   # FFmpeg's name in Task Manager (see grouped)


def grouped(path, description=None):
    """Windows release: ``path`` under the service's own file name, so Task Manager
    lists it inside Jarvis's row. Task Manager groups a process with its parent only
    when both run a file of the same name (tested on Windows 11; descriptions and
    AppUserModelIDs don't count). The name is a hard link in the data folder: the
    same file, no extra space, remade whenever the program's file is replaced, and
    used only while it is still that very file. Anything else: ``path`` itself.

    A program without a description of its own (FFmpeg) would show in that row as
    "JarvisConsole": with ``description`` it gets a copy that carries one instead,
    remade whenever the program changes."""
    if path is None or not is_windows() or not getattr(sys, "frozen", False):
        return path
    path = Path(path)
    # The settings window runs Jarvis.exe: what it starts must carry that name to
    # sit in the window's row. Everything else is under the service's name.
    name = Path(sys.executable).name
    if name.lower() not in ("jarvis.exe", GROUP_NAME.lower()):
        name = GROUP_NAME
    if path.name.lower() == name.lower():
        return path
    link = data_dir() / "bin" / path.stem / GROUP_NAME
    try:
        target = _described_copy(path, link, description) if description else _same_file(path, link)
        if name.lower() == GROUP_NAME.lower() or target == path:
            return target
        return _same_file(target, link.with_name(name))
    except Exception:   # a name is only nicer to read: never a reason to fail
        return path


def _same_file(path, link):
    """``link``, a hard link to ``path`` (the same file under another name), or
    ``path`` itself when one can't be made."""
    if link.is_file() and os.path.samefile(link, path):
        return link
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    os.link(path, link)
    return link if os.path.samefile(link, path) else path


def _described_copy(path, copy, description):
    import json

    source = path.stat()
    record = copy.with_name("source.json")
    wanted = {"size": source.st_size, "modified": source.st_mtime_ns, "description": description}
    try:
        if copy.is_file() and json.loads(record.read_text(encoding="utf-8")) == {**wanted, "copy": copy.stat().st_size}:
            return copy
    except (OSError, ValueError):
        pass
    copy.parent.mkdir(parents=True, exist_ok=True)
    # Written beside and swapped in, never in place: an older copy here may be a
    # hard link to the program itself, which must not be changed.
    staging = copy.with_name(f"{os.getpid()}.part")
    try:
        shutil.copyfile(path, staging)
        _set_description(staging, description)
        os.replace(staging, copy)
    finally:
        staging.unlink(missing_ok=True)
    record.write_text(json.dumps({**wanted, "copy": copy.stat().st_size}), encoding="utf-8")
    return copy


def version_resource(description, version="0.0.0"):
    """A VS_VERSIONINFO block, as a program's RT_VERSION resource carries it."""
    import struct

    def node(key, value=b"", value_length=0, kind=1, children=b""):
        data = struct.pack("<HHH", 0, value_length, kind) + (key + "\0").encode("utf-16-le")
        data += b"\0" * (-len(data) % 4) + value
        data += b"\0" * (-len(data) % 4) + children
        return struct.pack("<H", len(data)) + data[2:]

    def padded(block):
        return block + b"\0" * (-len(block) % 4)

    def text(key, value):
        return padded(node(key, (value + "\0").encode("utf-16-le"), len(value) + 1))

    numbers = ([int(part) for part in version.split(".") if part.isdigit()] + [0, 0, 0, 0])[:4]
    major, minor = numbers[0] << 16 | numbers[1], numbers[2] << 16 | numbers[3]
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x00010000, major, minor, major, minor, 0x3F, 0, 0x40004, 1, 0, 0, 0)
    strings = b"".join(text(k, v) for k, v in (("CompanyName", "Jarvis"), ("FileDescription", description),
                                                  ("FileVersion", version), ("ProductName", "Jarvis"),
                                                  ("ProductVersion", version)))
    table = padded(node("StringFileInfo", children=padded(node("040904B0", children=strings))))
    translation = padded(node("VarFileInfo", children=padded(node("Translation", struct.pack("<HH", 0x0409, 0x04B0), 4, 0))))
    return node("VS_VERSION_INFO", fixed, len(fixed), 0, table + translation)


def _set_description(program, description):
    import win32api

    from . import __version__
    handle = win32api.BeginUpdateResource(str(program), False)
    try:
        win32api.UpdateResource(handle, 16, 1, version_resource(description, __version__), 1033)   # RT_VERSION
    except Exception:
        win32api.EndUpdateResource(handle, True)
        raise
    win32api.EndUpdateResource(handle, False)


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
