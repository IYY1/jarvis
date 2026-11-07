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
        self.palette = palettes.chosen("graphite")
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
        for key, title in NAVIGATION:
            button = ttk.Button(rail, text=title, style="Nav.TButton", command=lambda key=key: self.show(key))
            button.pack(fill="x", pady=1)
            self.nav[key] = button
        self.content = ttk.Frame(body)
        self.content.pack(side="left", fill="both", expand=True)
        for key, title in NAVIGATION:
            getattr(self, "build_" + key)(self.page(key, title))
        self.toast()
        self.show(page)
        root.bind_all("<MouseWheel>", self.scroll)
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
                  "PillOff.TLabel": (r["error-bg"], r["error-text"], (f, 9, "bold")),
                  "Toast.TLabel": (r["text"], r["bg"], (f, 10, "bold")), "ToastError.TLabel": (r["error-bg"], r["error-text"], (f, 10, "bold"))}
        for name, (background, foreground, font) in labels.items():
            s.configure(name, background=background, foreground=foreground, font=font)
        for name in ("Pill.TLabel", "PillLive.TLabel", "PillCalm.TLabel", "PillOff.TLabel"):
            s.configure(name, padding=(10, 3))
        s.configure("Key.TLabel", padding=(8, 2))
        s.configure("Toast.TFrame", background=r["text"])
        s.configure("ToastError.TFrame", background=r["error-bg"])
        buttons = {"TButton": (r["control"], r["text"], r["line-strong"], r["control-hover"]),
                   "Primary.TButton": (r["accent"], r["on-accent"], r["accent"], r["accent-hover"]),
                   "Danger.TButton": (r["error-bg"], r["error-text"], r["error-text"], r["error-bg"]),
                   "Tab.TButton": (r["bg"], r["text-muted"], r["bg"], r["control"]),
                   "TabOn.TButton": (r["control"], r["text"], r["control"], r["control"]),
                   "Nav.TButton": (r["surface"], r["text-muted"], r["surface"], r["control"]),
                   "NavOn.TButton": (r["selected"], r["text"], r["selected"], r["selected"]),
                   "Header.TButton": (r["control"], r["text"], r["line-strong"], r["control-hover"]),
                   "Muted.TButton": (r["live-soft"], r["live-text"], r["live"], r["live-soft"]),
                   "Toast.TButton": (r["text"], r["bg"], r["text"], r["text"])}
        for name, (background, foreground, border, hover) in buttons.items():
            s.configure(name, background=background, foreground=foreground, bordercolor=border, lightcolor=background,
                        darkcolor=background, relief="flat", padding=(12, 4), font=(f + " Semibold", 10))
            s.map(name, background=[("disabled", r["disabled"]), ("pressed", hover), ("active", hover)],
                  foreground=[("disabled", r["text-disabled"])], bordercolor=[("focus", r["focus"])])
        s.configure("Nav.TButton", anchor="w", padding=(12, 6), borderwidth=0)
        s.configure("NavOn.TButton", anchor="w", padding=(12, 6), borderwidth=0)
        s.configure("Toast.TButton", padding=(8, 2))
        for name in ("TCheckbutton", "TRadiobutton"):
            s.configure(name, background=r["card"], foreground=r["text"], indicatorbackground=r["inset"],
                        indicatorforeground=r["on-accent"], upperbordercolor=r["line-strong"], lowerbordercolor=r["line-strong"],
                        font=(f, 10))
            s.map(name, background=[("active", r["card"])], indicatorbackground=[("selected", r["accent"]), ("active", r["control"])],
                  foreground=[("disabled", r["text-disabled"])])
        s.configure("Swatch.TRadiobutton", font=(f, 10, "bold"))
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
        if self.loading or name == self.palette:
            return
        self.palette = name
        self.apply_theme()
        try:
            palettes.choose(name)
        except OSError as error:
            self.say(f"The colors changed, but couldn't be kept for next time: {error}", error=True)
            return
        self.say(f"Colors: {palettes.PRESETS[name]['name']}.")

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
        inner.bind("<Configure>", lambda _, view=canvas: view.configure(scrollregion=view.bbox("all")))
        def fit(event, view=canvas, item=item):
            width = min(event.width, 1000)   # lines stay readable in a wide window
            view.itemconfigure(item, width=width)
            view.coords(item, event.width / 2, 0)
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
            self.nav[name].configure(style="NavOn.TButton" if name == key else "Nav.TButton")
        self.pages[key].pack(fill="both", expand=True)
        self.current = key
        self.rewrap()
        self.load_apps()

    def load_apps(self):
        """Fill the app list when first seen; otherwise it stays empty until someone finds Refresh."""
        if getattr(self, "current", None) == "commands" and self.online and not self.apps_loaded:
            self.apps_loaded = True
            self.call("apps", self.show_apps)

    def scroll(self, event):
        if not isinstance(event.widget, (ttk.Combobox, ttk.Treeview, tk.Text, ttk.Spinbox)):
            self.canvases[self.current].yview_scroll(-int(event.delta / 120), "units")

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
        self.bubble = ttk.Frame(self.root, style="Toast.TFrame", padding=(16, 8, 6, 8))
        self.notice = tk.StringVar()
        self.notice_label = ttk.Label(self.bubble, textvariable=self.notice, style="Toast.TLabel", wraplength=640)
        self.notice_label.pack(side="left")
        self.dismiss = ttk.Button(self.bubble, text="✕", width=3, style="Toast.TButton", command=self.hide_toast)
        self.dismiss.pack(side="left", padx=(12, 0))

    def say(self, text, error=False):
        """Results fade after a few seconds; errors stay until dismissed or replaced."""
        if self.stop.is_set() or not text:
            return
        self.notice.set(text)
        self.bubble.configure(style="ToastError.TFrame" if error else "Toast.TFrame")
        self.notice_label.configure(style="ToastError.TLabel" if error else "Toast.TLabel")
        self.bubble.place(relx=0.5, rely=1.0, anchor="s", y=-18)
        self.bubble.lift()
        if self.toast_timer:
            self.root.after_cancel(self.toast_timer)
            self.toast_timer = None
        if not error:
            self.toast_timer = self.root.after(min(9000, 2500 + 45 * len(text)), self.hide_toast)

    def hide_toast(self):
        self.toast_timer = None
        self.bubble.place_forget()

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
        self.mic_choice = ttk.Combobox(microphone, textvariable=self.variable("audio", "device"), width=26)
        self.mic_choice.bind("<<ComboboxSelected>>", lambda _: self.changed(300))
        self.mic_choice.bind("<Return>", lambda _: self.changed(0))
        self.mic_choice.bind("<FocusOut>", lambda _: self.changed(0))
        self.row(microphone, "Input", self.mic_choice, "Empty uses your Windows default input.")
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

    def load_inputs(self, result):
        self.mic_choice.configure(values=[item["id"] for item in result["devices"]])
        self.say(f"Found {len(result['devices'])} inputs. Pick one from the Input list.")

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
        self.display_choice = self.choice(source, "Display", "recording", "monitor", {self.config["recording"]["monitor"] or "Primary display":
                                                                                     self.config["recording"]["monitor"]})
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
        self.switch(replay, "Game sound", "replay", "game_audio", "Everything you hear, from the default output.")
        self.switch(replay, "Microphone", "replay", "microphone", "Your voice, from the default input.")
        self.switch(replay, "Clip sound", "replay", "sound", "A short chirp when a clip is saved.")
        self.switch(replay, "Start with Jarvis", "replay", "autostart", "Turn replay on whenever Jarvis starts.")

        extra = self.group(tabs["replay"], "Separate sound and storage")
        self.choice(extra, "Sound tracks", "replay", "audio_tracks",
                    {"One mixed track": "mixed", "Separate tracks (Windows 11)": "separate"},
                    "Separate tracks give each program you tick below, the rest of the desktop and your "
                    "microphone a track of their own, after a first track with them all mixed, so clips play "
                    "anywhere and you can mute one later. For replay and recordings.")
        self.separate_app = tk.StringVar(value=", ".join(self.config["replay"]["separate_apps"]))
        self.wide(extra, self.wrapping(ttk.Label(
            extra, style="Caption.TLabel", justify="left",
            text=f"Programs on their own track, up to {MAX_SEPARATE_APPS}. They may start later or close. "
                 "To keep everything else out, turn off Game sound above: then only the programs you tick "
                 "and your microphone are recorded.")))
        self.sound_apps = self.wide(extra, ttk.Frame(extra, style="Card.TFrame", borderwidth=0))
        self.sound_app_labels = {}
        self.buttons(extra, [("Find programs with sound", self.find_sound_apps)])
        self.show_sound_apps([])
        self.find_sound_apps()
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
                             ("replay", "microphone")):
            self.variable(section, key).trace_add("write", lambda *_: self.estimate())
        self.estimate()

    def estimate(self):
        if hasattr(self, "video"):
            self.video.estimate()

    def find_sound_apps(self):
        from .audio import apps_with_sound
        self.submit(apps_with_sound, self.show_sound_apps)

    def show_sound_apps(self, found):
        """Ticked programs first, then those with sound now, then those heard before."""
        for app in found:
            self.sound_app_labels[app["name"].casefold()] = app["label"]
        ticked = [n.strip() for n in self.separate_app.get().split(",") if n.strip()]
        names = list(ticked)
        for app in found:
            if app["name"].casefold() not in {n.casefold() for n in names}:
                names.append(app["name"])
        for child in self.sound_apps.winfo_children():
            child.destroy()
        playing = {a["name"].casefold() for a in found if a.get("playing")}
        for name in names:
            chosen = tk.BooleanVar(value=name.casefold() in {n.casefold() for n in ticked})
            label = self.sound_app_labels.get(name.casefold(), name)
            text = label + ("  · playing now" if name.casefold() in playing else "")
            ttk.Checkbutton(self.sound_apps, text=text, variable=chosen,
                            command=lambda n=name, v=chosen: self.tick_sound_app(n, v)).pack(anchor="w", pady=2)
        if not names:
            ttk.Label(self.sound_apps, text="No programs with sound found. Play something, then find them again.",
                      style="Caption.TLabel").pack(anchor="w")

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

    def load_displays(self, result):
        names = [d["name"] for d in result["monitors"]]
        self.display_choice.configure(values=names)
        self.say(f"Found {len(names)} displays.")

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
        themes = self.group(tabs["general"], "Colors", "Graphite, or a combination from Sanzo Wada's A Dictionary of Color Combinations (1933).")
        swatches = ttk.Frame(themes, style="Card.TFrame", borderwidth=0)
        self.palette_choice = tk.StringVar(value=self.palette)
        for column, (key, preset) in enumerate(palettes.ALL_PRESETS.items()):
            cell = ttk.Frame(swatches, style="Card.TFrame", borderwidth=0)
            cell.grid(row=0, column=column, padx=(0, 16), sticky="w")
            strip = tk.Canvas(cell, width=180, height=34, highlightthickness=0, borderwidth=0)
            colors = list(preset["swatches"].values())
            for index, color in enumerate(colors):
                strip.create_rectangle(180 * index / len(colors), 0, 180 * (index + 1) / len(colors), 34, fill=color, width=0)
            strip.pack(anchor="w")
            ttk.Radiobutton(cell, text=preset["name"], value=key, variable=self.palette_choice, style="Swatch.TRadiobutton",
                            command=lambda key=key: self.choose_palette(key)).pack(anchor="w", pady=(6, 0))
            kind = "dark" if preset["dark"] else "light"
            ttk.Label(cell, text=f"No. {preset['number']} · {kind}" if preset["number"] else kind.capitalize(),
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
