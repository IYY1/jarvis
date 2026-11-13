"""Which game is focused, so its clips and recordings get their own folder
(mirrors desktop::focused_game in the Rust controller).

A window counts as a game when it is a Steam game (a steam_app_<id> window,
named from Steam's manifest, or a program under steamapps/common), a Heroic
or Lutris game under ~/Games, or a Wine/Proton .exe or gamescope window,
named by its title. On Windows: a program under steamapps\\common, Epic Games
or GOG Galaxy\\Games. Anything else is not a game."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from .host import is_windows

MARKERS = ("/steamapps/common/", "/Games/Heroic/", "/Games/", "/Epic Games/", "/GOG Galaxy/Games/")


# Store programs installed beside the games that are not games (mirrors NOT_GAMES in
# the voice worker's app_audio.rs): Epic's launcher and overlay, Steam tools.
NOT_GAMES = ("/epic games/launcher/", "/epic games/epic online services/", "/steamapps/common/wallpaper_engine/",
             "/steamapps/common/steamworks shared/", "/steamapps/common/steamvr/")


def game_from_paths(text: str) -> str | None:
    text = text.replace("\\", "/")
    if any(marker in text.lower() for marker in NOT_GAMES):
        return None
    for marker in MARKERS:
        at = text.find(marker)
        if at >= 0:
            rest = text[at + len(marker):]
            name = rest.split("/")[0].strip()
            if name and "/" in rest:
                return name
    return None


def game_folder(name: str) -> str | None:
    """A safe folder name, or None when nothing usable is left."""
    clean = "".join(c for c in name if c.isprintable() and c not in '/\\:*?"<>|').strip().strip(".").strip()
    clean = clean[:80].strip()
    return clean or None


def vdf_values(text: str, key: str) -> list[str]:
    values = []
    for line in text.splitlines():
        parts = [p for p in line.split('"') if p.strip()]
        if len(parts) >= 2 and parts[0] == key:
            values.append(parts[1])
    return values


def steam_name(app_id: str) -> str | None:
    if not app_id.isdigit():
        return None
    home = Path.home()
    libraries = [home / p / "steamapps" for p in (".local/share/Steam", ".steam/steam",
                                                    ".var/app/com.valvesoftware.Steam/data/Steam")]
    for library in list(libraries):
        try:
            libraries += [Path(p) / "steamapps" for p in vdf_values((library / "libraryfolders.vdf").read_text(), "path")]
        except OSError:
            pass
    for library in libraries:
        try:
            names = vdf_values((library / f"appmanifest_{app_id}.acf").read_text(), "name")
        except OSError:
            continue
        if names:
            return names[0]
    return None


def _windows_focused() -> str | None:
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    process = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return game_from_paths(buffer.value)
    finally:
        kernel32.CloseHandle(process)
    return None


def focused_game() -> str | None:
    """The focused game's folder name, or None."""
    try:
        if is_windows():
            name = _windows_focused()
            return game_folder(name) if name else None
        window = json.loads(subprocess.run(["hyprctl", "-j", "activewindow"], capture_output=True,
                                           text=True, timeout=2).stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    cls, title = str(window.get("class", "")), str(window.get("title", ""))
    if cls.startswith("steam_app_"):
        name = steam_name(cls.removeprefix("steam_app_"))
        if name and game_folder(name):
            return game_folder(name)
    pid = window.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        exe = str(Path(f"/proc/{pid}/exe").readlink())
    except OSError:
        exe = ""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").replace("\0", " ")
    except OSError:
        cmdline = ""
    name = game_from_paths(f"{exe} {cmdline}")
    if name is None and (cls.startswith("steam_app_") or cls == "gamescope" or ".exe" in cmdline.lower()):
        name = title
    return game_folder(name) if name else None


def into_game_folder(path: Path) -> Path:
    """Move a saved clip into its game's folder beside it; the path it ends at."""
    game = focused_game()
    if not game:
        return path
    folder = path.parent / game
    try:
        folder.mkdir(exist_ok=True, mode=0o700)
        target = folder / path.name
        path.rename(target)
        return target
    except OSError:
        return path
