"""Native Windows settings. Network/audio/COM work never runs on Tk's thread.

Settings apply as they change, as in the Linux window: switches and lists save
shortly after a change, text and numbers when you press Enter or leave the
field. With the service off, changes go straight to the settings file.
Colors are the Sanzo Wada presets in palettes.py, drawn with ttk's clam theme.
"""

from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import tkinter.font as tkfont

from .. import config, palettes
from ..client import TIMEOUTS, request, restart_note, save_config_restarting, watch
from ..commands import REFERENCE
from . import look
from .home import Home
from .system import release_unused_memory

RELEASE_MEMORY_AFTER_MS = 4000
MAX_SEPARATE_APPS = 4   # config.validate allows this many
DEFAULT_MICROPHONE = "Default Windows microphone"
PRIMARY_DISPLAY = "Looking for displays…"   # shown only until the service lists them
SEE_THROUGH = "#ff00fe"   # the message window's corners: Windows shows what's behind wherever this colour is


def microphone_name(device_id):
    """A saved microphone setting as the list shows it: its name, without the "input:" id prefix."""
    return device_id.removeprefix("input:") if device_id else DEFAULT_MICROPHONE


# Sidebar order: (page, title).
NAVIGATION = [("dashboard", "Home"), ("voice", "Voice"), ("commands", "Apps & commands"),
              ("recording", "Capture"), ("clips", "Clips"), ("general", "Settings")]
PAGE_ALIASES = {"activation": "voice", "replay": "recording", "privacy": "general",   # older links
                "applications": "commands", "diagnostics": "general"}

PRIVACY_FACTS = [
    ("Everything stays on this computer", "Speech is recognized locally. No account, cloud service or API key."),
    ("It listens only for its name", "While unmuted, a small local model waits for your wake name. Commands are "
                                     "recognized only in the short session after it."),
    ("Nothing is kept", "Command audio and text are discarded after use. Recording and replay include your voice "
                        "only when you turn their microphone on."),
    ("Mute is one click away", "Use Mute at the top of this window, or Ctrl+M. Closing this window keeps Jarvis "
                               "in the notification area; Quit Jarvis stops it."),
]


