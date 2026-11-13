"""Rounded shapes for the Tk window, drawn once into small images.

ttk stretches each image's middle and keeps its corners, so every button and card
shares a handful of tiny pictures and stays as cheap as a flat widget. Pictures are
opaque, blended with what sits under them, so no widget needs real transparency.
"""

from __future__ import annotations
import base64
import itertools
from math import hypot
import os
import struct
import tkinter as tk
import zlib

_names = itertools.count()


def _rgb(color):
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _blend(a, b, amount):
    return tuple(round(x + (y - x) * amount) for x, y in zip(a, b))


def _coverage(distance):
    return max(0.0, min(1.0, 0.5 - distance))


def _png(width, height, pixels):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\0" + bytes(value for pixel in pixels[y * width:(y + 1) * width] for value in pixel)
                    for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def _box_distance(x, y, width, height, radius):
    qx = abs(x - width / 2) - (width / 2 - radius)
    qy = abs(y - height / 2) - (height / 2 - radius)
    return hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - radius


def rounded_png(width, height, radius, fill, under, border=None, knob=None):
    """A rounded rectangle on `under`, with an optional 1px border and a round knob at (x, y, radius, color).

    Only pixels near the edge are worked out. Everything else is plain fill, which
    keeps the larger pictures cheap to make.
    """
    fill, under = _rgb(fill), _rgb(under)
    edge = _rgb(border) if border else fill
    margin = radius + 2
    pixels = []
    for y in range(height):
        near_edge = y < margin or y >= height - margin
        for x in range(width):
            if not knob and not near_edge and margin <= x < width - margin:
                pixels.append(fill)
                continue
            px, py = x + 0.5, y + 0.5
            distance = _box_distance(px, py, width, height, radius)
            color = _blend(edge, fill, _coverage(distance + 1))
            if knob:
                color = _blend(color, _rgb(knob[3]), _coverage(hypot(px - knob[0], py - knob[1]) - knob[2]))
            pixels.append(_blend(under, color, _coverage(distance)))
    return _png(width, height, pixels)


from .icon import ICON, ICON_SIZES, icon_png  # noqa: E402,F401  (the window's pictures)


def image(root, data):
    return tk.PhotoImage(master=root, data=base64.b64encode(data), format="png")


class Shapes:
    """Makes each distinct picture once, and keeps it alive as long as the theme."""

    def __init__(self, root):
        self.root = root
        self.images = {}

    def box(self, fill, under, border=None, radius=6, size=(120, 48)):
        # ttk fills a large widget by repeating the picture's middle, one tile at a
        # time, so a tiny picture repeated thousands of times makes painting slow.
        key = (fill, under, border, radius, size)
        if key not in self.images:
            self.images[key] = image(self.root, rounded_png(*size, radius, fill, under, border))
        return self.images[key]

    def switch(self, fill, under, knob, on):
        key = ("switch", fill, under, knob, on)
        if key not in self.images:
            self.images[key] = image(self.root, rounded_png(34, 18, 9, fill, under, knob=(25 if on else 9, 9, 6, knob)))
        return self.images[key]


def _boxed(style, shapes, name, base, parts, normal, under, *, hover=None, disabled=None, focus=None, radius=6, border=None):
    """Give ttk style `name` a rounded picture behind `parts`, its usual inner layout."""
    states = []
    if disabled:
        states.append(("disabled", shapes.box(disabled, under, radius=radius)))
    if hover:
        states += [("pressed", shapes.box(hover, under, border, radius)), ("active", shapes.box(hover, under, border, radius))]
    if focus:
        states.append(("focus", shapes.box(normal, under, focus, radius)))
    element = f"{base}.round{next(_names)}"
    # width and height: without them the picture's size becomes the widget's minimum.
    style.element_create(element, "image", shapes.box(normal, under, border, radius), *states,
                         border=radius + 1, sticky="nsew", width=radius * 2 + 2, height=radius * 2 + 2)
    style.layout(name, [(element, {"sticky": "nsew", "children": parts})])


