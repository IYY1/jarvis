#!/usr/bin/env python3
"""Make src/jarvis/windows/jarvis.ico from the Jarvis mark (Windows only: GDI+).

The program's icon, its tray icon, the window's title bar and sidebar all come
from this file, drawn by the same paint_mark the listening sign uses, so they
match it. Small sizes keep fewer lines so they stay clear. See-through around the
mark; one PNG per size.
"""
from __future__ import annotations

import ctypes
from pathlib import Path
import struct
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jarvis.windows.icon import ICON, ICON_SIZES  # noqa: E402
from jarvis.windows.listening_sign import GLOW, paint_mark  # noqa: E402

ANGLE, GLOW_LEVEL = 12.0, 0.6   # how the turning layers sit in the still icon


def png(size, rgba):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def render(gp, size):
    P, c_float, c_int = ctypes.c_void_p, ctypes.c_float, ctypes.c_int
    buffer = ctypes.create_string_buffer(size * size * 4)
    bitmap, graphics, brush, pen = P(), P(), P(), P()
    gp.GdipCreateBitmapFromScan0(c_int(size), c_int(size), c_int(size * 4), c_int(0x000E200B), buffer,
                                 ctypes.byref(bitmap))   # 32bpp premultiplied, into `buffer`
    gp.GdipGetImageGraphicsContext(bitmap, ctypes.byref(graphics))
    gp.GdipSetSmoothingMode(graphics, c_int(4))
    gp.GdipSetPixelOffsetMode(graphics, c_int(4))
    gp.GdipCreateSolidFill(ctypes.c_uint32(0), ctypes.byref(brush))
    gp.GdipCreatePen1(ctypes.c_uint32(0), c_float(1), c_int(2), ctypes.byref(pen))
    detail = 1 if size <= 24 else 2 if size <= 48 else 3
    radius = size / 2 - max(1.5, size / 48)
    bold = 2.0 if size <= 24 else 1.6 if size <= 48 else 1.15   # thicker lines where pixels are few
    paint_mark(gp, graphics, brush, pen, size / 2, radius, radius / 27 * bold, ANGLE, GLOW_LEVEL, GLOW, detail=detail)
    gp.GdipFlush(graphics, c_int(1))
    for free, handle in (("GdipDeleteGraphics", graphics), ("GdipDeleteBrush", brush), ("GdipDeletePen", pen),
                         ("GdipDisposeImage", bitmap)):
        getattr(gp, free)(handle)
    rgba = bytearray(size * size * 4)
    data = buffer.raw
    for i in range(0, len(data), 4):   # BGRA, premultiplied -> RGBA, straight
        b, g, r, a = data[i:i + 4]
        if a:
            rgba[i:i + 4] = bytes((min(255, r * 255 // a), min(255, g * 255 // a), min(255, b * 255 // a), a))
    return png(size, bytes(rgba))


def main():
    gp = ctypes.WinDLL("gdiplus")
    P, c_float = ctypes.c_void_p, ctypes.c_float
    for name, types in {"GdipSetPenWidth": (P, c_float), "GdipSetPenColor": (P, ctypes.c_uint32),
                        "GdipSetSolidFillColor": (P, ctypes.c_uint32),
                        "GdipFillEllipse": (P, P) + (c_float,) * 4, "GdipDrawEllipse": (P, P) + (c_float,) * 4,
                        "GdipDrawArc": (P, P) + (c_float,) * 6, "GdipDrawLine": (P, P) + (c_float,) * 4,
                        "GdipSetPenDashArray": (P, P, ctypes.c_int), "GdipSetPenDashStyle": (P, ctypes.c_int)}.items():
        getattr(gp, name).argtypes = types

    class StartupInput(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("callback", P), ("no_thread", ctypes.c_int), ("no_codecs", ctypes.c_int)]
    token = ctypes.c_size_t()
    gp.GdiplusStartup(ctypes.byref(token), ctypes.byref(StartupInput(1, None, 0, 0)), None)
    images = [(size, render(gp, size)) for size in ICON_SIZES]
    gp.GdiplusShutdown(token)
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    ICON.write_bytes(header + entries + blobs)
    print(f"{ICON}: {len(images)} sizes, {ICON.stat().st_size} bytes")


if __name__ == "__main__":
    main()
