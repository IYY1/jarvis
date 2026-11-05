"""Prefer the available Rust worker without importing inference libraries."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

from .commands import GREETINGS, POLITE_PREFIXES, POLITE_SUFFIXES, match_candidates, all_wake_variants
from .host import app_dir, is_windows
from .models import (ACCURATE_MODELS, accurate_model_dir, accurate_model_ready, arabic_model_dir,
                     arabic_model_ready, default_model_path, denoiser_path, usable_model, valid_model)

# The Rust worker loads Whisper when it hears a wake name and frees it after
# this long without a command; waiting for the wake phrase needs only Vosk.
WHISPER_IDLE_SECONDS = 60
EXE = ".exe" if sys.platform == "win32" else ""


def package_dir(name: str) -> Path | None:
    spec = importlib.util.find_spec(name)
    return Path(spec.origin).parent if spec and spec.origin else None


def binary_path() -> Path | None:
    # Windows releases put native helpers in tools/, beside libvosk.dll.
    candidates = [Path(__file__).with_name("bin") / ("jarvis-voice" + EXE),
                  Path(__file__).resolve().parents[2] / "native/voice-worker/target/release" / ("jarvis-voice" + EXE)]
    if is_windows():
        candidates.insert(0, app_dir() / "tools" / "jarvis-voice.exe")
    return next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


def vosk_library() -> Path | None:
    name = "libvosk.dll" if is_windows() else "libvosk.so"
    if is_windows() and (app_dir() / "tools" / name).is_file():
        return app_dir() / "tools" / name
    vosk = package_dir("vosk")
    return vosk / name if vosk and (vosk / name).is_file() else None


def python_worker_available() -> bool:
    return any(importlib.util.find_spec(name) is not None for name in ("vosk", "pocketsphinx"))


def rust_ready(config: dict) -> bool:
    """The Rust worker, Vosk library and wake model are all present."""
    settings = config["recognition"]
    model = Path(settings.get("model_path") or default_model_path()).expanduser()
    return (settings["backend"] != "pocketsphinx" and binary_path() is not None
            and vosk_library() is not None and valid_model(model))


def prepare(config: dict, mode: str, grammar: dict) -> tuple[list[str], dict, dict]:
    """Prefer Rust; explicit environment overrides select either worker.

    Paths come from the installed packages and model discovery, never from a
    command string. Python supports PocketSphinx and source-only installs.
    Windows releases ship only the Rust worker.
    """
    from .host import command
    selected = os.environ.get("MURMUR_VOICE_WORKER")
    payload = {"config": config, "mode": mode, "grammar": grammar}
    env = dict(os.environ, MURMUR_OWNER_PID=str(os.getpid()))
    python = command("voice") if is_windows() else [sys.executable, "-m", "murmur.voice"]
    if selected == "python":
        return python, payload, env
    if selected not in (None, "rust"):
        raise ValueError("MURMUR_VOICE_WORKER must be python or rust")
    binary = binary_path()
    settings = config["recognition"]
    # Linux/source installs fall back to Python, which reports its own missing
    # dependencies; Windows releases ship only the Rust worker.
    fallback = selected is None and (python_worker_available() or not is_windows())
    if fallback and (binary is None or settings["backend"] == "pocketsphinx"):
        return python, payload, env
    if binary is None:
        raise ValueError("The voice worker is missing. Reinstall Jarvis, or run scripts/build_voice_worker.py in a checkout.")
    if mode != "test" and settings["backend"] == "pocketsphinx":
        raise ValueError("The Rust worker requires Vosk; select auto/vosk or use the Python worker.")
    model = Path(settings.get("model_path") or default_model_path()).expanduser()
    library = vosk_library()
    if mode != "test" and (library is None or not valid_model(model)):
        if fallback:
            return python, payload, env
        raise ValueError("The voice worker requires the bundled Vosk library and model; reinstall Jarvis.")
    native = {"vosk_library": str(library or ""),
              "model_path": str(model), "whisper": None, "denoiser": None,
              "threads": max(1, min(8, (os.cpu_count() or 2) // 2)),
              "candidates": list(match_candidates(grammar).items()),
              "wake_forms": all_wake_variants(config["activation"]),
              "greetings": list(GREETINGS), "prefixes": list(POLITE_PREFIXES),
              "suffixes": list(POLITE_SUFFIXES), "whisper_idle_seconds": WHISPER_IDLE_SECONDS,
              # Frees each Vosk decoder's feature history; see docs/vosk-finalization.md.
              "vosk_finalize_utterances": True}
    whisper = usable_model(config)
    if settings["accurate"] and accurate_model_ready(whisper):
        folder = accurate_model_dir(whisper)
        native["whisper"] = {k: str(folder / v) for k, v in ACCURATE_MODELS[whisper]["files"].items()}
        if settings["noise_suppression"] and denoiser_path().is_file():
            native["denoiser"] = str(denoiser_path())
        # Arabic and English: Moonshine Streaming Tiny Arabic beside Whisper.
        if settings["language"] == "ar_en":
            if not arabic_model_ready():
                raise ValueError("Arabic is selected but the Arabic model is missing. "
                                 "Install it in Voice settings or run: jarvis install-model --arabic")
            native["arabic"] = str(arabic_model_dir())
    language = config["dictation"]["language"]
    if language == "auto":
        language = "ar" if native.get("arabic") else "en"
    if language == "ar" and not native.get("arabic") and mode == "dictation":
        raise ValueError("Arabic dictation needs the Arabic and English language setting and the Arabic model")
    native["dictation_language"] = language if native.get("arabic") else "en"
    payload["native"] = native
    return [str(binary)], payload, env
