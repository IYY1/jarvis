"""The Jarvis icon file and its pictures, without Tk (the service reads it too)."""

from __future__ import annotations
from pathlib import Path
import struct

ICON = Path(__file__).with_name("jarvis.ico")
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def icon_png(size):
    """The PNG stored in jarvis.ico for one size (the exe, tray, window, sidebar and listening sign share it)."""
    data = ICON.read_bytes()
    count = struct.unpack_from("<H", data, 4)[0]
    for n in range(count):
        width, _h, _c, _r, _p, _b, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * n)
        if (width or 256) == size:
            return data[offset:offset + length]
    raise LookupError(f"jarvis.ico has no {size} px picture")
