"""XDG desktop entry discovery; launches through GIO's desktop launcher."""
from __future__ import annotations

import configparser
from dataclasses import dataclass, asdict
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from .commands import CommandError, normalize, spoken_numbers
from .desktop_process import desktop_argv

# Programs that start *another* program. Their name says nothing about the
# window that appears (e.g. every Steam game's Exec is "steam steam://rungameid/N",
# and the Steam client's own window class is also "steam").
LAUNCHER_WRAPPERS = frozenset({
    "env", "sh", "bash", "dash", "zsh", "fish", "nice", "ionice", "taskset", "chrt", "setsid",
    "flatpak", "snap", "steam", "lutris", "heroic", "bottles-cli", "itch", "legendary",
    "wine", "wine64", "proton", "umu-run", "gamemoderun", "game-performance", "prime-run",
    "mangohud", "gamescope", "xdg-open", "gio", "gtk-launch", "kioclient", "kioclient5",
    "kde-open", "kde-open5", "dbus-launch", "dbus-run-session", "systemd-run", "uwsm",
    "uwsm-app", "app2unit", "pkexec", "sudo", "java", "python", "python3", "electron",
})
# Browser "installed web apps" share the browser binary but have their own window class.
WEB_APP_FLAGS = ("--app-id=", "--app=")
STEAM_GAME = re.compile(r"steam://rungameid/(\d+)")
# Reverse-DNS desktop IDs such as org.mozilla.firefox; only these are split on dots
# (a Steam entry like "F.E.A.R. 2 Project Origin" must not become "2 project origin").
REVERSE_DNS = re.compile(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){2,}")
def id_stem(desktop_id: str) -> str:
    stem = desktop_id.removesuffix(".desktop")
    return stem.split(".")[-1] if REVERSE_DNS.fullmatch(stem) else stem


def is_flatpak(app: "App") -> bool:
    return "/flatpak/" in app.path or app.exec.split(" ", 1)[0].endswith("flatpak")


@dataclass
class App:
    id: str
    name: str
    path: str
    exec: str
    icon: str = ""
    wm_class: str = ""

    def classes(self) -> set[str]:
        """Window classes that identify an already-running instance of this app."""
        stem = self.id.removesuffix(".desktop")
        names = [self.wm_class, stem, id_stem(self.id)]
        try:
            argv = shlex.split(self.exec)
        except ValueError:
            argv = []
        if argv:
            program = Path(argv[0]).name
            if program not in LAUNCHER_WRAPPERS and not any(a.startswith(WEB_APP_FLAGS) for a in argv[1:]):
                names.append(program)
            for argument in argv[1:]:
                if m := STEAM_GAME.match(argument):
                    names.append(f"steam_app_{m[1]}")  # Proton/Steam game window class
        return {n.casefold() for n in names if n}


