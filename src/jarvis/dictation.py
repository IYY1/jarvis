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


# Jarvis's own windows (settings, dictation bar) are never a dictation target.
OWN_CLASSES = ("org.jarvis.controller", "org.jarvis.dictation")


def language(config, trigger=None) -> str:
    """Dictation's language. Voice → Dictation can fix it; otherwise ("auto")
    it follows the phrase that started it: اسمعني dictates Arabic, “listen”
    English. The button and shortcut dictate Arabic while Arabic is on."""
    chosen = config["dictation"]["language"]
    if chosen in ("ar", "en"):
        return chosen
    if config["recognition"]["language"] != "ar_en":
        return "en"
    if trigger is not None and not any("\u0600" <= c <= "\u06ff" for c in trigger):
        return "en"
    return "ar"


def focused() -> tuple[str, bool, bool] | None:
    """The focused window's identity, whether it looks like a secret prompt,
    and whether it is Jarvis's own window."""
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
            own = klass.value == "TkTopLevel" and title.value.startswith("Jarvis")
            return str(hwnd), any(s in label for s in SECRET_WINDOWS), own
        result = subprocess.run(["hyprctl", "-j", "activewindow"], capture_output=True, text=True, timeout=2)
        window = json.loads(result.stdout or "{}")
        if not window.get("address"):
            return None
        label = " ".join(str(window.get(k, "")) for k in ("class", "initialClass", "title")).lower()
        own = str(window.get("class", "")).lower() in OWN_CLASSES
        return window["address"], any(s in label for s in SECRET_WINDOWS), own
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def copy_to_clipboard(text: str) -> bool:
    """Put text on the clipboard (Windows, or wl-copy on Wayland); False if not possible."""
    try:
        if is_windows():
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return True
        return subprocess.run(["wl-copy"], input=text.encode(), timeout=3,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:  # best effort; pywin32 raises its own error type
        return False


def held_message(message: str, copied: bool) -> str:
    """A refusal's message, plus how to paste the text when it reached the clipboard."""
    return f"{message} The text is on the clipboard: press Ctrl+V to paste it." if copied else message


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
        return subprocess.Popen([sys.executable, "-m", "jarvis.indicator"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except OSError:
        return None
