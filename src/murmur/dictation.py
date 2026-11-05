"""Dictation for the Python service (Windows, and Linux without the Rust controller).

Mirrors native/controller/src/dictation.rs: text the voice worker transcribed is
typed by the `jarvis-type` helper into the window focused when dictation
started. A focus change stops dictation without typing; the text is kept so
the user can copy it. Password fields are refused by the helper.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from .commands import normalize
from .host import app_dir, child_options, is_windows

EXE = ".exe" if sys.platform == "win32" else ""
# Windows that must never receive keyboard-typed text: the helper cannot see
# inside them, so a password prompt there would be invisible.
SECRET_WINDOWS = ("pinentry", "polkit", "gcr-prompter", "keepassxc", "bitwarden", "1password", "seahorse",
                  "kwalletd", "credential", "password", "passphrase", "كلمة المرور", "كلمه المرور")


def helper_path() -> Path | None:
    candidates = [Path(__file__).with_name("bin") / ("jarvis-type" + EXE),
                  Path(__file__).resolve().parents[2] / "native/text-input/target/release" / ("jarvis-type" + EXE)]
    if is_windows():
        candidates.insert(0, app_dir() / "tools" / "jarvis-type.exe")
    return next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


def split_stop(text: str, stops) -> tuple[str, bool]:
    """Split a configured stop phrase off the end of dictated text."""
    words = text.split()
    for stop in stops:
        try:
            stop = normalize(stop)
        except ValueError:
            continue
        size = len(stop.split())
        if not stop or len(words) < size:
            continue
        tail = " ".join(w for w in (normalize(word) for word in words[-size:]) if w)
        if tail == stop:
            return " ".join(words[:-size]), True
    return text, False


def focused() -> tuple[str, bool] | None:
    """The focused window's identity, and whether it looks like a secret prompt."""
    try:
        if is_windows():
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            klass = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, klass, 256)
            label = f"{klass.value} {title.value}".lower()
            return str(hwnd), any(s in label for s in SECRET_WINDOWS)
        result = subprocess.run(["hyprctl", "-j", "activewindow"], capture_output=True, text=True, timeout=2)
        window = json.loads(result.stdout or "{}")
        if not window.get("address"):
            return None
        label = " ".join(str(window.get(k, "")) for k in ("class", "initialClass", "title")).lower()
        return window["address"], any(s in label for s in SECRET_WINDOWS)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


class Typer:
    """One `jarvis-type` process for one dictation session."""

    def __init__(self, process, capabilities):
        self.process, self.capabilities = process, capabilities
        self.lock = asyncio.Lock()

    @classmethod
    async def start(cls) -> "Typer":
        binary = helper_path()
        if binary is None:
            raise RuntimeError("Dictation's text helper is missing. Reinstall Jarvis or run scripts/build_controller.py.")
        process = await asyncio.create_subprocess_exec(
            str(binary), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=65536, **child_options())
        ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 5) or b"{}")
        if not ready.get("ready"):
            process.kill()
            raise RuntimeError(ready.get("message") or "Text insertion is unavailable in this session")
        return cls(process, ready.get("capabilities", {}))

    async def insert(self, text: str, keyboard: bool) -> dict:
        async with self.lock:
            self.process.stdin.write((json.dumps({"op": "insert", "text": text, "keyboard": keyboard}) + "\n").encode())
            await self.process.stdin.drain()
            line = await asyncio.wait_for(self.process.stdout.readline(), 5 + len(text) / 100)
            if not line:
                raise RuntimeError("Text insertion helper stopped")
            return json.loads(line)

    async def close(self) -> None:
        async with self.lock:
            if self.process.returncode is None:
                self.process.stdin.close()
                try:
                    await asyncio.wait_for(self.process.wait(), 2)
                except TimeoutError:
                    self.process.kill()


class Session:
    def __init__(self):
        self.active = False
        self.typer: Typer | None = None
        self.window = None
        self.inserted = 0
        self.method = ""
        self.held = ""
        self.message = ""
        self.language = "en"
        self.typed_any = False
        self.indicator = None

    def status(self) -> dict:
        return {"active": self.active, "inserted": self.inserted, "method": self.method, "held": self.held,
                "message": self.message, "language": self.language,
                "capabilities": self.typer.capabilities if self.typer else {}}


def indicator():
    """The always-on-top Stop bar (indicator.py); Linux only."""
    if is_windows():
        return None
    env = dict(os.environ)
    layer = next((p for p in ("/usr/lib/libgtk4-layer-shell.so.0", "/usr/lib64/libgtk4-layer-shell.so.0")
                  if Path(p).is_file()), None)
    if layer:
        env["LD_PRELOAD"] = layer
    try:
        return subprocess.Popen([sys.executable, "-m", "murmur.indicator"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except OSError:
        return None