def data_dirs() -> list[Path]:
    roots = [os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))]
    roots.extend(os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"))
    # Flatpak exports may not yet be in a service's environment.
    roots.extend([str(Path.home() / ".local/share/flatpak/exports/share"), "/var/lib/flatpak/exports/share"])
    return list(dict.fromkeys(Path(p) / "applications" for p in roots if p))


def current_desktops() -> set[str]:
    return {d.casefold() for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if d}


def _shown_here(entry, desktops: set[str]) -> bool:
    """Honor OnlyShowIn/NotShowIn so GNOME/KDE-only entries don't create ambiguity."""
    def values(key):
        return {v.casefold() for v in entry.get(key, "").split(";") if v}
    only, never = values("OnlyShowIn"), values("NotShowIn")
    if only and not only & desktops:
        return False
    return not never & desktops


class AppRegistry:
    def __init__(self, aliases: dict | None = None, roots: list[Path] | None = None):
        self.aliases = aliases or {}
        self.roots = roots
        self.apps: dict[str, App] = {}
        self.index: dict[str, list[App]] = {}
        self._scan()
        self._register_fallbacks()

    def rescan(self) -> bool:
        """Re-read desktop entries (e.g. after installing an app). True if names changed."""
        before = (set(self.index), set(self.apps))
        self._scan()
        self._register_fallbacks()
        return (set(self.index), set(self.apps)) != before

    def _scan(self) -> None:
        apps: dict[str, App] = {}
        index: dict[str, list[App]] = {}
        desktops = current_desktops()
        seen = set()
        for root in self.roots if self.roots is not None else data_dirs():
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.desktop")):
                desktop_id = str(path.relative_to(root)).replace("/", "-")
                if desktop_id in seen:
                    continue
                seen.add(desktop_id)  # Hidden user entry masks a system entry.
                try:
                    p = configparser.ConfigParser(interpolation=None, strict=False)
                    p.read(path, encoding="utf-8")
                    e = p["Desktop Entry"]
                    if e.get("Type") != "Application" or e.getboolean("Hidden", False) or e.getboolean("NoDisplay", False):
                        continue
                    if not _shown_here(e, desktops):
                        continue
                    if e.get("TryExec") and not shutil.which(e["TryExec"]):
                        continue
                    if not e.get("Name") or not (e.get("Exec") or e.getboolean("DBusActivatable", False)):
                        continue
                    a = App(desktop_id, e["Name"], str(path.resolve()), e.get("Exec", ""), e.get("Icon", ""), e.get("StartupWMClass", ""))
                except (OSError, configparser.Error, ValueError, KeyError):
                    continue
                apps[desktop_id] = a
                # Display name and desktop-ID stem often differ only in case.
                # Deduplicate AFTER normalization, not before (Firefox/firefox).
                indexed_names = set()
                for name in {a.name, id_stem(a.id)}:
                    try:
                        normalized = normalize(name)
                    except ValueError:
                        continue
                    for variant in {normalized, spoken_numbers(normalized), normalized.removeprefix("the "), spoken_numbers(normalized.removeprefix("the "))}:
                        if variant and variant not in indexed_names:
                            index.setdefault(variant, []).append(a)
                            indexed_names.add(variant)
        for name, target in self.aliases.items():
            if target in apps:
                index[normalize(name)] = [apps[target]]
        self.apps, self.index = apps, index

    def _register_fallbacks(self):
        # Reserve unambiguous common-word names without adding unused names to
        # the speech grammar. Only apps needing a fallback use it in commands.
        from .commands import NUMBER_WORDS
        used = set(self.index) | {normalize(name) for name in self.aliases}
        self._fallbacks = {}
        counter = 1
        for identity in sorted(self.apps):
            while True:
                name = 'application ' + ' '.join(NUMBER_WORDS[int(d)] for d in str(counter))
                counter += 1
                if name not in used:
                    break
            used.add(name)
            self._fallbacks[name] = identity

    def voice_commands(self, parser):
        names_by_app = None

        def candidates(app):
            nonlocal names_by_app
            # Most apps resolve by display name. Build the reverse index only
            # when an ambiguous or reserved name needs an alternative.
            yield self._display(app)
            if names_by_app is None:
                names_by_app = {}
                for name in sorted(self.index):
                    for identity in {entry.id for entry in self.index[name]}:
                        names_by_app.setdefault(identity, []).append(name)
                for name, identity in self._fallbacks.items():
                    names_by_app.setdefault(identity, []).append(name)
            yield from names_by_app.pop(app.id, ())

        result = []
        for app in sorted(self.apps.values(), key=lambda a: (a.name.casefold(), a.id)):
            seen = set()
            for name in candidates(app):
                if not name or name in seen:
                    continue
                seen.add(name)
                try:
                    intents = [parser.parse(f'{verb} {name}') for verb in ('open', 'close')]
                    if any(intent.action != action or self.resolve(intent.target).id != app.id
                           for intent, action in zip(intents, ('open_app', 'close_app'))):
                        continue
                except (ValueError, KeyError):
                    continue
                result.append({'category': 'Applications', 'title': app.name, 'app_id': app.id,
                               'voice_name': name, 'examples': [f'open {name}', f'close {name}']})
                break
            else:
                raise CommandError(f'Cannot assign open/close commands to {app.name}')
            if names_by_app is not None:
                names_by_app.pop(app.id, None)
        return result

    def resolve(self, phrase: str) -> App:
        name = normalize(phrase)
        if name in getattr(self, '_fallbacks', {}):
            return self.apps[self._fallbacks[name]]
        for alias, target in self.aliases.items():
            if normalize(alias) == name:
                if target not in self.apps:
                    raise CommandError(f"Alias points to a missing application: {target}")
                break
        matches = self.index.get(name, [])
        if not matches:
            raise CommandError("Application not found. Add an alias in Applications.")
        if len(matches) > 1:
            matches = self._prefer(name, matches)
        if len(matches) > 1:
            raise CommandError("Ambiguous application; add an alias for: " + ", ".join(a.id for a in matches))
        return matches[0]

    @staticmethod
    def _display(app: App) -> str:
        try:
            return spoken_numbers(normalize(app.name))
        except ValueError:
            return ""

    def _prefer(self, name: str, matches: list[App]) -> list[App]:
        """Pick one entry when several match the spoken name.

        1. An entry whose *display name* is what was said beats one that only
           matched through its desktop-ID (e.g. "Steam" vs some com.x.steam ID).
        2. If the rest share one display name *and* identify the same windows
           (overlapping window classes), they are one app installed more than
           once, typically native + Flatpak (firefox.desktop and
           org.mozilla.firefox.desktop). Prefer the native entry, then a user
           entry over a system one, then the shortest ID.
        Different apps that merely share a name (two unrelated "Settings")
        stay ambiguous until an alias picks one.
        """
        exact = [a for a in matches if self._display(a) == name] or matches
        if len({self._display(a) for a in exact}) != 1:
            return exact
        first = exact[0].classes()
        if not all(first & other.classes() for other in exact[1:]):
            return exact
        home = str(Path.home())
        return [min(exact, key=lambda a: (is_flatpak(a), not a.path.startswith(home), len(a.id), a.id))]

    def duplicates(self, app: App) -> list[App]:
        """Other installed entries with the same display name (e.g. a Flatpak copy)."""
        name = self._display(app)
        return [a for a in self.apps.values() if a.id != app.id and name and self._display(a) == name]

    def launch(self, app: App):
        launcher = shutil.which("gio")
        if not launcher:
            raise CommandError("GIO desktop launcher is missing (install glib2)")
        completed = subprocess.run(desktop_argv([launcher, "launch", app.path]), capture_output=True, text=True, timeout=8, check=False)
        if completed.returncode:
            raise CommandError("Desktop launch failed: " + completed.stderr.strip()[:300])

    def list(self) -> list[dict]:
        return [{**asdict(a), "aliases": [k for k, v in self.aliases.items() if v == a.id], "speakable": None}
                for a in sorted(self.apps.values(), key=lambda a: a.name.casefold())]
