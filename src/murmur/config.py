"""Validated user configuration; atomic, private writes and no implicit setup."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import sys
import tempfile


DEFAULTS = {
    "activation": {"mode": "wake_word", "wake_phrase": "hey jarvis", "wake_aliases": [], "sensitivity": 0.5,
                   "command_timeout": 8, "confidence": 0.35},
    # adaptive_gate: raise the speech threshold automatically with steady background sound.
    "audio": {"device": "", "vad_rms": 0.005, "adaptive_gate": True},
    # backend: auto = Vosk when its model is installed, else PocketSphinx.
    # closest_match: let Vosk pick the nearest command when the grammar can't.
    # accurate: use the downloaded Whisper model for commands (better with accents).
    # noise_suppression: clean command audio before transcribing (off: it made
    # synthetic tests slightly worse and doesn't remove background voices).
    # speech_model: "light" (Whisper tiny.en, default: about half the peak RAM)
    # or "small" (small.en: more accurate on unusual words).
    "recognition": {"backend": "auto", "model_path": "", "closest_match": True,
                    "accurate": True, "noise_suppression": False, "speech_model": "light",
                    # language: "en", or "ar_en" for Arabic and English commands and dictation
                    # (a multilingual model; see docs/arabic.md).
                    "language": "en"},
    "privacy": {"onboarded": False},
    # close_words: what to say before an app name to close it ("close firefox").
    "aliases": {}, "commands": {}, "app_prefixes": {}, "close_words": ["close"], "startup": False,
    # dictation: "Jarvis, listen to me" types speech into the focused text field.
    # language: "auto" follows recognition.language; "ar" or "en" chooses one.
    "dictation": {"language": "auto", "idle_seconds": 20, "max_minutes": 10, "stop_phrases": ["stop dictation", "وقف الكتابة"],
                  "keyboard_fallback": True},
    "recording": {"output_dir": "~/Videos/Murmur", "monitor": "", "system_audio": False,
                  "microphone": False, "audio_device": "", "fps": 30,
                  "codec": "libx264", "quality": 23, "rate_control": "quality",
                  "bitrate_mbps": 8, "quality_preset": "very_high", "encoder": "auto", "configured": False, "color_mode": "auto",
                  "combined_audio_source": False, "sound_feedback": False,
                  "vaapi_device": "/dev/dri/renderD128"},
    # Disk storage is an explicit privacy choice. RAM remains the default.
    # game_audio/microphone mix the default output and input into one track; sound chirps on save.
    # audio_tracks "separate" keeps game, each of separate_apps and the microphone on their own tracks.
    "replay": {"seconds": 120, "autostart": False, "game_audio": True, "microphone": True, "sound": True,
               "storage": "ram", "max_mb": 256, "buffer_dir": "", "audio_tracks": "mixed", "separate_apps": []},
}
# Sections that existed in earlier versions and are now ignored.
LEGACY_SECTIONS: tuple[str, ...] = ()
# Free-form mappings; their keys are user phrases, not setting names.
MAPPINGS = ("aliases", "commands", "app_prefixes")


def unknown_keys(data: dict) -> list[str]:
    """Setting names that DEFAULTS does not define, e.g. a typo like 'comand_timeout'."""
    found = []
    for key, value in data.items():
        if key in LEGACY_SECTIONS:
            continue
        if key not in DEFAULTS:
            found.append(key)
        elif key not in MAPPINGS and isinstance(value, dict) and isinstance(DEFAULTS[key], dict):
            found.extend(f"{key}.{name}" for name in value if name not in DEFAULTS[key])
    return found


def without_unknown(data: dict) -> dict:
    """Drop legacy sections and unknown settings (used when loading an existing file)."""
    clean = {}
    for key, value in data.items():
        if key not in DEFAULTS:
            continue
        if key not in MAPPINGS and isinstance(value, dict) and isinstance(DEFAULTS[key], dict):
            value = {name: v for name, v in value.items() if name in DEFAULTS[key]}
        clean[key] = value
    return clean


def config_path() -> Path:
    from .host import settings_dir
    return Path(os.environ.get("MURMUR_CONFIG", str(settings_dir() / "config.json")))


def merged(base: dict, update: dict) -> dict:
    """Merge settings, copying each retained default value only once."""
    result = {}
    for key, value in base.items():
        if key not in update:
            result[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(update[key], dict):
            result[key] = merged(value, update[key])
        else:
            result[key] = update[key]
    for key, value in update.items():
        if key not in base:
            result[key] = value
    return result


def validate(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a JSON object")
    unknown = unknown_keys(data)
    if unknown:
        raise ValueError("Unknown setting(s): " + ", ".join(sorted(unknown)[:5]))
    c = merged(DEFAULTS, {k: v for k, v in data.items() if k not in LEGACY_SECTIONS})
    for key in ("activation", "audio", "recognition", "privacy", "recording", "replay", "aliases", "commands", "dictation"):
        if not isinstance(c[key], dict):
            raise ValueError(f"{key} must be an object")
    a = c["activation"]
    recognition = c["recognition"]
    if recognition["backend"] not in ("auto", "pocketsphinx", "vosk"):
        raise ValueError("Recognition backend must be auto, pocketsphinx or vosk")
    if not isinstance(recognition["model_path"], str) or len(recognition["model_path"]) > 4096 or "\0" in recognition["model_path"]:
        raise ValueError("Invalid voice model path")
    if recognition["speech_model"] not in ("small", "light"):
        raise ValueError("Speech model must be small or light")
    if recognition["language"] not in ("en", "ar_en"):
        raise ValueError("Recognition language must be en (English) or ar_en (Arabic and English)")
    d = c["dictation"]
    if d["language"] not in ("auto", "ar", "en"):
        raise ValueError("Dictation language must be auto, ar or en")
    if not isinstance(d, dict) or not all(isinstance(d[k], (int, float)) and not isinstance(d[k], bool) for k in ("idle_seconds", "max_minutes")) \
            or not 5 <= d["idle_seconds"] <= 300 or not 1 <= d["max_minutes"] <= 60 or not isinstance(d["keyboard_fallback"], bool):
        raise ValueError("Dictation needs 5–300 idle seconds, 1–60 minutes and a true/false keyboard fallback")
    if not isinstance(d["stop_phrases"], list) or len(d["stop_phrases"]) > 8:
        raise ValueError("Use up to eight dictation stop phrases")
    from .commands import normalize as _normalize
    for phrase in d["stop_phrases"]:
        if not isinstance(phrase, str) or len(phrase) > 40 or not _normalize(phrase):
            raise ValueError("Dictation stop phrases must be short words")
    for flag in (recognition["closest_match"], recognition["accurate"], recognition["noise_suppression"],
                 c["audio"]["adaptive_gate"]):
        if not isinstance(flag, bool):
            raise ValueError("Recognition and audio switches must be true or false")
    if a["mode"] not in ("wake_word", "push_to_talk"):
        raise ValueError("Activation must be wake_word or push_to_talk")
    from .commands import normalize
    if not isinstance(a["wake_phrase"], str) or not normalize(a["wake_phrase"]) or len(a["wake_phrase"]) > 80:
        raise ValueError("Choose a short wake phrase")
    aliases = a["wake_aliases"]
    if not isinstance(aliases, list) or len(aliases) > 7:
        raise ValueError("Use up to eight wake names including the primary name")
    names = []
    for value in [a["wake_phrase"], *aliases]:
        if not isinstance(value, str) or len(value) > 80 or not normalize(value):
            raise ValueError("Choose short wake names")
        words = normalize(value).split()
        from .commands import GREETINGS
        names.append(" ".join(words[1:] if len(words) > 1 and words[0] in GREETINGS else words))
    if len(set(names)) != len(names):
        raise ValueError("Wake names must be distinct")
    for value, low, high, name in (
        (a["sensitivity"], 0, 1, "wake sensitivity"),
        (a["confidence"], 0, 1, "confidence"),
        (a["command_timeout"], 2, 15, "command timeout"),
        (c["audio"]["vad_rms"], 0.0001, 0.5, "VAD level"),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Invalid {name}: expected {low}–{high}")
    for key in ("device",):
        if not isinstance(c["audio"][key], str) or len(c["audio"][key]) > 512 or "\0" in c["audio"][key]:
            raise ValueError("Invalid audio device")
    for section in ("aliases", "commands"):
        if len(c[section]) > 256 or any(not isinstance(k, str) or not isinstance(v, str) or
                                      not normalize(k) or len(k) > 120 or len(v) > 256
                                      for k, v in c[section].items()):
            raise ValueError(f"Invalid {section}: use short phrase to target mappings")
        names = [normalize(k) for k in c[section]]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate normalized {section}")
    # Same rules the service applies at startup, so a saved file can never stop it starting.
    from .commands import Parser
    Parser.from_config(c)  # validate configured shortcuts with the dispatch parser
    for value in (c["privacy"]["onboarded"], c["startup"], c["recording"]["configured"],
                  c["recording"]["system_audio"], c["recording"]["microphone"], c["recording"]["combined_audio_source"], c["recording"]["sound_feedback"]):
        if not isinstance(value, bool):
            raise ValueError("Consent, startup and recording flags must be booleans")
    r = c["recording"]
    if r["color_mode"] not in ("auto", "hdr", "sdr"):
        raise ValueError("Capture color mode must be auto, hdr or sdr")
    from .encoding import validate_encoding
    validate_encoding(r)
    if r["encoder"] not in ("auto", "nvenc", "amf", "qsv", "cpu"):
        raise ValueError("Choose automatic, NVIDIA, AMD, Intel or CPU encoding")
    vaapi = r["vaapi_device"]
    if not isinstance(vaapi, str) or not vaapi.startswith("/dev/dri/renderD") or len(vaapi) > 64:
        raise ValueError("VA-API device must be a /dev/dri/renderD* path")
    for key in ("output_dir", "monitor", "audio_device"):
        if not isinstance(r[key], str) or len(r[key]) > 4096 or "\0" in r[key]:
            raise ValueError(f"Invalid recording {key}")
    if type(c["replay"]["seconds"]) is not int or not 10 <= c["replay"]["seconds"] <= 1800:
        raise ValueError("Replay length must be 10–1800 seconds (30 minutes)")
    if c["replay"]["storage"] not in ("ram", "disk"):
        raise ValueError("Replay storage must be ram or disk")
    if type(c["replay"]["max_mb"]) is not int or not 64 <= c["replay"]["max_mb"] <= 16384:
        raise ValueError("Replay budget must be 64–16384 MiB")
    path = c["replay"]["buffer_dir"]
    if not isinstance(path, str) or len(path) > 4096 or "\0" in path:
        raise ValueError("Invalid replay buffer directory")
    if not all(isinstance(c["replay"][key], bool) for key in ("autostart", "game_audio", "microphone", "sound")):
        raise ValueError("Replay switches must be true or false")
    if c["replay"]["audio_tracks"] not in ("mixed", "separate"):
        raise ValueError("Replay audio tracks must be mixed or separate")
    apps = c["replay"]["separate_apps"]
    if not isinstance(apps, list) or len(apps) > 4:
        raise ValueError("Choose up to four apps for their own replay tracks")
    from .replay import valid_app_name
    if not all(isinstance(app, str) and valid_app_name(app) for app in apps):
        raise ValueError("App audio names may use letters, digits, spaces, dots, dashes and underscores")
    from .host import is_windows
    if not is_windows() and r["system_audio"] and r["microphone"] and not (r["combined_audio_source"] and r["audio_device"].strip()):
        raise ValueError("wf-recorder accepts one audio source. Set up a combined PipeWire source first.")
    if len(json.dumps(c, allow_nan=False).encode()) > 60000:
        raise ValueError("Configuration is too large; keep serialized settings below 60 KiB")
    return c


def load(path: Path | None = None) -> dict:
    path = path or config_path()
    if not path.exists():
        defaults = copy.deepcopy(DEFAULTS)
        from .host import is_windows
        if is_windows():
            defaults["recording"].update(codec="h264", rate_control="bitrate", output_dir="~/Videos/Jarvis")
        return defaults
    if path.stat().st_size > 65536:
        raise ValueError("Configuration exceeds 64 KiB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        unknown = unknown_keys(data)
        if unknown:
            # Tolerate old or hand-edited files: warn, ignore, and drop on next save.
            print(f"murmur: ignoring unknown setting(s) in {path}: {', '.join(sorted(unknown))}", file=sys.stderr)
        data = without_unknown(data)
    return validate(data)


def save(data: dict, path: Path | None = None) -> dict:
    c = validate(data)
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(c, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        from .host import sync_directory
        sync_directory(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return c