def apply(style, root, r):
    """Round the window's controls for the role colors `r`; returns what must stay alive."""
    shapes = Shapes(root)
    button = [("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]
    for name, fill, hover, under, border in (
            ("TButton", r["control"], r["control-hover"], r["card"], None),
            ("Primary.TButton", r["accent"], r["accent-hover"], r["card"], None),
            ("Danger.TButton", r["error-bg"], r["error-bg"], r["card"], r["error-text"]),
            ("Header.TButton", r["control"], r["control-hover"], r["surface"], None),
            ("Muted.TButton", r["live-soft"], r["live-soft"], r["surface"], r["live"]),
            ("Tab.TButton", r["bg"], r["control"], r["bg"], None),
            ("TabOn.TButton", r["control"], r["control"], r["bg"], None),
            ("Nav.TButton", r["surface"], r["control"], r["surface"], None),
            ("NavOn.TButton", r["selected"], r["selected"], r["surface"], None)):
        _boxed(style, shapes, name, "Button", button, fill, under, hover=hover, disabled=r["disabled"],
               focus=r["focus"], border=border)
    menu = [("Menubutton.padding", {"sticky": "nsew", "children": [
        ("Menubutton.indicator", {"side": "right", "sticky": ""}), ("Menubutton.label", {"side": "left", "sticky": ""})]})]
    _boxed(style, shapes, "TMenubutton", "Menubutton", menu, r["control"], r["surface"], hover=r["control-hover"],
           focus=r["focus"])
    label = [("Label.padding", {"sticky": "nsew", "children": [("Label.label", {"sticky": "nsew"})]})]
    for name, fill in (("Pill.TLabel", r["control"]), ("PillLive.TLabel", r["live"]),
                       ("PillCalm.TLabel", r["selected"]), ("PillOff.TLabel", r["error-bg"])):
        _boxed(style, shapes, name, "Label", label, fill, r["surface"], radius=8)
    _boxed(style, shapes, "Glyph.TLabel", "Label", label, r["control"], r["card"], radius=7)

    panel = f"Panel.round{next(_names)}"
    style.element_create(panel, "image", shapes.box(r["card"], r["bg"], r["line"], 10, size=(240, 160)), border=12,
                         sticky="nsew", width=24, height=24)
    style.layout("Panel.TFrame", [(panel, {"sticky": "nsew"})])

    # A separator is a one-pixel line: clam's own draws a light edge whatever its color.
    line = f"Line.round{next(_names)}"
    shapes.images["line"] = image(root, _png(400, 1, [_rgb(r["line"])] * 400))
    style.element_create(line, "image", shapes.images["line"], sticky="ew", width=4, height=1)
    style.layout("TSeparator", [(line, {"sticky": "ew"})])

    # Switches instead of ticked boxes.
    switch = f"Switch.round{next(_names)}"
    off = shapes.switch(r["line-strong"], r["card"], r["text-muted"], False)
    on = shapes.switch(r["accent"], r["card"], "#ffffff", True)
    style.element_create(switch, "image", off, ("selected", on), sticky="w")
    # No focus frame: a switch has no text of its own (its title is beside it), so the
    # frame was a dashed line next to the knob after every click.
    style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nsew", "children": [
        (switch, {"side": "left", "sticky": ""}),
        ("Checkbutton.label", {"side": "left", "sticky": "nsew"})]})])

    # A slim scrollbar with no arrows.
    bar = [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nsew"})]})]
    style.layout("Vertical.TScrollbar", bar)
    style.configure("Vertical.TScrollbar", arrowsize=8, gripcount=0, background=r["line-strong"], troughcolor=r["bg"],
                    bordercolor=r["bg"], lightcolor=r["line-strong"], darkcolor=r["line-strong"])
    style.map("Vertical.TScrollbar", background=[("active", r["text-muted"])])
    # Nothing to scroll: same width, so the page doesn't shift, but nothing to see.
    style.layout("Quiet.Vertical.TScrollbar", bar)
    style.configure("Quiet.Vertical.TScrollbar", arrowsize=8, gripcount=0, background=r["bg"], troughcolor=r["bg"],
                    bordercolor=r["bg"], lightcolor=r["bg"], darkcolor=r["bg"])
    style.map("Quiet.Vertical.TScrollbar", background=[("active", r["bg"])])
    return shapes


def title_bar(root, background, text, dark):
    """Match Windows' title bar to the window. Older Windows ignores what it doesn't know."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    def colorref(color):
        red, green, blue = _rgb(color)
        return red | green << 8 | blue << 16

    try:
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        hwnd = user32.GetAncestor(root.winfo_id(), 2)
        for attribute, value in ((20, int(dark)), (35, colorref(background)), (36, colorref(text))):
            data = wintypes.DWORD(value)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))
    except (AttributeError, OSError):
        pass
