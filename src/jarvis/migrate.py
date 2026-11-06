"""One-time move from the old name, Murmur, to Jarvis on Linux (mirrors
config::migrate_from_old_name in the Rust controller).

Settings, models and the videos folder are moved (a rename, so nothing is
copied), settings that name the old folders are updated, and the launcher
and user service the installer wrote are replaced by Jarvis ones. Nothing is
touched when the Jarvis location already exists."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from .host import is_windows

OLD = "mur" + "mur"   # spelled apart so a rename of the code leaves it alone


def _homes() -> tuple[Path, Path, Path]:
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    data = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share")))
    return home, config, data


def _move(old: Path, new: Path) -> bool:
    if old.is_dir() and not old.is_symlink() and not new.exists():
        try:
            old.rename(new)
            return True
        except OSError:
            return False
    return False


def _update_settings(config_file: Path, moves: list[tuple[Path, Path]]) -> None:
    """Point settings at the moved folders (recording.output_dir, model and buffer paths)."""
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    home = str(Path.home())
    changed = False
    for section, key in (("recording", "output_dir"), ("recognition", "model_path"), ("replay", "buffer_dir")):
        value = data.get(section, {}).get(key)
        if not isinstance(value, str) or not value:
            continue
        expanded = value.replace("~", home, 1) if value.startswith("~") else value
        for old, new in moves:
            if expanded == str(old) or expanded.startswith(str(old) + "/"):
                moved = str(new) + expanded[len(str(old)):]
                data[section][key] = moved.replace(home, "~", 1) if value.startswith("~") else moved
                changed = True
    if changed:
        temporary = config_file.with_name(".config-migrate.json")
        temporary.write_text(json.dumps(data) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(config_file)


def _replace_file(old: Path, new: Path) -> bool:
    if not old.is_file() or new.exists():
        return False
    text = old.read_text(encoding="utf-8")
    new.write_text(text.replace(OLD, "jarvis").replace(OLD.title(), "Jarvis").replace(OLD.upper(), "JARVIS"),
                   encoding="utf-8")
    old.unlink()
    return True


def from_old_name() -> list[str]:
    """Move what the old name left behind; returns what was moved, for a note."""
    if is_windows():
        return []   # Windows releases always used Jarvis folders
    home, config_home, data_home = _homes()
    done, moves = [], []
    for old, new in ((config_home / OLD, config_home / "jarvis"), (data_home / OLD, data_home / "jarvis")):
        if _move(old, new):
            moves.append((old, new))
            done.append(f"{old} → {new}")
    settings = config_home / "jarvis" / "config.json"
    try:
        output = json.loads(settings.read_text(encoding="utf-8")).get("recording", {}).get("output_dir", "")
    except (OSError, ValueError):
        output = ""
    videos_old = Path(str(output).replace("~", str(home), 1)) if str(output).startswith("~") else Path(str(output))
    if videos_old.name == OLD.title() and _move(videos_old, videos_old.with_name("Jarvis")):
        moves.append((videos_old, videos_old.with_name("Jarvis")))
        done.append(f"{videos_old} → {videos_old.with_name('Jarvis')}")
    if moves and settings.is_file():
        _update_settings(settings, moves)
    applications = data_home / "applications"
    if _replace_file(applications / f"org.{OLD}.Controller.desktop", applications / "org.jarvis.Controller.desktop"):
        done.append("launcher renamed")
    units = config_home / "systemd/user"
    old_unit, new_unit = units / f"{OLD}.service", units / "jarvis.service"
    if old_unit.is_file() and not new_unit.exists():
        enabled = subprocess.run(["systemctl", "--user", "is-enabled", f"{OLD}.service"],
                                 capture_output=True, text=True).stdout.strip() == "enabled"
        if enabled:
            subprocess.run(["systemctl", "--user", "disable", f"{OLD}.service"], capture_output=True)
        _replace_file(old_unit, new_unit)
        done.append("service renamed to jarvis.service")
        enabled_changed = enabled
    else:
        enabled_changed = None
    # Drop-in overrides (for example the Rust controller's ExecStart) follow the unit.
    old_dropins, new_dropins = units / f"{OLD}.service.d", units / "jarvis.service.d"
    moved_dropins = old_dropins.is_dir() and not new_dropins.exists()
    if moved_dropins:
        new_dropins.mkdir(parents=True)
        for item in sorted(old_dropins.iterdir()):
            if item.is_file():
                _replace_file(item, new_dropins / item.name)
        try:
            old_dropins.rmdir()
        except OSError:
            pass
        done.append("service overrides moved to jarvis.service.d")
    if enabled_changed is not None or moved_dropins:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    if enabled_changed:
        subprocess.run(["systemctl", "--user", "enable", "jarvis.service"], capture_output=True)
    return done
