"""Apps the user adds by hand (AppImages, portable programs, scripts).

An added app becomes an ordinary launcher entry, so Jarvis discovers and
launches it exactly like installed apps, and the desktop's own app menu shows
it too: a desktop entry in ~/.local/share/applications on Linux, a Start menu
shortcut on Windows. Jarvis only removes entries it created. Speech still only
selects catalog entries; it never runs a path.
"""
from __future__ import annotations

import os
from pathlib import Path
import re

from .host import is_windows

MARK = "X-Jarvis-Added"
APPIMAGE_FOLDERS = ("Applications", "AppImages", ".local/bin", "Downloads", "Desktop")


def _applications() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "applications"


def _start_menu() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "Microsoft/Windows/Start Menu/Programs/Jarvis"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:48] or "app"


def _check_name(name: str) -> str:
    name = " ".join(name.split())
    if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise ValueError("Give the app a short name (up to 80 characters)")
    return name


def exec_value(path: Path) -> str:
    """A desktop-entry Exec value that runs exactly this file, with no arguments.

    The spec's quoting (\\" \\` \\$ \\\\ inside double quotes, %% for %) is
    applied first, then the general string escaping of the file format."""
    quoted = '"' + re.sub(r'(["`$\\\\])', r"\\\1", str(path)).replace("%", "%%") + '"'
    return quoted.replace("\\", "\\\\")


def add(path: str | Path, name: str) -> str:
    """Add a launcher for an executable file; returns its catalog ID."""
    path = Path(path).expanduser().resolve()
    name = _check_name(name)
    if not path.is_file():
        raise ValueError(f"{path} is not a file")
    if is_windows():
        if path.suffix.lower() not in (".exe", ".bat", ".cmd", ".lnk"):
            raise ValueError("Choose a program (.exe) to add")
        folder = _start_menu()
        folder.mkdir(parents=True, exist_ok=True)
        link = folder / f"{_slug(name)}.lnk"
        if link.exists():
            raise ValueError(f"An app named {name} was already added")
        import win32com.client
        shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(link))
        shortcut.TargetPath = str(path)
        shortcut.WorkingDirectory = str(path.parent)
        shortcut.Description = f"Added by Jarvis ({name})"
        shortcut.Save()
        return str(link)
    if not os.access(path, os.X_OK):
        raise ValueError(f"{path.name} is not executable. Make it executable first (for example chmod +x); "
                         "Jarvis does not change file permissions.")
    folder = _applications()
    folder.mkdir(parents=True, exist_ok=True)
    entry = folder / f"jarvis-added-{_slug(name)}.desktop"
    if entry.exists():
        raise ValueError(f"An app named {name} was already added")
    entry.write_text("[Desktop Entry]\nType=Application\n"
                     f"Name={name}\nExec={exec_value(path)}\n"
                     f"Path={str(path.parent)}\nComment=Added by Jarvis\n{MARK}=true\n", encoding="utf-8")
    return entry.name


def added() -> list[dict]:
    """Apps Jarvis added: {"id", "name", "entry"}."""
    if is_windows():
        folder = _start_menu()
        return [{"id": str(p), "name": p.stem, "entry": str(p)} for p in sorted(folder.glob("*.lnk"))] \
            if folder.is_dir() else []
    result = []
    for entry in sorted(_applications().glob("jarvis-added-*.desktop")):
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        if f"\n{MARK}=true" in text:
            name = next((line[5:] for line in text.splitlines() if line.startswith("Name=")), entry.stem)
            result.append({"id": entry.name, "name": name, "entry": str(entry)})
    return result


def remove(identity: str) -> None:
    """Remove an app Jarvis added; installed apps are never touched."""
    match = next((a for a in added() if a["id"] == identity), None)
    if match is None:
        raise ValueError("Only apps added in Jarvis can be removed here")
    Path(match["entry"]).unlink()


def appimage_candidates(known_exec: str = "") -> list[Path]:
    """AppImages in the usual folders that no launcher entry runs yet."""
    if is_windows():
        return []
    found = []
    for folder in APPIMAGE_FOLDERS:
        root = Path.home() / folder
        try:
            files = sorted(p for p in root.iterdir() if p.suffix.lower() == ".appimage" and p.is_file())
        except OSError:
            continue
        found += [p for p in files if str(p) not in known_exec and p.name not in known_exec]
    return found[:20]