def icon_bits(key):
    """A 16×16 one-colour icon for a sidebar page, drawn from simple shapes, as XBM
    data: a few dozen bytes each, so the sidebar costs no image files."""
    pixels = [[False] * 16 for _ in range(16)]

    def dot(x, y):
        if 0 <= x < 16 and 0 <= y < 16:
            pixels[int(y)][int(x)] = True

    def box(x0, y0, x1, y1, fill=True):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if fill or x in (x0, x1) or y in (y0, y1):
                    dot(x, y)

    def ring(cx, cy, outer, inner=0.0):
        for y in range(16):
            for x in range(16):
                d = ((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5
                if inner <= d <= outer:
                    dot(x, y)

    if key == "dashboard":                      # a house
        for i in range(8):
            dot(7 - i, 1 + i), dot(8 + i, 1 + i), dot(7 - i, 2 + i), dot(8 + i, 2 + i)
        box(3, 8, 12, 14, fill=False), box(3, 9, 3, 14), box(12, 9, 12, 14), box(7, 10, 8, 14)
    elif key == "voice":                        # a microphone
        box(6, 1, 9, 8), dot(5, 2), dot(10, 2), box(5, 2, 5, 8), box(10, 2, 10, 8)
        for y in range(6, 11):
            dot(3, y), dot(12, y)
        box(4, 11, 4, 11), box(11, 11, 11, 11), box(5, 12, 10, 12), box(7, 13, 8, 14), box(5, 15, 10, 15)
    elif key == "commands":                     # four tiles
        box(2, 2, 6, 6), box(9, 2, 13, 6), box(2, 9, 6, 13), box(9, 9, 13, 13)
    elif key == "recording":                    # record
        ring(8, 8, 7.2, 5.6), ring(8, 8, 3.4)
    elif key == "clips":                        # a video: frame and play mark
        box(1, 3, 14, 12, fill=False), box(1, 4, 14, 4), box(1, 11, 14, 11)
        for i, x in enumerate(range(6, 11)):   # a play triangle, narrowing to the right
            box(x, 5 + i // 2, x, 10 - i // 2)
    elif key == "general":                      # sliders
        for y, knob in ((3, 10), (8, 5), (13, 9)):
            box(1, y, 14, y), box(knob - 1, y - 2, knob + 1, y + 2)
    rows = []
    for row in pixels:
        value = sum(1 << x for x, on in enumerate(row) if on)
        rows += [value & 0xFF, value >> 8]
    # Tk reads the braces only as words of their own.
    return ("#define i_width 16\n#define i_height 16\nstatic unsigned char i_bits[] = {\n"
            + ", ".join(f"0x{b:02x}" for b in rows) + "\n};\n")


class VolumeSlider(tk.Canvas):
    """A volume slider like Medal's: a thin track filled up to the value, and a
    round knob. ``on_change`` follows the drag; ``on_done`` gets the final value."""

    PAD, HEIGHT = 9, 22

    def __init__(self, parent, window, value, on_change, on_done, width=180):
        super().__init__(parent, width=width, height=self.HEIGHT, highlightthickness=0, borderwidth=0,
                         background=palettes.roles(window.palette)["card"], cursor="hand2")
        self.window, self.value, self.on_change, self.on_done = window, value, on_change, on_done
        window.recolor.append((self, {"background": "card"}))
        self.bind("<Configure>", lambda _: self.draw())
        self.bind("<Button-1>", self.drag)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<ButtonRelease-1>", lambda _: self.on_done(self.value))

    def drag(self, event):
        span = max(1, self.winfo_width() - 2 * self.PAD)
        self.value = max(0, min(100, round((event.x - self.PAD) / span * 100)))
        self.draw()
        self.on_change(self.value)

    def draw(self):
        r = palettes.roles(self.window.palette)
        self.delete("all")
        width, middle = self.winfo_width(), self.HEIGHT // 2
        end = width - self.PAD
        at = self.PAD + (end - self.PAD) * self.value / 100
        self.create_line(self.PAD, middle, end, middle, fill=r["line-strong"], width=4, capstyle="round")
        if self.value:
            self.create_line(self.PAD, middle, at, middle, fill=r["accent"], width=4, capstyle="round")
        self.create_oval(at - 7, middle - 7, at + 7, middle + 7, fill=r["text"], outline="")


class Tabs:
    """Short sections of one page, one shown at a time."""

    def __init__(self, window, bar, holder, items):
        self.window, self.frames, self.buttons = window, {}, {}
        for key, label in items:
            frame = ttk.Frame(holder)
            frame.columnconfigure(0, weight=1)
            frame.next_row = 0
            frame.grid(row=0, column=0, sticky="new")
            self.frames[key] = frame
            self.buttons[key] = ttk.Button(bar, text=label, style="Tab.TButton", width=0, command=lambda key=key: self.show(key))
            self.buttons[key].pack(side="left", padx=(0, 4))
        self.show(items[0][0])

    def __getitem__(self, key):
        return self.frames[key]

    def show(self, key):
        for name, frame in self.frames.items():
            frame.grid() if name == key else frame.grid_remove()
            self.buttons[name].configure(style="TabOn.TButton" if name == key else "Tab.TButton")
        current = getattr(self.window, "current", None)
        if current in self.window.canvases:
            self.window.canvases[current].yview_moveto(0)
            self.window.rewrap()


class Window:
    def __init__(self, root, page="dashboard"):
        self.root = root
        self.config = config.load()
        self.events = queue.Queue(maxsize=128)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-settings")
        self.stop = threading.Event()
        self.variables = {}
        self.pages, self.canvases, self.nav = {}, {}, {}
        self.recolor = []          # plain Tk widgets the theme colors by hand: (widget, {option: role})
        self.online = False
        self.loading = True        # values being shown, not changed
        self.save_timer = None
        self.toast_timer = None
        self.status = {}
        self.palette_setting = palettes.chosen("graphite")   # may be "system": follow Windows
        if self.palette_setting not in (*palettes.WINDOWS, palettes.SYSTEM):   # a theme this window no longer offers
            self.palette_setting = "graphite"
        self.palette = palettes.resolve(self.palette_setting)   # what the window is painted with
        self.shapes = None
        self.apps_loaded = False
        self.wrapped = []          # labels that wrap to their width
        root.title("Jarvis")
        root.geometry(f"{min(1060, root.winfo_screenwidth() - 60)}x{min(680, root.winfo_screenheight() - 80)}")
        root.minsize(860, 540)
        self.style = ttk.Style(root)
        self.family = "Segoe UI" if os.name == "nt" else tkfont.nametofont("TkDefaultFont").actual("family")
        self.apply_theme()
        root.bind("<Map>", lambda event: self.color_title_bar() if event.widget is root else None, add="+")

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        rail = ttk.Frame(body, style="Rail.TFrame", padding=(10, 14), width=200)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        self.brand(rail)
        self.controls(rail)
        self.nav_icons = {}   # page: (icon in the muted colour, icon in the text colour)
        for key, title in NAVIGATION:
            r = palettes.roles(self.palette)
            self.nav_icons[key] = (tk.BitmapImage(data=icon_bits(key), foreground=r["text-muted"]),
                                   tk.BitmapImage(data=icon_bits(key), foreground=r["text"]))
            button = ttk.Button(rail, text="  " + title, image=self.nav_icons[key][0], compound="left",
                                style="Nav.TButton", command=lambda key=key: self.show(key))
            button.pack(fill="x", pady=1)
            self.nav[key] = button
        self.content = ttk.Frame(body)
        self.content.pack(side="left", fill="both", expand=True)
        for key, title in NAVIGATION:
            getattr(self, "build_" + key)(self.page(key, title))
        self.toast()
        self.show(page)
        root.bind_all("<MouseWheel>", self.scroll)
        for widget_class in ("TCombobox", "TSpinbox"):
            root.bind_class(widget_class, "<MouseWheel>", self.scroll_not_choose)
        root.bind("<Control-m>", lambda _: self.toggle_mute())
        root.bind("<Control-space>", lambda _: self.listen())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.pump)
        self.loading = False
        self.refresh_status()
        threading.Thread(target=self.observe, name="jarvis-status", daemon=True).start()

    # Look ----------------------------------------------------------------

    def apply_theme(self):
        r = palettes.roles(self.palette)
        s, f = self.style, self.family
        for muted, bright in getattr(self, "nav_icons", {}).values():   # the sidebar icons follow the colours
            muted.configure(foreground=r["text-muted"])
            bright.configure(foreground=r["text"])
        if getattr(self, "toast_shown", False):
            self.draw_toast()
        if "clam" in s.theme_names():
            s.theme_use("clam")
        self.root.configure(background=r["bg"])
        s.configure(".", background=r["bg"], foreground=r["text"], fieldbackground=r["inset"],
                    bordercolor=r["line"], lightcolor=r["bg"], darkcolor=r["bg"], troughcolor=r["inset"],
                    focuscolor=r["focus"], selectbackground=r["selected"], selectforeground=r["text"],
                    insertcolor=r["text"], arrowcolor=r["text"], font=(f, 10))
        for name, background in (("TFrame", r["bg"]), ("Rail.TFrame", r["surface"]), ("Header.TFrame", r["surface"]),
                                 ("Card.TFrame", r["card"])):
            s.configure(name, background=background)
        labels = {"TLabel": (r["bg"], r["text"], (f, 10)), "Title.TLabel": (r["bg"], r["text"], (f, 20, "bold")),
                  "Subtitle.TLabel": (r["bg"], r["text-muted"], (f, 11)), "Group.TLabel": (r["bg"], r["text"], (f, 11, "bold")),
                  "GroupNote.TLabel": (r["bg"], r["text-muted"], (f, 10)), "Brand.TLabel": (r["surface"], r["text"], (f, 11, "bold")),
                  "RailEyebrow.TLabel": (r["surface"], r["accent-text"], (f, 8, "bold")),
                  "Card.TLabel": (r["card"], r["text"], (f, 10)), "Row.TLabel": (r["card"], r["text"], (f, 10, "bold")),
                  "Caption.TLabel": (r["card"], r["text-muted"], (f, 9)), "State.TLabel": (r["card"], r["text"], (f, 14, "bold")),
                  "Live.TLabel": (r["card"], r["live-text"], (f, 14, "bold")), "Eyebrow.TLabel": (r["card"], r["accent-text"], (f, 8, "bold")),
                  "Key.TLabel": (r["inset"], r["text"], ("Consolas", 9, "bold")),
                  "Pill.TLabel": (r["control"], r["text-muted"], (f, 9, "bold")),
                  "PillLive.TLabel": (r["live"], r["on-live"], (f, 9, "bold")),
                  "PillCalm.TLabel": (r["selected"], r["text"], (f, 9, "bold")),
                  "PillOff.TLabel": (r["error-bg"], r["error-text"], (f, 9, "bold"))}
        for name, (background, foreground, font) in labels.items():
            s.configure(name, background=background, foreground=foreground, font=font)
        for name in ("Pill.TLabel", "PillLive.TLabel", "PillCalm.TLabel", "PillOff.TLabel"):
            s.configure(name, padding=(10, 3))
        s.configure("Key.TLabel", padding=(8, 2))
        buttons = {"TButton": (r["control"], r["text"], r["line-strong"], r["control-hover"]),
                   "Primary.TButton": (r["accent"], r["on-accent"], r["accent"], r["accent-hover"]),
                   "Danger.TButton": (r["error-bg"], r["error-text"], r["error-text"], r["error-bg"]),
                   "Tab.TButton": (r["bg"], r["text-muted"], r["bg"], r["control"]),
                   "TabOn.TButton": (r["control"], r["text"], r["control"], r["control"]),
                   "Nav.TButton": (r["surface"], r["text-muted"], r["surface"], r["control"]),
                   "NavOn.TButton": (r["selected"], r["text"], r["selected"], r["selected"]),
                   "Header.TButton": (r["control"], r["text"], r["line-strong"], r["control-hover"]),
                   "Muted.TButton": (r["live-soft"], r["live-text"], r["live"], r["live-soft"])}
        for name, (background, foreground, border, hover) in buttons.items():
            s.configure(name, background=background, foreground=foreground, bordercolor=border, lightcolor=background,
                        darkcolor=background, relief="flat", padding=(12, 4), font=(f + " Semibold", 10))
            s.map(name, background=[("disabled", r["disabled"]), ("pressed", hover), ("active", hover)],
                  foreground=[("disabled", r["text-disabled"])], bordercolor=[("focus", r["focus"])])
        s.configure("Nav.TButton", anchor="w", padding=(12, 6), borderwidth=0)
        s.configure("NavOn.TButton", anchor="w", padding=(12, 6), borderwidth=0)
        for name in ("TCheckbutton", "TRadiobutton"):
            s.configure(name, background=r["card"], foreground=r["text"], indicatorbackground=r["inset"],
                        indicatorforeground=r["on-accent"], upperbordercolor=r["line-strong"], lowerbordercolor=r["line-strong"],
                        font=(f, 10))
            s.map(name, background=[("active", r["card"])], indicatorbackground=[("selected", r["accent"]), ("active", r["control"])],
                  foreground=[("disabled", r["text-disabled"])])
        s.configure("Swatch.TRadiobutton", font=(f, 10, "bold"))
        # Quality tiles: a picked tile takes the selected colour and an accent edge.
        for on, background, edge in (("", r["card"], r["line-strong"]), ("On", r["selected"], r["accent"])):
            s.configure(f"Tile{on}.TFrame", background=background, bordercolor=edge, lightcolor=edge, darkcolor=edge,
                        relief="solid", borderwidth=2)
            s.configure(f"TileName{on}.TLabel", background=background, foreground=r["text"], font=(f, 11, "bold"))
            s.configure(f"TileNote{on}.TLabel", background=background, foreground=r["text"] if on else r["text-muted"],
                        font=(f, 9))
        for name in ("TEntry", "TSpinbox", "TCombobox"):
            s.configure(name, fieldbackground=r["inset"], foreground=r["text"], background=r["control"],
                        bordercolor=r["line-strong"], lightcolor=r["inset"], darkcolor=r["inset"], arrowcolor=r["text"],
                        insertcolor=r["text"], padding=5)
            s.map(name, bordercolor=[("focus", r["accent"])], fieldbackground=[("disabled", r["disabled"]), ("readonly", r["control"])],
                  foreground=[("disabled", r["text-disabled"])])
        s.map("TCombobox", selectbackground=[("readonly", r["control"])], selectforeground=[("readonly", r["text"])])
        for option, role in (("background", "card"), ("foreground", "text"), ("selectBackground", "selected"),
                             ("selectForeground", "text")):
            self.root.option_add(f"*TCombobox*Listbox.{option}", r[role])
        s.configure("Treeview", background=r["inset"], fieldbackground=r["inset"], foreground=r["text"],
                    bordercolor=r["line"], rowheight=26)
        s.map("Treeview", background=[("selected", r["selected"])], foreground=[("selected", r["text"])])
        s.configure("Treeview.Heading", background=r["control"], foreground=r["text"], relief="flat", font=(f, 9, "bold"))
        s.configure("Horizontal.TScale", background=r["accent"], troughcolor=r["inset"], bordercolor=r["line"])
        s.configure("TSeparator", background=r["line"], lightcolor=r["line"], darkcolor=r["line"], bordercolor=r["line"])
        s.configure("TMenubutton", background=r["control"], foreground=r["text"], arrowcolor=r["text-muted"],
                    padding=(12, 4), relief="flat", font=(f + " Semibold", 10))
        s.map("TMenubutton", background=[("active", r["control-hover"])])
        s.configure("Glyph.TLabel", background=r["control"], foreground=r["accent-text"], font=("Segoe MDL2 Assets", 13), padding=(9, 8))
        s.configure("CardTitle.TLabel", background=r["card"], foreground=r["text"], font=(f + " Semibold", 11))
        s.configure("RailName.TLabel", background=r["surface"], foreground=r["text"], font=(f, 15, "bold"))
        s.configure("RailNote.TLabel", background=r["surface"], foreground=r["text-muted"], font=(f, 8))
        s.configure("RailLogo.TLabel", background=r["surface"])
        s.configure("Panel.TFrame", background=r["card"])
        s.configure("Title.TLabel", font=(f + " Semibold", 20))
        s.configure("State.TLabel", font=(f + " Semibold", 15))
        s.configure("Live.TLabel", font=(f + " Semibold", 15))
        self.shapes = look.apply(s, self.root, r)   # the previous theme's pictures can go now
        self.logo = look.image(self.root, look.logo_png(44, r["surface"], r["accent"], r["text"]))
        self.icon = look.image(self.root, look.logo_png(32, r["surface"], r["accent"], r["text"]))
        self.root.iconphoto(True, self.icon)
        if self.root.winfo_ismapped():
            self.color_title_bar()
        if hasattr(self, "logo_label"):
            self.logo_label.configure(image=self.logo)
        if hasattr(self, "home"):
            self.home.paint()
        for widget, options in self.recolor:
            try:
                widget.configure(**{option: r[role] for option, role in options.items()})
            except tk.TclError:
                pass   # destroyed

    def color_title_bar(self):
        r = palettes.roles(self.palette)
        look.title_bar(self.root, r["surface"], r["text"], palettes.ALL_PRESETS[self.palette]["dark"])

    def choose_palette(self, name):
        if self.loading or name == self.palette_setting:
            return
        self.palette_setting = name
        self.palette = palettes.resolve(name)
        self.apply_theme()
        try:
            palettes.choose(name)
        except OSError as error:
            self.say(f"The colors changed, but couldn't be kept for next time: {error}", error=True)
            return
        # Graphite and the new themes aren't in PRESETS (the book's): this raised for them.
        label = "Like Windows" if name == palettes.SYSTEM else palettes.ALL_PRESETS[name]["name"]
        self.say(f"Colors: {label}.")

    def tk_widget(self, widget, **roles):
        """Register a plain Tk widget so theme changes recolor it."""
        self.recolor.append((widget, roles))
        widget.configure(**{option: palettes.roles(self.palette)[role] for option, role in roles.items()})
        return widget

    # Layout --------------------------------------------------------------

    def brand(self, rail):
        top = ttk.Frame(rail, style="Rail.TFrame")
        top.pack(fill="x", padx=4, pady=(0, 14))
        self.logo_label = ttk.Label(top, image=self.logo, style="RailLogo.TLabel")
        self.logo_label.pack(side="left", padx=(0, 10))
        names = ttk.Frame(top, style="Rail.TFrame")
        names.pack(side="left")
        ttk.Label(names, text="JARVIS", style="RailName.TLabel").pack(anchor="w")
        ttk.Label(names, text="Voice control", style="RailNote.TLabel").pack(anchor="w")

    def controls(self, rail):
        """Status, Mute and the menu, always in the same corner."""
        bar = ttk.Frame(rail, style="Rail.TFrame")
        bar.pack(side="bottom", fill="x")
        pills = ttk.Frame(bar, style="Rail.TFrame")
        pills.pack(fill="x", pady=(0, 8))
        self.pill = ttk.Label(pills, text="Offline", style="PillOff.TLabel")
        self.pill.pack(anchor="w")
        self.replay_pill = ttk.Label(pills, text="Replay on", style="PillLive.TLabel")
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Export settings…", command=self.export_settings)
        menu.add_command(label="Import settings…", command=self.import_settings)
        menu.add_command(label="Open backups folder", command=self.open_backups)
        menu.add_separator()
        menu.add_command(label="Quit Jarvis…", command=self.quit_jarvis)
        self.tk_widget(menu, background="card", foreground="text", activebackground="selected", activeforeground="text")
        self.mute_button = ttk.Button(bar, text="Mute", style="Header.TButton", command=self.toggle_mute)
        self.mute_button.pack(fill="x", pady=(0, 6))
        ttk.Menubutton(bar, text="Menu", menu=menu).pack(fill="x")

    def page(self, key, title):
        frame = ttk.Frame(self.content)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        canvas = self.tk_widget(tk.Canvas(frame, highlightthickness=0, borderwidth=0), background="bg")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        def scrolled(first, last, bar=scrollbar):
            bar.set(first, last)
            bar.configure(style="Quiet.Vertical.TScrollbar" if float(first) <= 0 and float(last) >= 1 else "Vertical.TScrollbar")
        canvas.configure(yscrollcommand=scrolled)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        inner = ttk.Frame(canvas, padding=(28, 18, 28, 24))
        item = canvas.create_window(0, 0, anchor="n", window=inner)

        def region(view=canvas, content=inner):
            # Never shorter than the view: a short page (Voice) otherwise slid
            # down, leaving a gap above its title.
            view.configure(scrollregion=(0, 0, view.winfo_width(), max(content.winfo_reqheight(), view.winfo_height())))
        inner.bind("<Configure>", lambda _: region())
        def fit(event, view=canvas, item=item):
            width = min(event.width, 1000)   # lines stay readable in a wide window
            view.itemconfigure(item, width=width)
            view.coords(item, event.width / 2, 0)
            region()
        canvas.bind("<Configure>", fit)
        inner.columnconfigure(0, weight=1)
        ttk.Label(inner, text=title, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.pages[key] = frame
        self.canvases[key] = canvas
        inner.next_row = 1
        return inner

    def tabs(self, page, items):
        bar = ttk.Frame(page)
        bar.grid(row=page.next_row, column=0, sticky="w", pady=(10, 0))
        holder = ttk.Frame(page)
        holder.grid(row=page.next_row + 1, column=0, sticky="ew")
        holder.columnconfigure(0, weight=1)
        page.next_row += 2
        return Tabs(self, bar, holder, items)

    def subtitle(self, page, text):
        self.wrapping(ttk.Label(page, text=text, style="Subtitle.TLabel", justify="left")).grid(
            row=page.next_row, column=0, sticky="ew", pady=(4, 8))
        page.next_row += 1

    def show(self, key):
        key = PAGE_ALIASES.get(key, key)
        key = key if key in self.pages else "dashboard"
        for name, frame in self.pages.items():
            frame.pack_forget()
            self.nav[name].configure(style="NavOn.TButton" if name == key else "Nav.TButton",
                                     image=self.nav_icons[name][1 if name == key else 0])
        self.pages[key].pack(fill="both", expand=True)
        self.current = key
        self.rewrap()
        self.load_apps()
        if hasattr(self, "sound_watch"):
            self.watch_sound_apps(key == "recording")

    def load_apps(self):
        """Fill the app list when first seen; otherwise it stays empty until someone finds Refresh."""
        if getattr(self, "current", None) == "commands" and self.online and not self.apps_loaded:
            self.apps_loaded = True
            self.call("apps", self.show_apps)
        # The displays, by name, the first time the Capture page is open.
        if getattr(self, "current", None) == "recording" and self.online and not getattr(self, "displays_loaded", False):
            self.displays_loaded = True
            self.call("monitors", lambda result: self.load_displays(result, quiet=True))
        # The microphones, by name, the first time the Voice page is open.
        if getattr(self, "current", None) == "voice" and self.online and not getattr(self, "inputs_loaded", False):
            self.inputs_loaded = True
            self.call("devices", lambda result: self.load_inputs(result, quiet=True))

    def scroll(self, event):
        if not isinstance(event.widget, (ttk.Treeview, tk.Text)):   # lists and text scroll themselves
            self.canvases[self.current].yview_scroll(-int(event.delta / 120), "units")

    def scroll_not_choose(self, event):
        """The wheel over a dropdown or number box scrolls the page: by default Tk
        changed the setting under the pointer while you only scrolled past it."""
        self.scroll(event)
        return "break"

    def wrapping(self, label):
        """A label that wraps to its width, once it can be seen.

        A hidden page has no width of its own: its text and its size would chase
        each other forever. Showing a page or a tab wraps its text again.
        """
        self.wrapped.append(label)
        label.bind("<Configure>", lambda _: self.wrap(label))
        return label

    def wrap(self, label):
        if label.winfo_viewable():
            width = max(200, label.winfo_width() - 4)
            if width != label.cget("wraplength"):
                label.configure(wraplength=width)

    def rewrap(self):
        self.root.update_idletasks()   # labels just shown need their width first
        for label in self.wrapped:
            self.wrap(label)

    def group(self, page, title, note=""):
        """A titled card of setting rows; returns the card."""
        if title:
            ttk.Label(page, text=title, style="Group.TLabel").grid(row=page.next_row, column=0, sticky="w", pady=(20, 4))
            page.next_row += 1
        if note:
            self.wrapping(ttk.Label(page, text=note, style="GroupNote.TLabel", justify="left")).grid(
                row=page.next_row, column=0, sticky="ew", pady=(0, 6))
            page.next_row += 1
        card = ttk.Frame(page, style="Panel.TFrame", padding=(18, 6))
        card.grid(row=page.next_row, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)
        card.next_row = 0
        page.next_row += 1
        return card

    def line(self, card, *widgets_or_text, caption=False):
        """Free-form content in a card: text, or widgets side by side."""
        self.divider(card)
        holder = ttk.Frame(card, style="Card.TFrame", borderwidth=0)
        holder.grid(row=card.next_row, column=0, columnspan=2, sticky="ew", pady=10)
        card.next_row += 1
        for item in widgets_or_text:
            if isinstance(item, str):
                self.wrapping(ttk.Label(holder, text=item, style="Caption.TLabel" if caption else "Card.TLabel",
                                        justify="left")).pack(side="left", fill="x", expand=True)
            else:
                item.pack(in_=holder, side="left", padx=(0, 8))
                item.lift(holder)   # made before its holder: raise it, or Tk draws it underneath
        return holder

    def wide(self, card, widget):
        """One widget across the whole card. Packing it through a holder hangs Tk inside a hidden tab."""
        self.divider(card)
        widget.grid(row=card.next_row, column=0, columnspan=2, sticky="ew", pady=10)
        card.next_row += 1
        return widget

    def divider(self, card):
        if card.next_row and not getattr(card, "after_heading", False):
            ttk.Separator(card).grid(row=card.next_row, column=0, columnspan=2, sticky="ew")
            card.next_row += 1
        card.after_heading = False

    def row(self, card, title, widget, detail=""):
        self.divider(card)
        labels = ttk.Frame(card, style="Card.TFrame", borderwidth=0)
        labels.grid(row=card.next_row, column=0, sticky="ew", pady=10, padx=(0, 16))
        ttk.Label(labels, text=title, style="Row.TLabel").pack(anchor="w")
        if detail:
            self.wrapping(ttk.Label(labels, text=detail, style="Caption.TLabel", justify="left")).pack(fill="x", anchor="w")
        widget.grid(in_=card, row=card.next_row, column=1, sticky="e", pady=10)
        card.next_row += 1
        return widget

    def variable(self, section, key):
        identity = (section, key)
        if identity not in self.variables:
            value = self.config[section][key] if section else self.config[key]
            cls = tk.BooleanVar if type(value) is bool else tk.StringVar
            self.variables[identity] = cls(value=value)
        return self.variables[identity]

    def switch(self, card, title, section, key, detail=""):
        check = ttk.Checkbutton(card, variable=self.variable(section, key), command=lambda: self.changed(400))
        return self.row(card, title, check, detail)

    def text(self, card, title, section=None, key=None, detail="", variable=None, width=28):
        entry = ttk.Entry(card, textvariable=variable or self.variable(section, key), width=width)
        entry.bind("<Return>", lambda _: self.changed(0))
        entry.bind("<FocusOut>", lambda _: self.changed(0))
        return self.row(card, title, entry, detail)

    def number(self, card, title, section, key, low, high, step, detail=""):
        spin = ttk.Spinbox(card, from_=low, to=high, increment=step, width=8, textvariable=self.variable(section, key),
                           command=lambda: self.changed(800))
        spin.bind("<Return>", lambda _: self.changed(0))
        spin.bind("<FocusOut>", lambda _: self.changed(0))
        return self.row(card, title, spin, detail)

    def choice(self, card, title, section, key, choices, detail=""):
        variable = self.variable(section, key)
        display = tk.StringVar(value=next((name for name, value in choices.items() if value == variable.get()), variable.get()))
        display.trace_add("write", lambda *_: variable.set(choices.get(display.get(), display.get())))
        box = ttk.Combobox(card, textvariable=display, values=tuple(choices), state="readonly", width=26)
        box.bind("<<ComboboxSelected>>", lambda _: self.changed(300))
        return self.row(card, title, box, detail)

    def buttons(self, card, items):
        frame = ttk.Frame(card, style="Card.TFrame", borderwidth=0)
        for label, command, *style in items:
            ttk.Button(frame, text=label, command=command, style=(style[0] if style else "TButton")).pack(side="left", padx=(0, 8))
        self.divider(card)
        frame.grid(row=card.next_row, column=0, columnspan=2, sticky="w", pady=10)
        card.next_row += 1
        return frame

    def toast(self):
        """A rounded message at the bottom: a green edge and ✓ when something worked, red and ! when it didn't.

        It's a borderless window of its own whose corners Windows leaves see-through:
        a widget inside the main window can't be transparent, so its corners would
        show as a box. Made once, kept hidden, and moved with the main window."""
        self.toast_window = tk.Toplevel(self.root)
        self.toast_window.withdraw()
        self.toast_window.overrideredirect(True)
        self.toast_tied = False   # tied to the main window when first shown: tied before that window is on screen, it never shows
        try:
            self.toast_window.attributes("-transparentcolor", SEE_THROUGH)
        except tk.TclError:   # not Windows: the corners show the key colour's neighbour, the page
            pass
        self.bubble = tk.Canvas(self.toast_window, highlightthickness=0, borderwidth=0, background=SEE_THROUGH)
        self.bubble.pack()
        self.notice = tk.StringVar()
        self.toast_error = self.toast_shown = self.toast_waiting = False
        self.bubble.bind("<Button-1>", lambda _: self.hide_toast())   # a click anywhere on it, ✕ included, closes it
        # Follow the window. A tag only the main window carries: binding on the window itself
        # would also run for every widget inside it that changes size.
        self.root.bindtags((*self.root.bindtags(), "JarvisToast"))
        for sequence in ("<Configure>", "<Map>"):
            self.root.bind_class("JarvisToast", sequence, lambda _: self.place_toast())

    def place_toast(self, show=False):
        """Keep the message at the bottom middle of the window; `show` when a message arrives.

        Moving, resizing, minimising and restoring only move it: Windows hides and brings back a
        window's own windows with it, and showing it here too would fight that. Raising isn't done
        here either: a raise counts as a change and would call this again, forever."""
        if not self.toast_shown:
            return
        if not self.root.winfo_ismapped():   # minimised: show it when the window is back
            self.toast_waiting = self.toast_waiting or show
            return
        width, height = int(self.bubble.cget("width")), int(self.bubble.cget("height"))
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + self.root.winfo_height() - height - 18
        where = f"{width}x{height}+{x}+{y}"
        if self.toast_window.geometry() != where:
            self.toast_window.geometry(where)
        if show or self.toast_waiting:
            self.toast_waiting = False
            if not self.toast_tied:
                self.toast_window.transient(self.root)   # owned by the main window, so Windows keeps it above it
                self.toast_tied = True
            self.toast_window.deiconify()

    def draw_toast(self):
        r, c, f = palettes.roles(self.palette), self.bubble, self.family
        light = palettes.luminance(r["card"]) > 0.4
        edge = ("#c62828" if light else "#ff6b6b") if self.toast_error else ("#137a47" if light else "#3dd68c")
        c.delete("all")
        words = c.create_text(48, 0, text=self.notice.get(), font=(f, 10, "bold"), fill=r["text"], anchor="w", width=560)
        x0, y0, x1, y1 = c.bbox(words)
        height = max(44, y1 - y0 + 22)
        width = x1 + 44
        c.configure(width=width, height=height)
        a, b, w, h = 2, 2, width - 2, height - 2   # a capsule: two half circles joined by straight edges
        d = h - b
        for box in ((a, b, a + d, h), (w - d, b, w, h)):
            c.create_oval(*box, fill=r["card"], outline="")
        c.create_rectangle(a + d / 2, b, w - d / 2, h, fill=r["card"], outline="")
        c.create_arc(a, b, a + d, h, start=90, extent=180, style="arc", outline=edge, width=2, tags="edge")
        c.create_arc(w - d, b, w, h, start=-90, extent=180, style="arc", outline=edge, width=2, tags="edge")
        for y in (b, h):
            c.create_line(a + d / 2, y, w - d / 2, y, fill=edge, width=2, tags="edge")
        c.tag_raise(words)
        c.coords(words, 48, height / 2)
        c.create_oval(16, height / 2 - 11, 38, height / 2 + 11, fill=edge, outline=edge)
        c.create_text(27, height / 2, text="!" if self.toast_error else "✓", font=(f, 11, "bold"), fill=r["card"])
        c.create_text(width - 22, height / 2, text="✕", font=(f, 10), fill=r["text-muted"], tags="close")

    def say(self, text, error=False):
        """Results fade after a few seconds; errors stay until dismissed or replaced."""
        if self.stop.is_set() or not text:
            return
        self.notice.set(text)
        self.toast_error = error
        self.draw_toast()
        self.toast_shown = True
        self.place_toast(show=True)
        if self.toast_timer:
            self.root.after_cancel(self.toast_timer)
            self.toast_timer = None
        if not error:
            self.toast_timer = self.root.after(min(9000, 2500 + 45 * len(text)), self.hide_toast)

    def hide_toast(self):
        self.toast_timer = None
        self.toast_shown = self.toast_waiting = False
        self.toast_window.withdraw()

    # Service -------------------------------------------------------------

    def post(self, event):
        # Bounded backpressure while a native dialog blocks Tk. Closing the
        # window must also release a producer, including the executor thread.
        while not self.stop.is_set():
            try:
                self.events.put(event, timeout=0.1)
                return
            except queue.Full:
                continue

    def submit(self, func, callback=None):
        def run():
            try:
                result = func()
                self.post((callback, result, None))
            except Exception as exc:
                self.post((None, None, str(exc)))

        self.executor.submit(run)

    def call(self, op, callback=None, **payload):
        def run():
            # Longer per-operation limits (display inventory, commands,
            # recording start) apply; others keep at least 15 seconds.
            reply = request(
                op,
                timeout=60
                if op in ("record_stop", "replay_save", "save_config")
                else max(15, TIMEOUTS.get(op, 0)),
                **payload,
            )
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error", "Jarvis request failed"))
            return reply

        self.submit(run, callback or self.received)

    def pump(self):
        try:
            for _ in range(100):
                callback, result, error = self.events.get_nowait()
                if error:
                    self.say(error, error=True)
                elif callback:
                    callback(result)
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.root.after(100, self.pump)

    def observe(self):
        delay = 1
        while not self.stop.is_set():
            try:
                watch(lambda event: self.post((self.received, event, None)), self.stop)
                delay = 1
            except (OSError, ValueError, RuntimeError):
                self.post((self.disconnected, None, None))
            self.stop.wait(delay)
            delay = min(30, delay * 2)

    def disconnected(self, _):
        self.online = False
        self.pill.configure(text="Offline", style="PillOff.TLabel")
        self.home.went_offline()

    def received(self, reply):
        status = reply.get("status")
        if not status:
            if reply.get("message"):
                self.say(str(reply["message"]))
            return
        self.status = status
        self.voice_controls.status(status)
        self.online = True
        self.load_apps()
        self.home.update(status)
        state = status.get("state", "paused")
        muted = bool(status.get("muted", True))
        pills = {"paused": ("Muted", "Pill.TLabel"), "idle": ("Ready", "PillCalm.TLabel"),
                 "listening": ("Listening", "PillLive.TLabel"), "processing": ("Working", "PillLive.TLabel"),
                 "dictating": ("Dictating", "PillLive.TLabel"), "error": ("Needs attention", "PillOff.TLabel")}
        text, style = pills.get(state, (state.capitalize(), "Pill.TLabel"))
        self.pill.configure(text=text, style=style)
        wake = str(self.config["activation"].get("wake_phrase", "jarvis")).title()
        titles = {"paused": "Microphone muted", "idle": f"Waiting for “{wake}”", "listening": "Listening…",
                  "processing": "Working on it", "dictating": "Dictating", "error": "Voice needs attention"}
        self.voice_state.set(titles.get(state, state.capitalize()))
        self.voice_title.configure(style="Live.TLabel" if state in ("listening", "processing", "dictating") else "State.TLabel")
        message = str(status.get("message", ""))
        if state == "idle" and message.startswith("Waiting for"):
            message = "say its name, then a command"   # the title says the rest
        self.voice_message.set(("Microphone on · " if status.get("microphone_open") else "Microphone off · ") + message)
        self.mute_button.configure(text="Unmute" if muted else "Mute", style="Muted.TButton" if muted else "Header.TButton")
        recording, replay = status.get("recording", {}), status.get("replay", {})
        self.record_state.set(str(recording.get("message") or "Not recording"))
        self.replay_state.set(str(replay.get("message") or "Replay off"))
        if replay.get("state") == "on":
            self.replay_pill.pack(anchor="w", pady=(4, 0))
        else:
            self.replay_pill.pack_forget()
        self.onboard_card.grid() if not self.config["privacy"]["onboarded"] else self.onboard_card.grid_remove()

    def refresh_status(self):
        def run():
            try:
                return request("status", timeout=1)
            except (OSError, ValueError):
                return {}

        self.submit(run, lambda reply: self.received(reply) if reply.get("ok") else self.disconnected(None))

    def start_recording(self):
        if not self.online:
            self.say("Start Jarvis first.", error=True)
            return
        try:
            value = self.settings()
            edited = value != self.config
            value['recording']['configured'] = True
            config.validate(value)
        except ValueError as error:
            self.say(str(error), error=True)
            return
        def run():
            # Pressing Start changes nothing else: unedited, this window's copy may be
            # older than the service's, and must not overwrite it.
            wanted = value
            if not edited:
                wanted = request('config', timeout=5)['config']
                if wanted['recording']['configured']:
                    return wanted, request('record_start', timeout=20)
                wanted['recording']['configured'] = True
            saved = save_config_restarting(wanted)
            if not saved.get('ok'):
                raise ValueError(saved.get('error', 'Could not save recording settings'))
            return wanted, request('record_start', timeout=20)
        def done(result):
            saved, started = result
            self.config = saved
            self.variable('recording', 'configured').set(True)
            self.received(started)
            self.say(started.get('error') or started.get('message') or 'Recording started.', error=not started.get('ok'))
        self.submit(run, done)

    def toggle_mute(self):
        if self.status.get("muted", True) and not self.config["privacy"]["onboarded"]:
            self.show("dashboard")
            self.say("Allow the microphone on Home first.", error=True)
            return
        self.call("mute", muted=not bool(self.status.get("muted", True)))

    def listen(self):
        if not self.config["privacy"]["onboarded"]:
            self.show("dashboard")
            self.say("Allow the microphone on Home first.", error=True)
            return
        self.call("cancel" if self.status.get("state") == "listening" else "listen")

    # Pages ---------------------------------------------------------------

    def build_dashboard(self, p):
        self.record_state = tk.StringVar(value="Not recording")   # also shown on Capture
        self.replay_state = tk.StringVar(value="Replay off")
        self.home = Home(self, p, PRIVACY_FACTS)

    def onboard(self):
        if not self.consent.get():
            self.say("Tick “I understand when Jarvis opens the microphone” first.", error=True)
            return
        self.variable("privacy", "onboarded").set(True)
        self.variable("activation", "mode").set("wake_word")
        self.save(success="Microphone allowed. The wake word is on.")

    def build_voice(self, p):
        tabs = self.tabs(p, [("wake", "Wake word"), ("language", "Language"), ("dictation", "Dictation"),
                             ("tuning", "Fine-tuning")])
        wake = self.group(tabs["wake"], "Wake word")
        self.choice(wake, "Listen", "activation", "mode", {"When I say the wake word": "wake_word", "Only when I press Listen": "push_to_talk"},
                    "The wake word keeps the microphone open while unmuted; push to talk opens it for one command.")
        self.text(wake, "Wake name", "activation", "wake_phrase", detail="Short, and unlike the words in your games.")
        self.wake_aliases = tk.StringVar(value=", ".join(self.config["activation"]["wake_aliases"]))
        self.text(wake, "Other names", variable=self.wake_aliases, detail="Separate with commas; up to eight names in all.")

        from .voice_controls import VoiceControls
        self.voice_controls = VoiceControls(self, tabs["language"], tabs["dictation"])

        microphone = self.group(tabs["wake"], "Microphone")
        # The list shows each microphone's own name; the setting keeps Windows's
        # "input:" id for it, which the voice worker looks the device up by.
        self.mic_ids = {DEFAULT_MICROPHONE: ""}
        self.variable("audio", "device")   # saved with the other settings
        self.mic_name = tk.StringVar(value=microphone_name(self.config["audio"]["device"]))
        self.mic_choice = ttk.Combobox(microphone, textvariable=self.mic_name, state="readonly", width=30,
                                       values=list(dict.fromkeys([DEFAULT_MICROPHONE, self.mic_name.get()])))
        self.mic_choice.bind("<<ComboboxSelected>>", lambda _: self.pick_microphone())
        self.row(microphone, "Input", self.mic_choice, f"{DEFAULT_MICROPHONE} follows your Windows setting.")
        self.buttons(microphone, [("Refresh inputs", lambda: self.call("devices", self.load_inputs)),
                                  ("Test microphone", lambda: self.call("mic_test", enabled=True)),
                                  ("End test", lambda: self.call("mic_test", enabled=False))])

        recognition = self.group(tabs["tuning"], "Recognition")
        self.switch(recognition, "Accurate command model", "recognition", "accurate", "Whisper, offline. Off saves memory.")
        self.choice(recognition, "Command model", "recognition", "speech_model",
                    {"Small (most accurate)": "small", "Light (less memory)": "light"}, "Light uses about half the memory.")
        self.switch(recognition, "Closest-match guessing", "recognition", "closest_match", "Run the nearest command when one isn't heard exactly.")
        self.switch(recognition, "Noise suppression", "recognition", "noise_suppression", "For steady noise such as fans; experimental.")
        self.switch(recognition, "Ignore background sound", "audio", "adaptive_gate", "Only a voice close to the microphone counts.")

        tuning = self.group(tabs["tuning"], "Fine-tuning", "The defaults suit most rooms. Change one at a time, then test with your own voice.")
        self.number(tuning, "Longest command (seconds)", "activation", "command_timeout", 2, 15, 1)
        self.number(tuning, "Wake sensitivity", "activation", "sensitivity", 0, 1, 0.05, "Higher hears quieter wake names, but triggers more often.")
        self.number(tuning, "Minimum command score", "activation", "confidence", 0, 1, 0.05, "Lower accepts more, including mistakes.")

    def load_inputs(self, result, quiet=False):
        self.mic_ids = {DEFAULT_MICROPHONE: ""}
        for item in result["devices"]:
            if item["id"]:
                self.mic_ids[microphone_name(item["id"])] = item["id"]
        names = list(self.mic_ids)
        if self.mic_name.get() not in names:   # the saved one is unplugged: still show it
            names.append(self.mic_name.get())
        self.mic_choice.configure(values=names)
        if not quiet:
            self.say(f"Found {len(self.mic_ids) - 1} microphones. Pick one from the Input list.")

    def pick_microphone(self):
        name = self.mic_name.get()
        self.variable("audio", "device").set(self.mic_ids.get(name, "" if name == DEFAULT_MICROPHONE else f"input:{name}"))
        self.changed(300)

    def build_commands(self, p):
        tabs = self.tabs(p, [("apps", "Apps"), ("say", "What you can say"), ("shortcuts", "Your shortcuts")])
        self.build_applications(tabs["apps"])
        say = self.group(tabs["say"], "Say this", "After your wake name.")
        for group in REFERENCE:
            self.row(say, group["title"], ttk.Label(say, text=" · ".join(f"“{e}”" for e in group["examples"][:2]), style="Caption.TLabel"))
        custom = self.group(tabs["shortcuts"], "Your shortcuts", "Map a phrase of your own to a built-in command. Shell commands are never run.")
        self.commands = dict(self.config["commands"])
        self.shortcut_tree = ttk.Treeview(custom, columns=("phrase", "command"), show="headings", height=4)
        self.shortcut_tree.heading("phrase", text="You say")
        self.shortcut_tree.heading("command", text="Jarvis runs")
        self.wide(custom, self.shortcut_tree)
        self.new_phrase, self.new_command = tk.StringVar(), tk.StringVar()
        self.line(custom, ttk.Label(custom, text="You say", style="Card.TLabel"), ttk.Entry(custom, textvariable=self.new_phrase, width=20),
                  ttk.Label(custom, text="Jarvis runs", style="Card.TLabel"), ttk.Entry(custom, textvariable=self.new_command, width=20))
        self.buttons(custom, [("Add shortcut", self.add_shortcut, "Primary.TButton"), ("Remove selected", self.remove_shortcut)])
        self.show_shortcuts()
        apps = self.group(tabs["say"], "Every app", "Open and close phrases for every installed app.")
        self.application_commands = self.tk_widget(tk.Text(apps, height=8, wrap="word", state="disabled", relief="flat",
                                                           borderwidth=0), background="inset", foreground="text")
        self.wide(apps, self.application_commands)

    def show_shortcuts(self):
        self.shortcut_tree.delete(*self.shortcut_tree.get_children())
        for phrase, command in sorted(self.commands.items()):
            self.shortcut_tree.insert("", "end", iid=phrase, values=(phrase, command))

    def add_shortcut(self):
        phrase, command = " ".join(self.new_phrase.get().lower().split()), " ".join(self.new_command.get().lower().split())
        if not phrase or not command:
            self.say("Type both your phrase and the built-in command it runs.", error=True)
            return
        self.commands[phrase] = command
        self.new_phrase.set("")
        self.new_command.set("")
        self.show_shortcuts()
        self.save(success="Shortcut saved.")

    def remove_shortcut(self):
        for phrase in self.shortcut_tree.selection():
            self.commands.pop(phrase, None)
        self.show_shortcuts()
        self.save(success="Removed.")

    def show_app_commands(self, result):
        self.application_commands.configure(state="normal")
        self.application_commands.delete("1.0", "end")
        text = "\n\n".join(row["title"] + "\n" + " · ".join(row["examples"]) for row in result.get("applications", []))
        self.application_commands.insert("1.0", text)
        self.application_commands.configure(state="disabled")

    def build_applications(self, p):
        apps = self.group(p, "Installed apps", "Select an app, type the name you'll say, then Add voice name. The "
                          "launch word goes before the name; clear it to say only the name.")
        self.app_filter = tk.StringVar()
        self.app_filter.trace_add("write", lambda *_: self.filter_apps())
        self.text(apps, "Search", variable=self.app_filter)
        self.apps_tree = ttk.Treeview(apps, columns=("name", "id"), displaycolumns=("name",), show="headings", height=6)
        self.apps_tree.heading("name", text="App")
        self.wide(apps, self.apps_tree)
        self.app_alias = tk.StringVar()
        self.launch_word = tk.StringVar(value="open")
        self.line(apps, ttk.Label(apps, text="Launch word", style="Card.TLabel"), ttk.Entry(apps, textvariable=self.launch_word, width=10),
                  ttk.Label(apps, text="Voice name", style="Card.TLabel"), ttk.Entry(apps, textvariable=self.app_alias, width=22))
        self.buttons(apps, [("Add voice name", self.assign_alias, "Primary.TButton"), ("Refresh apps", lambda: self.call("apps", self.show_apps)),
                            ("Add program…", self.add_program)])
        self.aliases = copy.deepcopy(self.config["aliases"])
        self.prefixes = copy.deepcopy(self.config["app_prefixes"])
        self.app_names, self.all_apps = {}, []
        names = self.group(p, "Your voice names", "Removing a name keeps the app installed.")
        self.alias_tree = ttk.Treeview(names, columns=("voice", "app"), show="headings", height=4)
        self.alias_tree.heading("voice", text="Say this")
        self.alias_tree.heading("app", text="App")
        self.wide(names, self.alias_tree)
        self.alias_tree.bind("<<TreeviewSelect>>", self.select_alias)
        self.buttons(names, [("Remove selected", self.remove_alias)])
        self.refresh_aliases()
        closing = self.group(p, "Close words")
        self.close_words = tk.StringVar(value=", ".join(self.config["close_words"]))
        self.text(closing, "Words that close an app", variable=self.close_words,
                  detail="Separate with commas, up to eight. Keep “close” in the list to still say it.")

    def filter_apps(self):
        needle = self.app_filter.get().casefold().strip()
        self.apps_tree.delete(*self.apps_tree.get_children())
        for app in self.all_apps:
            if needle in f"{app['name']} {app['id']}".casefold():
                self.apps_tree.insert("", "end", values=(app["name"], app["id"]))

    def refresh_aliases(self):
        self.alias_tree.delete(*self.alias_tree.get_children())
        for name, identity in sorted(self.aliases.items()):
            phrase = " ".join(filter(None, (self.prefixes.get(name, "open"), name)))
            self.alias_tree.insert("", "end", iid=name, values=(phrase, self.app_names.get(identity, identity)))

    def select_alias(self, _):
        selection = self.alias_tree.selection()
        if selection:
            name = selection[0]
            self.app_alias.set(name)
            self.launch_word.set(self.prefixes.get(name, "open"))

    def show_apps(self, result):
        self.all_apps = result["apps"]
        self.app_names = {app["id"]: app["name"] for app in result["apps"]}
        self.filter_apps()
        self.refresh_aliases()
        self.call("commands", self.show_app_commands)

    def add_program(self):
        """Add a portable program as a Start-menu shortcut (Start Menu\\Programs\\
        Jarvis), so Windows and Jarvis list it like an installed app. Programs
        found in the usual folders are offered first."""
        from .. import app_catalog

        self.submit(app_catalog.program_candidates, self.pick_program)

    def pick_program(self, candidates):
        if not candidates:
            self.browse_program()
            return
        dialog = self.tk_widget(tk.Toplevel(self.root), background="bg")
        dialog.title("Add a program")
        dialog.transient(self.root)
        ttk.Label(dialog, text="Programs found on the desktop, in Downloads and portable-app folders:").pack(
            anchor="w", padx=12, pady=(12, 6))
        listing = self.tk_widget(tk.Listbox(dialog, height=min(12, len(candidates)), width=90, relief="flat"),
                                 background="inset", foreground="text", selectbackground="selected", selectforeground="text")
        for path in candidates:
            listing.insert("end", str(path))
        listing.selection_set(0)
        listing.pack(fill="both", expand=True, padx=12)

        def choose(path=None):
            dialog.destroy()
            self.name_program(path) if path else self.browse_program()

        row = ttk.Frame(dialog)
        row.pack(anchor="e", padx=12, pady=12)
        ttk.Button(row, text="Add selected", style="Primary.TButton",
                   command=lambda: choose(str(candidates[listing.curselection()[0]]))
                   if listing.curselection() else None).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Browse…", command=choose).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Cancel", command=dialog.destroy).pack(side="left")
        listing.focus_set()

    def browse_program(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="Add a program",
            filetypes=[("Programs", "*.exe *.bat *.cmd *.lnk"), ("All files", "*.*")])
        if path:
            self.name_program(path)

    def name_program(self, path):
        name = simpledialog.askstring("Add a program", "Name to show and say:", initialvalue=Path(path).stem, parent=self.root)
        if not name:
            return

        def add():
            from .. import app_catalog
            from .system import com_context
            with com_context():
                return app_catalog.add(path, name)

        self.submit(add, lambda _: (self.say(f"Added {name}. Say “open {name}”."), self.call("apps", self.show_apps)))

    def assign_alias(self):
        selected = self.apps_tree.selection()
        name = " ".join(self.app_alias.get().lower().split())
        if not selected or not name:
            self.say("Select an app and type the name you'll say.", error=True)
            return
        self.aliases[name] = self.apps_tree.item(selected[0], "values")[1]
        self.prefixes[name] = self.launch_word.get().strip()
        self.refresh_aliases()
        wording = " ".join(filter(None, (self.prefixes[name], name)))
        self.save(success=f"Saved. Say “{str(self.config['activation']['wake_phrase']).capitalize()}, {wording}”.")

    def remove_alias(self):
        for name in self.alias_tree.selection():
            self.aliases.pop(name, None)
            self.prefixes.pop(name, None)
        self.refresh_aliases()
        self.save(success="Removed.")

    def build_recording(self, p):
        tabs = self.tabs(p, [("recording", "Recording"), ("replay", "Instant replay"), ("picture", "Picture and size")])

        now = self.group(tabs["recording"], "Now")
        self.wide(now, self.wrapping(ttk.Label(now, textvariable=self.record_state, style="Row.TLabel", justify="left")))
        controls = self.buttons(now, [("Start", self.home.toggle_recording), ("Folder", self.open_folder)])
        self.home.record_buttons.append(controls.winfo_children()[0])

        source = self.group(tabs["recording"], "Source and files", "Changes apply right away: a running recording is saved and continues in a new file.")
        # The list shows each display's own name; the setting keeps Windows's device
        # name (\\.\DISPLAY1), which capture finds the display by.
        saved = self.config["recording"]["monitor"]
        self.variable("recording", "monitor")   # saved with the other settings
        self.display_ids = {PRIMARY_DISPLAY: saved}   # until the displays are listed, moments after the page opens
        self.display_name = tk.StringVar(value=PRIMARY_DISPLAY)
        self.display_choice = ttk.Combobox(source, textvariable=self.display_name, state="readonly", width=44,
                                           values=[PRIMARY_DISPLAY])
        self.display_choice.bind("<<ComboboxSelected>>", lambda _: self.pick_display())
        self.row(source, "Display", self.display_choice)
        self.choice(source, "Color", "recording", "color_mode", {"Automatic": "auto", "SDR": "sdr", "HDR": "hdr"})
        self.text(source, "Save to", "recording", "output_dir")
        self.buttons(source, [("Refresh displays", lambda: self.call("monitors", self.load_displays)), ("Choose folder…", self.browse)])
        self.switch(source, "System sound", "recording", "system_audio", "The Windows default output.")
        self.switch(source, "Microphone", "recording", "microphone", "The Windows default input.")
        self.switch(source, "Folder for each game", "recording", "game_folders")
        self.switch(source, "Recording sounds", "recording", "sound_feedback")
        self.switch(source, "Voice commands can start recordings", "recording", "configured", "Turns on after your first recording.")

        now = self.group(tabs["replay"], "Now")
        self.wide(now, self.wrapping(ttk.Label(now, textvariable=self.replay_state, style="Row.TLabel", justify="left")))
        controls = self.buttons(now, [("Start", self.home.replay_action), ("Stop", lambda: self.call("replay_stop"))])
        self.home.replay_buttons.append(controls.winfo_children()[0])
        self.home.replay_stops.append(controls.winfo_children()[1])

        replay = self.group(tabs["replay"], "What it keeps", "The last moments of your display, ready to save as a clip. Changing these starts replay over, empty.")
        self.number(replay, "Length (seconds)", "replay", "seconds", 10, 1800, 10, "Up to 1800 (30 minutes).")
        self.number(replay, "Budget (MiB)", "replay", "max_mb", 64, 16384, 64, "The most it may hold; older seconds go first.")
        self.replay_fit = tk.StringVar()
        self.wide(replay, self.wrapping(ttk.Label(replay, textvariable=self.replay_fit, style="Caption.TLabel", justify="left")))
        self.switch(replay, "Clip sound", "replay", "sound", "A short chirp when a clip is saved.")
        self.switch(replay, "Start with Jarvis", "replay", "autostart", "Turn replay on whenever Jarvis starts.")

        # Like Medal's "Audio processes": all desktop sound, or only the sounds ticked
        # here, each with its own volume; programs appear as they play sound.
        sound = self.group(tabs["replay"], "Recording audio",
                           "For replay and recordings. Changing these starts replay over, empty.")
        self.choice(sound, "Audio processes", "replay", "sources", {"All audio": "all", "Specific apps": "specific"},
                    "All audio records everything you hear. Specific apps records only what you tick (Windows 11).")
        self.variable("replay", "sources").trace_add("write", lambda *_: self.show_sound_apps())
        for key in ("game_audio", "game_track", "microphone"):
            self.variable("replay", key)
        self.separate_app = tk.StringVar(value=", ".join(self.config["replay"]["separate_apps"]))
        self.volumes = dict(self.config["replay"].get("volumes", {}))
        self.sound_found, self.sound_rows, self.sound_app_labels = [], {}, {}
        self.sound_watch = None
        self.sound_apps = self.wide(sound, ttk.Frame(sound, style="Card.TFrame", borderwidth=0))
        self.sound_note = tk.StringVar()
        self.wide(sound, self.wrapping(ttk.Label(sound, textvariable=self.sound_note, style="Caption.TLabel", justify="left")))
        self.separate_tracks = tk.BooleanVar(value=self.config["replay"]["audio_tracks"] == "separate")
        self.variable("replay", "audio_tracks")
        separate = ttk.Checkbutton(sound, variable=self.separate_tracks, command=self.pick_separate_tracks)
        self.row(sound, "Separate audio tracks", separate,
                 "Each sound on its own track too, after one with them all, so you can mute one in Clips.")
        self.show_sound_apps()

        extra = self.group(tabs["replay"], "Storage")
        self.choice(extra, "Storage", "replay", "storage", {"RAM": "ram", "Disk (continuous writes)": "disk"},
                    "Disk writes about 1–10 GB an hour and needs a local NTFS or ReFS disk.")
        self.text(extra, "Disk folder", "replay", "buffer_dir", detail="Optional. Empty uses the app-data folder.")

        picture = self.group(tabs["picture"], "Picture and size")
        from .video_settings import VideoSettings
        self.video = VideoSettings(self, picture)
        self.fps = self.variable("recording", "fps")
        for key in ("bitrate_mbps", "rate_control"):
            self.variable("recording", key).trace_add("write", lambda *_: self.estimate())
        for section, key in (("replay", "seconds"), ("replay", "max_mb"), ("replay", "own_video"), ("replay", "game_audio"),
                             ("replay", "microphone"), ("replay", "sources"), ("replay", "game_track"),
                             ("replay", "audio_tracks")):
            self.variable(section, key).trace_add("write", lambda *_: self.estimate())
        self.separate_app.trace_add("write", lambda *_: self.estimate())
        self.estimate()

    def estimate(self):
        if hasattr(self, "video"):
            self.video.estimate()

    def watch_sound_apps(self, on):
        """While the Capture page is open, a native helper reports the programs
        with sound whenever they change; nothing runs while it is closed."""
        if not on:
            if self.sound_watch is not None:
                self.sound_watch.kill()
                self.sound_watch = None
            return
        if self.sound_watch is not None:
            return
        import subprocess
        from ..host import child_options, grouped
        from ..native_worker import binary_path

        worker = binary_path()
        if worker is None:
            return
        try:
            process = subprocess.Popen([str(grouped(worker)), "--app-audio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, **child_options())
            process.stdin.write(b'{"watch":true}\n')
            process.stdin.close()
        except OSError:
            return
        self.sound_watch = process

        def read():
            from .audio import OWN_PROGRAMS
            for line in process.stdout:
                try:
                    apps = json.loads(line)["apps"] if line.strip() else None
                except (ValueError, KeyError, TypeError):
                    continue
                if apps is not None:
                    apps = [a for a in apps if isinstance(a, dict) and isinstance(a.get("name"), str)
                            and a["name"].casefold() not in OWN_PROGRAMS]
                    self.post((self.show_sound_apps, apps, None))

        threading.Thread(target=read, daemon=True, name="jarvis-sound-apps").start()

    def show_sound_apps(self, found=None):
        """Medal-like rows: what is recorded, each with a volume. Games come in
        through "Game audio"; programs are the ticked ones first, then those with
        sound now. Rebuilt only when the rows change, so a slider being dragged stays."""
        if found is not None:
            self.sound_found = found
            for app in found:
                self.sound_app_labels[app["name"].casefold()] = app["label"]
        specific = self.variable("replay", "sources").get() == "specific"
        ticked = [n.strip() for n in self.separate_app.get().split(",") if n.strip()]
        rows = [("game_track", "Game audio", "The game running now, found by itself.", "game") if specific else
                ("game_audio", "Game and desktop sound", "Everything you hear.", "desktop"),
                ("microphone", "Microphone", "Your voice, from the input on the Voice page.", "microphone")]
        playing = set()
        if specific:
            names = list(ticked)
            for app in self.sound_found:
                if not app.get("game") and app["name"].casefold() not in {n.casefold() for n in names}:
                    names.append(app["name"])
            playing = {a["name"].casefold() for a in self.sound_found if a.get("playing")}
            rows += [(None, self.sound_app_labels.get(n.casefold(), n), "", n) for n in names]
        layout = [(setting, key) for setting, _, _, key in rows]
        if layout != getattr(self, "sound_layout", None):
            self.sound_layout = layout
            for child in self.sound_apps.winfo_children():
                child.destroy()
            self.sound_rows = {}
            self.sound_apps.columnconfigure(0, weight=1)
            for row, (setting, title, detail, key) in enumerate(rows):
                if setting:
                    variable = self.variable("replay", setting)
                    command = lambda: self.changed(400)
                else:
                    variable = tk.BooleanVar(value=key.casefold() in {n.casefold() for n in ticked})
                    command = lambda k=key, v=variable: self.tick_sound_app(k, v)
                check = ttk.Checkbutton(self.sound_apps, text=title, variable=variable, command=command)
                check.grid(row=row, column=0, sticky="w", pady=3)
                level = self.volumes.get(key, 100)
                shown = ttk.Label(self.sound_apps, text=f"{level}%", width=5, anchor="e", style="Card.TLabel")
                slider = VolumeSlider(self.sound_apps, self, level, lambda v, s=shown: s.configure(text=f"{v}%"),
                                      lambda v, k=key: self.set_volume(k, v))
                slider.grid(row=row, column=1, padx=(12, 6))
                shown.grid(row=row, column=2)
                self.sound_rows[key] = (check, title)
        for key, (check, title) in self.sound_rows.items():
            check.configure(text=title + ("  · playing now" if key.casefold() in playing else ""))
        self.sound_note.set(f"Programs appear here as they play sound. Tick up to {MAX_SEPARATE_APPS}; one not running "
                            "is silent until it starts." if specific else
                            "Choose Specific apps to keep only the game, chosen programs and your voice, each with "
                            "its own volume.")

    def set_volume(self, key, value):
        if self.volumes.get(key, 100) != value:
            self.volumes[key] = value
            self.changed(400)

    def pick_separate_tracks(self):
        self.variable("replay", "audio_tracks").set("separate" if self.separate_tracks.get() else "mixed")
        self.changed(300)

    def tick_sound_app(self, name, chosen):
        ticked = [n.strip() for n in self.separate_app.get().split(",") if n.strip()]
        ticked = [n for n in ticked if n.casefold() != name.casefold()]
        if chosen.get():
            if len(ticked) >= MAX_SEPARATE_APPS:
                chosen.set(False)
                self.say(f"Up to {MAX_SEPARATE_APPS} programs can have their own track.", error=True)
                return
            ticked.append(name)
        self.separate_app.set(", ".join(ticked))
        self.changed(400)

    def load_displays(self, result, quiet=False):
        """Every display by its own name, the main one marked. Nothing saved means the main display."""
        from .displays import display_label
        monitors = result["monitors"]
        main = next((d for d in monitors if (d.get("left"), d.get("top")) == (0, 0)), monitors[0] if monitors else None)
        self.display_ids = {}
        for display in monitors:
            self.display_ids[(display.get("label") or display_label(display)) + (" · main" if display is main else "")] = display["name"]
        saved = self.variable("recording", "monitor").get()
        wanted = saved or (main["name"] if main else "")
        shown = next((label for label, name in self.display_ids.items() if name == wanted), None)
        if shown is None:   # the saved display is unplugged: still show it, by number rather than device path
            shown = (saved.replace("\\\\.\\", "").replace("DISPLAY", "Display ") + " · not connected") if saved else PRIMARY_DISPLAY
            self.display_ids[shown] = saved
        self.display_name.set(shown)
        names = list(self.display_ids)
        self.display_choice.configure(values=names)
        if not quiet:
            self.say(f"Found {len(result['monitors'])} displays.")

    def pick_display(self):
        label = self.display_name.get()
        self.variable("recording", "monitor").set(self.display_ids.get(label, ""))
        self.changed(300)

    def browse(self):
        directory = filedialog.askdirectory(parent=self.root, title="Save recordings in")
        if directory:
            self.variable("recording", "output_dir").set(directory)
            self.changed(0)

    def open_folder(self):
        path = Path(self.variable("recording", "output_dir").get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def build_clips(self, p):
        from .clips import ClipsPanel
        self.clips = ClipsPanel(self, p)

    def build_general(self, p):
        tabs = self.tabs(p, [("general", "General"), ("privacy", "Privacy"), ("diagnostics", "Diagnostics")])
        self.build_diagnostics(tabs["diagnostics"])
        themes = self.group(tabs["general"], "Colors", "Jarvis's own themes, or a combination from Sanzo Wada's "
                                                       "A Dictionary of Color Combinations (1933).")
        swatches = ttk.Frame(themes, style="Card.TFrame", borderwidth=0)
        self.palette_choice = tk.StringVar(value=self.palette_setting)
        light, dark = palettes.LIGHT["roles"], palettes.GRAPHITE["roles"]
        follow = {"name": "Like Windows", "number": None, "dark": None,   # half Light, half Graphite
                  "swatches": {"Light page": light["bg"], "Light card": light["card"],
                               "Dark card": dark["card"], "Dark page": dark["bg"]}}
        for index, (key, preset) in enumerate([(palettes.SYSTEM, follow), *((k, palettes.ALL_PRESETS[k]) for k in palettes.WINDOWS)]):
            cell = ttk.Frame(swatches, style="Card.TFrame", borderwidth=0)
            cell.grid(row=index // 3, column=index % 3, padx=(0, 18), pady=(0, 14), sticky="w")
            strip = tk.Canvas(cell, width=210, height=34, highlightthickness=0, borderwidth=0)
            colors = list(preset["swatches"].values())
            for i, color in enumerate(colors):
                strip.create_rectangle(210 * i / len(colors), 0, 210 * (i + 1) / len(colors), 34, fill=color, width=0)
            strip.pack(anchor="w")
            ttk.Radiobutton(cell, text=preset["name"], value=key, variable=self.palette_choice, style="Swatch.TRadiobutton",
                            command=lambda key=key: self.choose_palette(key)).pack(anchor="w", pady=(6, 0))
            kind = "Light or dark, as Windows is set" if preset["dark"] is None else "dark" if preset["dark"] else "light"
            ttk.Label(cell, text=f"No. {preset['number']} · {kind}" if preset["number"] else kind[0].upper() + kind[1:],
                      style="Caption.TLabel").pack(anchor="w")
        self.line(themes, swatches)

        privacy = self.group(tabs["privacy"], "Privacy")
        for title, text in PRIVACY_FACTS:
            self.row(privacy, title, ttk.Frame(privacy, style="Card.TFrame"), text)
        self.line(privacy, "Settings and logs stay in your Windows account's application-data folder.", caption=True)
        startup = self.group(tabs["general"], "Startup")
        self.switch(startup, "Start Jarvis when I sign in", "", "startup")
        backup = self.group(tabs["general"], "Your settings", "Save every setting to a file to back them up or move them to another PC. "
                            "Jarvis also keeps a copy before an update or an import changes them.")
        self.buttons(backup, [("Export…", self.export_settings), ("Import…", self.import_settings), ("Open backups", self.open_backups)])
        keys = self.group(tabs["general"], "Keyboard shortcuts", "While this window is open.")
        for shortcut, meaning in (("Ctrl+M", "Mute or unmute the microphone"), ("Ctrl+Space", "Listen now, or cancel")):
            self.row(keys, meaning, ttk.Label(keys, text=shortcut, style="Key.TLabel"))
        leave = self.group(tabs["general"], "Quit")
        self.line(leave, "Stops voice control, recording and instant replay, and closes this window.", caption=True)
        self.buttons(leave, [("Quit Jarvis…", self.quit_jarvis, "Danger.TButton")])

    def build_diagnostics(self, p):
        self.subtitle(p, "These checks read dependencies and model paths. They don't open the microphone, capture your "
                         "desktop or change Windows settings.")
        from .diagnostics import report

        def show(result):
            self.diagnostic_text.delete("1.0", "end")
            self.diagnostic_text.insert("1.0", json.dumps(result, indent=2))

        checks = self.group(p, "")
        self.buttons(checks, [("Run diagnostics", lambda: self.submit(report, show), "Primary.TButton")])
        self.diagnostic_text = self.tk_widget(tk.Text(checks, height=18, wrap="word", relief="flat", borderwidth=0),
                                              background="inset", foreground="text", insertbackground="text")
        self.wide(checks, self.diagnostic_text)

    # Saving --------------------------------------------------------------

    def changed(self, delay=400):
        """A setting changed: save shortly, once, after the last change."""
        if self.loading or self.stop.is_set():
            return
        if self.save_timer:
            self.root.after_cancel(self.save_timer)
        self.save_timer = self.root.after(max(delay, 50), self.save)

    def settings(self):
        value = copy.deepcopy(self.config)
        for (section, key), variable in self.variables.items():
            target = value[section] if section else value
            previous = target[key]
            raw = variable.get()
            target[key] = (
                int(float(raw))
                if type(previous) is int
                else float(raw)
                if type(previous) is float
                else raw
            )
        value["activation"]["wake_aliases"] = [name.strip() for name in self.wake_aliases.get().split(",") if name.strip()]
        value["recording"]["fps"] = round(float(self.fps.get()))
        value["aliases"] = dict(self.aliases)
        value["app_prefixes"] = dict(self.prefixes)
        value["close_words"] = [word.strip() for word in self.close_words.get().split(",") if word.strip()]
        value["replay"]["separate_apps"] = [
            name.strip().removesuffix(".exe") for name in self.separate_app.get().split(",") if name.strip()
        ]
        if hasattr(self, "volumes") and "volumes" in value["replay"]:
            # Only what differs from 100%, for sounds that are still in the list.
            kept = {"game", "desktop", "microphone", *value["replay"]["separate_apps"]}
            value["replay"]["volumes"] = {k: v for k, v in self.volumes.items() if v != 100 and k in kept}
        value["dictation"]["stop_phrases"] = [p.strip() for p in self.voice_controls.stop_phrases.get().split(",") if p.strip()]
        value["commands"] = dict(self.commands)
        return config.validate(value)

    def save(self, success="Saved."):
        self.save_timer = None
        try:
            value = self.settings()
        except (ValueError, TypeError, KeyError) as exc:
            self.say(f"Not saved: {exc}", error=True)
            return
        if value == self.config:
            return
        startup_changed = value["startup"] != self.config["startup"]
        self.config = value   # later changes build on this one; a failure reloads the saved file

        def run():
            from .startup import set_startup

            try:
                try:
                    status = request("status", timeout=1)
                except (OSError, ValueError):
                    status = {}
                if status.get("ok"):
                    result = save_config_restarting(value)
                    if not result.get("ok"):
                        raise ValueError(result.get("error", "Could not save"))
                else:
                    config.save(value)
                    result = {}
                if startup_changed:
                    set_startup(value["startup"])
            except (OSError, ValueError) as error:
                return str(error), ""
            return None, restart_note(result)

        def saved(outcome):
            error, note = outcome
            if error:
                self.config = config.load()
                self.say(f"Not saved: {error}", error=True)
                return
            self.say(success + note)
            if self.config["privacy"]["onboarded"]:
                self.onboard_card.grid_remove()

        self.submit(run, saved)

    def export_settings(self):
        import time
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export Jarvis settings", defaultextension=".json",
            initialfile=f"jarvis-settings-{time.strftime('%Y-%m-%d')}.json", filetypes=[("Settings", "*.json")])
        if path:
            from ..settings_file import export_settings
            self.submit(lambda: export_settings(path), lambda saved: self.say(f"Settings saved to {saved}."))

    def import_settings(self):
        path = filedialog.askopenfilename(parent=self.root, title="Import Jarvis settings",
                                          filetypes=[("Settings", "*.json"), ("All files", "*.*")])
        if path:
            from ..settings_file import import_settings
            self.submit(lambda: import_settings(path), lambda message: self.say(message + " Reopen settings to see them."))

    def open_backups(self):
        folder = config.backups_dir()
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def quit_jarvis(self):
        if messagebox.askyesno("Quit Jarvis?", "Voice control, recording and instant replay stop, and this window closes.",
                               icon="warning", default="no", parent=self.root):
            self.call("shutdown", lambda _: self.close())

    def close(self):
        self.watch_sound_apps(False)
        self.clips.close()
        if self.save_timer:   # a change still waiting for its pause
            self.root.after_cancel(self.save_timer)
            self.save()
        self.stop.set()
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--page", default="dashboard")
    args = parser.parse_args(argv)
    root = tk.Tk()
    Window(root, args.page)
    # Building every page touches far more memory than showing one needs.
    root.after(RELEASE_MEMORY_AFTER_MS, release_unused_memory)
    root.mainloop()
    return 0
