"""Small desktop adapter. Only validated, allowlisted hyprctl dispatches."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .commands import CommandError


class Hyprland:
    def query(self, name: str):
        if name not in ("monitors", "clients", "activewindow", "activeworkspace", "version"):
            raise CommandError("Unsupported desktop query")
        try:
            r = subprocess.run(["hyprctl", "-j", name], capture_output=True, text=True, timeout=2)
            if r.returncode:
                raise CommandError("Hyprland unavailable: " + r.stderr.strip()[:200])
            return json.loads(r.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            raise CommandError("Cannot reach Hyprland. Start Murmur inside the graphical session.") from e

    # Hyprland 0.55+ can be configured in Lua; then `hyprctl dispatch` takes a
    # Lua dispatcher expression (hl.dsp.*) instead of the old "name args" form.
    # Which one works is learned on first use and remembered for the session.
    syntax: str | None = None

    @staticmethod
    def _uses_lua_config() -> bool:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        return (base / "hypr" / "hyprland.lua").is_file()

    @staticmethod
    def _commands(action: str, target) -> dict[str, list[str]]:
        """The same validated action in both syntaxes. Targets are checked
        before formatting, so nothing user-controlled reaches the Lua text."""
        if action in ("workspace", "move_workspace"):
            if type(target) is not int or not 1 <= target <= 10:
                raise CommandError("Invalid workspace")
            if action == "workspace":
                return {"legacy": ["workspace", str(target)],
                        "lua": [f'hl.dsp.focus({{ workspace = "{target}" }})']}
            return {"legacy": ["movetoworkspacesilent", str(target)],
                    "lua": [f'hl.dsp.window.move({{ workspace = "{target}", follow = false }})']}
        if action in ("fullscreen", "unfullscreen"):
            state = "set" if action == "fullscreen" else "unset"
            return {"legacy": ["fullscreen", f"0 {state}"],
                    "lua": [f'hl.dsp.window.fullscreen({{ action = "{state}", mode = "fullscreen" }})']}
        if action == "close_address" and isinstance(target, str) and re.fullmatch(r"0x[0-9a-fA-F]+", target):
            return {"legacy": ["closewindow", "address:" + target],
                    "lua": [f'hl.dsp.window.close({{ window = "address:{target}" }})']}
        if action == "focus_address" and isinstance(target, str) and re.fullmatch(r"0x[0-9a-fA-F]+", target):
            return {"legacy": ["focuswindow", "address:" + target],
                    "lua": [f'hl.dsp.focus({{ window = "address:{target}" }})']}
        raise CommandError("Unsupported desktop action")

    @staticmethod
    def _failed(result) -> bool:
        output = (result.stdout + result.stderr).strip().casefold()
        return bool(result.returncode) or "error" in output or "invalid" in output or "unknown" in output

    def dispatch(self, action: str, target=None):
        commands = self._commands(action, target)
        preferred = Hyprland.syntax or ("lua" if self._uses_lua_config() else "legacy")
        order = [preferred, "legacy" if preferred == "lua" else "lua"]
        detail = ""
        for syntax in order:
            try:
                result = subprocess.run(["hyprctl", "dispatch", *commands[syntax]],
                                        capture_output=True, text=True, timeout=2, check=False)
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                raise CommandError("Hyprland dispatch unavailable") from e
            if not self._failed(result):
                Hyprland.syntax = syntax
                return
            detail = detail or (result.stdout + result.stderr).strip()[:200]
        raise CommandError("Hyprland rejected the action: " + detail)

    def running(self, app) -> list[dict]:
        classes = app.classes()
        return sorted([c for c in self.query("clients") if
                       str(c.get("class", "")).casefold() in classes or
                       str(c.get("initialClass", "")).casefold() in classes],
                      key=lambda c: c.get("focusHistoryID", 9999))


def volume(action: str):
    arguments = {"volume_down": ["set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"],
                 "volume_up": ["set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%+"],
                 # Explicit states: saying "mute" twice must not unmute.
                 "volume_mute": ["set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
                 "volume_unmute": ["set-mute", "@DEFAULT_AUDIO_SINK@", "0"]}
    if action not in arguments:
        raise CommandError("Unsupported volume action")
    if not shutil.which("wpctl"):
        raise CommandError("Install WirePlumber for volume controls")
    try:
        r = subprocess.run(["wpctl", *arguments[action]], capture_output=True, text=True, timeout=2)
        if r.returncode:
            raise CommandError("Volume control failed: " + r.stderr.strip()[:200])
    except subprocess.TimeoutExpired as e:
        raise CommandError("Audio server did not respond") from e
