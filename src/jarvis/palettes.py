"""Color presets from Sanzo Wada's "A Dictionary of Color Combinations" (1933).

Each preset is one of the book's numbered combinations. Its hues are the book's
pigments (hex values from the MIT-licensed dataset at
github.com/mattdesl/dictionary-of-colour-combinations); the layers a window
needs (page, cards, lines, muted text) are those same pigments mixed with each
other, with paper white or with the book's Black. Error colors use the book's
Carmine Red and Corinthian Pink in every preset.

The choice is a preference of the settings window, not of the service, so it
lives in appearance.json beside config.json and works while the service is off.
Both the GTK window (Linux) and the Tk window (Windows) read it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PAPER = "#ffffff"
BLACK = "#111314"            # the book's Black
CARMINE_RED = "#a62c37"
CORINTHIAN_PINK = "#f8b6ba"


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def mix(a: str, b: str, amount: float) -> str:
    """`amount` of the way from color a to color b."""
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * amount) for x, y in zip(_rgb(a), _rgb(b)))


def luminance(color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(color)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio, 1 to 21."""
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _dark(swatches: dict, base: str, tint: str, text: str, accent: str, live: str) -> dict:
    """Roles for a dark preset: `base` is the page, `tint` lifts cards and
    controls, `accent` marks choices, `live` marks listening and capture."""
    bg = mix(base, tint, 0.10)
    roles = {
        "bg": bg, "surface": mix(bg, tint, 0.20), "card": mix(bg, tint, 0.32),
        "inset": mix(base, BLACK, 0.5), "control": mix(bg, tint, 0.60),
        "line": mix(bg, text, 0.13), "line-strong": mix(bg, text, 0.28),
        "text": text, "text-muted": mix(text, bg, 0.30), "text-disabled": mix(text, bg, 0.55),
        "disabled": mix(bg, text, 0.06),
        "accent": accent, "accent-text": mix(accent, PAPER, 0.35), "selected": mix(bg, accent, 0.30),
        "live": live, "live-soft": mix(bg, live, 0.16), "live-text": live,
        "focus": live, "error-bg": mix(bg, CARMINE_RED, 0.40), "error-text": CORINTHIAN_PINK,
    }
    roles["control-hover"] = mix(roles["control"], text, 0.08)
    roles["control-active"] = mix(roles["control"], text, 0.16)
    roles["accent-hover"] = mix(accent, PAPER, 0.18)
    roles["on-accent"] = BLACK
    roles["on-live"] = BLACK
    return {"dark": True, "swatches": swatches, "roles": roles}


def _light(swatches: dict, base: str, text: str, accent: str, live: str) -> dict:
    """Roles for a light preset: `base` is the paper, `text` the ink."""
    bg = mix(base, PAPER, 0.45)
    roles = {
        "bg": bg, "surface": mix(base, PAPER, 0.25), "card": mix(base, PAPER, 0.72),
        "inset": mix(base, PAPER, 0.88), "control": mix(base, text, 0.08),
        "line": mix(bg, text, 0.16), "line-strong": mix(bg, text, 0.40),
        "text": text, "text-muted": mix(text, bg, 0.30), "text-disabled": mix(text, bg, 0.55),
        "disabled": mix(bg, text, 0.06),
        "accent": accent, "accent-text": accent, "selected": mix(bg, live, 0.20),
        "live": live, "live-soft": mix(bg, live, 0.18), "live-text": mix(live, text, 0.45),
        "focus": mix(live, text, 0.45), "error-bg": mix(bg, CORINTHIAN_PINK, 0.55), "error-text": mix(CARMINE_RED, text, 0.15),
    }
    roles["control-hover"] = mix(roles["control"], text, 0.08)
    roles["control-active"] = mix(roles["control"], text, 0.16)
    roles["accent-hover"] = mix(accent, live, 0.25)
    roles["on-accent"] = bg
    roles["on-live"] = text
    return {"dark": False, "swatches": swatches, "roles": roles}


PRESETS: dict[str, dict] = {
    "344": {"name": "Violet & cinnamon", "number": 344,
            **_dark({"Aconite Violet": "#a36aa5", "Cinnamon Buff": "#fdc57e",
                     "Deep Lyons Blue": "#1c4286", "Black": BLACK},
                    base=BLACK, tint="#1c4286", text=mix("#fdc57e", PAPER, 0.80),
                    accent=mix("#a36aa5", PAPER, 0.18), live="#fdc57e")},
    "166": {"name": "Grenadine & slate green", "number": 166,
            **_dark({"Grenadine Pink": "#f48067", "Naples Yellow": "#fbe6a0",
                     "Deep Slate Green": "#112f2c"},
                    base=mix("#112f2c", BLACK, 0.45), tint=mix("#112f2c", "#fbe6a0", 0.18), text=mix("#fbe6a0", PAPER, 0.60),
                    accent="#f48067", live="#fbe6a0")},
    "190": {"name": "Ivory & English red", "number": 190,
            **_light({"Ivory Buff": "#ebd3a2", "English Red": "#d96629", "Black": BLACK},
                     base="#ebd3a2", text=BLACK, accent=BLACK, live="#d96629")},
}
DEFAULT = "344"

# Neutral graphite with one blue accent, for the Windows window. Not from the book.
_GRAPHITE_BG = "#14161c"
GRAPHITE = {
    "name": "Graphite", "number": None, "dark": True,
    "swatches": {"Blue": "#4c8dff", "Card": "#1c1f27", "Page": "#14161c", "Rail": "#0f1116"},
    "roles": {
        "bg": _GRAPHITE_BG, "surface": "#0f1116", "card": "#1c1f27", "inset": "#14161c", "control": "#2a2e39",
        "control-hover": "#343945", "control-active": "#3d4250", "line": "#2c303b", "line-strong": "#4a4f5d",
        "text": "#f2f3f5", "text-muted": "#a4aab6", "text-disabled": "#6b7180", "disabled": "#20232b",
        "accent": "#4c8dff", "accent-hover": "#6aa1ff", "accent-text": "#7fb0ff", "on-accent": "#0b0d12",
        "selected": "#26324b", "live": "#3dd68c", "live-soft": mix(_GRAPHITE_BG, "#3dd68c", 0.16), "live-text": "#3dd68c",
        "on-live": "#0b0d12", "focus": "#6aa1ff", "error-bg": mix(_GRAPHITE_BG, "#f0616d", 0.22), "error-text": "#ff9aa2",
    },
}
ALL_PRESETS: dict[str, dict] = {"graphite": GRAPHITE, **PRESETS}


def preference_path() -> Path:
    from .config import config_path
    return config_path().with_name("appearance.json")


def chosen(default: str = DEFAULT) -> str:
    """The saved preset, or the default when none (or an unknown one) is saved."""
    try:
        name = str(json.loads(preference_path().read_text(encoding="utf-8")).get("palette", default))
    except (OSError, ValueError, AttributeError):
        return default
    return name if name in ALL_PRESETS else default


def choose(name: str) -> None:
    if name not in ALL_PRESETS:
        raise ValueError(f"Unknown color preset {name!r}")
    path = preference_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"palette": name}) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def roles(name: str | None = None) -> dict[str, str]:
    return ALL_PRESETS[name if name in ALL_PRESETS else chosen()]["roles"]


def gtk_colors(name: str | None = None) -> str:
    """@define-color lines for style.css."""
    return "".join(f"@define-color {role} {color};\n" for role, color in roles(name).items())
