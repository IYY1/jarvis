"""Native Windows settings. Network/audio/COM work never runs on Tk's thread."""

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

from .. import config
from ..client import TIMEOUTS, request, restart_note, save_config_restarting, watch
from ..commands import REFERENCE
from ..encoding import storage_estimate


class Window:
    def __init__(self, root, page="dashboard"):
        self.root = root
        self.config = config.load()
        self.events = queue.Queue(maxsize=128)
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="jarvis-settings"
        )
        self.stop = threading.Event()
        self.variables = {}
        self.pages = {}
        self.tabs, self.canvases = {}, {}
        self.online = False
        root.title("Jarvis · Settings")
        root.geometry(
            f"{min(1060, root.winfo_screenwidth() - 60)}x{min(800, root.winfo_screenheight() - 80)}"
        )
        root.minsize(820, 520)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("Heading.TLabel", font=("Segoe UI", 17, "bold"))
        style.configure("TButton", padding=(12, 7))
        shell = ttk.Frame(root, padding=18)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="Jarvis", style="Heading.TLabel").pack(anchor="w")
        self.notice = tk.StringVar(value="Service offline · microphone closed")
        ttk.Label(shell, textvariable=self.notice, wraplength=980).pack(
            anchor="w", pady=(4, 12)
        )
        self.book = ttk.Notebook(shell)
        self.book.pack(fill="both", expand=True)
        for key, title in (
            ("dashboard", "Home"),
            ("activation", "Voice"),
            ("applications", "Applications"),
            ("commands", "Commands"),
            ("recording", "Recording"),
            ("replay", "Replay"),
            ("privacy", "Privacy"),
            ("diagnostics", "Diagnostics"),
        ):
            tab = ttk.Frame(self.book)
            tab.rowconfigure(0, weight=1)
            tab.columnconfigure(0, weight=1)
            canvas = tk.Canvas(
                tab,
                highlightthickness=0,
                background=style.lookup("TFrame", "background") or "SystemButtonFace",
            )
            scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.grid(row=0, column=0, sticky="nsew")
            scrollbar.grid(row=0, column=1, sticky="ns")
            frame = ttk.Frame(canvas, padding=18)
            item = canvas.create_window(0, 0, anchor="nw", window=frame)
            frame.bind(
                "<Configure>",
                lambda _, view=canvas: view.configure(scrollregion=view.bbox("all")),
            )
            canvas.bind(
                "<Configure>",
                lambda event, view=canvas, item=item: view.itemconfigure(
                    item, width=event.width
                ),
            )
            frame.columnconfigure(1, weight=1)
            self.book.add(tab, text=title)
            self.pages[key] = frame
            self.tabs[key] = tab
            self.canvases[str(tab)] = canvas
            getattr(self, "build_" + key)(frame)
        self.book.select(self.tabs.get(page, self.tabs["dashboard"]))
        root.bind("<MouseWheel>", self.scroll)
        footer = ttk.Frame(shell)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Button(footer, text="Save settings", command=self.save).pack(side="right")
        ttk.Label(footer, text="Offline recognition · no Discord integration").pack(
            side="left"
        )
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.pump)
        self.refresh_status()
        threading.Thread(target=self.observe, name="jarvis-status", daemon=True).start()

    def scroll(self, event):
        if not isinstance(event.widget, (ttk.Combobox, ttk.Treeview, tk.Text)):
            self.canvases[self.book.select()].yview_scroll(
                -int(event.delta / 120), "units"
            )

    def note(self, parent, text, row):
        ttk.Label(parent, text=text, wraplength=910, justify="left").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(5, 12)
        )

    def variable(self, section, key):
        identity = (section, key)
        if identity not in self.variables:
            value = self.config[section][key] if section else self.config[key]
            cls = tk.BooleanVar if type(value) is bool else tk.StringVar
            self.variables[identity] = cls(value=value)
        return self.variables[identity]

    def field(self, parent, label, section, key, row, choices=None):
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", pady=7, padx=(0, 20)
        )
        variable = self.variable(section, key)
        if isinstance(choices, dict):
            display = tk.StringVar(
                value=next(
                    (
                        name
                        for name, value in choices.items()
                        if value == variable.get()
                    ),
                    variable.get(),
                )
            )
            display.trace_add(
                "write",
                lambda *_, target=variable, mapping=choices: target.set(
                    mapping[display.get()]
                ),
            )
            variable = display
            choices = tuple(choices)
        widget = (
            ttk.Combobox(
                parent, textvariable=variable, values=choices, state="readonly"
            )
            if choices
            else ttk.Entry(parent, textvariable=variable)
        )
        widget.grid(row=row, column=1, sticky="ew", pady=7)
        return widget

    def check(self, parent, label, section, key, row):
        ttk.Checkbutton(parent, text=label, variable=self.variable(section, key)).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=7
        )

    def buttons(self, parent, items, row):
        group = ttk.Frame(parent)
        group.grid(row=row, column=0, columnspan=3, sticky="w", pady=9)
        for label, command in items:
            ttk.Button(group, text=label, command=command).pack(
                side="left", padx=(0, 8)
            )

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
                    messagebox.showerror("Jarvis", error, parent=self.root)
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
                watch(
                    lambda event: self.post((self.received, event, None)),
                    self.stop,
                )
                delay = 1
            except (OSError, ValueError, RuntimeError):
                self.post((self.disconnected, None, None))
            self.stop.wait(delay)
            delay = min(30, delay * 2)

    def disconnected(self, _):
        self.online = False
        self.notice.set("Service offline · microphone closed")

    def received(self, reply):
        status = reply.get("status")
        if status:
            self.online = True
            mic = (
                "MICROPHONE OPEN" if status["microphone_open"] else "Microphone closed"
            )
            self.notice.set(mic + " · " + status["message"])
            self.record_state.set(status["recording"]["message"])
            self.replay_state.set(status["replay"]["message"])
            self.mute_button.configure(
                text="Resume microphone" if status["muted"] else "Mute microphone",
                command=lambda: self.call("mute", muted=not status["muted"]),
            )

    def refresh_status(self):
        def run():
            try:
                return request("status", timeout=1)
            except (OSError, ValueError):
                return {}

        self.submit(run, self.received)

    def build_dashboard(self, p):
        self.note(
            p,
            "Control your desktop with your voice. Start the service, then review microphone consent in Privacy. Closing this window leaves the service in the notification area.",
            0,
        )
        self.mute_button = ttk.Button(
            p, text="Resume microphone", command=lambda: self.call("mute", muted=False)
        )
        self.mute_button.grid(row=1, column=0, sticky="w", pady=10)
        from .startup import start_service

        self.buttons(
            p,
            [
                (
                    "Start service",
                    lambda: self.submit(
                        start_service,
                        lambda _: self.root.after(600, self.refresh_status),
                    ),
                ),
                ("Listen once", lambda: self.call("listen")),
                ("Cancel", lambda: self.call("cancel")),
            ],
            2,
        )
        self.note(
            p,
            "Wake mode keeps the microphone open after you grant consent. Push-to-talk opens it only when you choose Listen once. A microphone test runs for at most 15 seconds.",
            3,
        )
        self.buttons(
            p,
            [
                ("Test microphone", lambda: self.call("mic_test", enabled=True)),
                ("End test", lambda: self.call("mic_test", enabled=False)),
                ("Quit service", lambda: self.call("shutdown")),
            ],
            4,
        )
        self.check(p, "Start Jarvis when I sign in to Windows", "", "startup", 5)
        self.note(
            p,
            "Try: “hey Jarvis, open Firefox”, “turn the volume down”, “start recording”, or “clip that”. Existing Windows virtual desktops are called workspaces one through ten.",
            6,
        )
        self.buttons(
            p,
            [
                ("Export settings…", self.export_settings),
                ("Import settings…", self.import_settings),
                ("Open backups folder", self.open_backups),
            ],
            7,
        )
        self.note(
            p,
            "Save your settings to a file to back them up or move them to another PC. Jarvis also keeps a copy before an update changes settings and before an import.",
            8,
        )

    def export_settings(self):
        import time
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export Jarvis settings", defaultextension=".json",
            initialfile=f"jarvis-settings-{time.strftime('%Y-%m-%d')}.json", filetypes=[("Settings", "*.json")])
        if path:
            from ..settings_file import export_settings
            self.submit(lambda: export_settings(path),
                        lambda saved: messagebox.showinfo("Jarvis", f"Settings saved to {saved}", parent=self.root))

    def import_settings(self):
        path = filedialog.askopenfilename(parent=self.root, title="Import Jarvis settings",
                                          filetypes=[("Settings", "*.json"), ("All files", "*.*")])
        if path:
            from ..settings_file import import_settings
            self.submit(lambda: import_settings(path),
                        lambda message: messagebox.showinfo("Jarvis", message + " Reopen settings to see them.",
                                                            parent=self.root))

    def open_backups(self):
        folder = config.backups_dir()
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def build_activation(self, p):
        self.field(
            p,
            "Activation",
            "activation",
            "mode",
            0,
            {"Wake phrase": "wake_word", "Push to talk": "push_to_talk"},
        )
        self.field(p, "Wake phrase", "activation", "wake_phrase", 1)
        self.wake_aliases = tk.StringVar(
            value=", ".join(self.config["activation"]["wake_aliases"])
        )
        ttk.Label(p, text="Extra wake names (comma separated)").grid(
            row=2, column=0, sticky="w"
        )
        ttk.Entry(p, textvariable=self.wake_aliases).grid(
            row=2, column=1, sticky="ew", pady=7
        )
        self.field(
            p, "Command window (2–15 seconds)", "activation", "command_timeout", 3
        )
        self.field(p, "Wake sensitivity (0–1)", "activation", "sensitivity", 4)
        self.field(p, "Recognition confidence (0–1)", "activation", "confidence", 5)
        self.mic_choice = self.field(p, "Microphone", "audio", "device", 6)
        ttk.Button(
            p,
            text="Refresh inputs",
            command=lambda: self.call("devices", self.load_inputs),
        ).grid(row=6, column=2, padx=8)
        self.check(p, "Adapt to steady background sound", "audio", "adaptive_gate", 7)
        self.check(
            p,
            "Use the accurate offline Whisper model for commands",
            "recognition",
            "accurate",
            8,
        )
        self.check(
            p, "Match the closest supported command", "recognition", "closest_match", 9
        )
        self.field(
            p,
            "Command model",
            "recognition",
            "speech_model",
            11,
            choices={"Small (most accurate)": "small", "Light (less memory)": "light"},
        )
        self.check(
            p,
            "Noise suppression (experimental)",
            "recognition",
            "noise_suppression",
            10,
        )
        self.note(
            p,
            "Microphone input IDs are shown after refreshing. An empty ID uses your Windows default input. No audio leaves your computer.",
            12,
        )

    def load_inputs(self, result):
        self.mic_choice.configure(values=[item["id"] for item in result["devices"]])

    def build_applications(self, p):
        self.note(
            p,
            "Refresh installed applications, select one and give it a short spoken name. Save settings to apply. Add program… adds a portable program that Windows does not list, as a Start menu shortcut in a Jarvis folder. A launch word such as “open” or “watch” goes before the name; leave it empty to say only the name.",
            0,
        )
        self.apps_tree = ttk.Treeview(
            p,
            columns=("name", "id"),
            displaycolumns=("name",),
            show="headings",
            height=8,
        )
        self.apps_tree.heading("name", text="Installed applications")
        self.apps_tree.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.app_alias = tk.StringVar()
        self.launch_word = tk.StringVar(value="open")
        ttk.Label(p, text="Voice name").grid(row=2, column=0, sticky="w")
        ttk.Entry(p, textvariable=self.app_alias).grid(
            row=2, column=1, sticky="ew", pady=7
        )
        ttk.Label(p, text="Launch word").grid(row=3, column=0, sticky="w")
        ttk.Entry(p, textvariable=self.launch_word).grid(
            row=3, column=1, sticky="ew", pady=7
        )
        self.buttons(
            p,
            [
                ("Refresh apps", lambda: self.call("apps", self.show_apps)),
                ("Add program…", self.add_program),
                ("Assign voice name", self.assign_alias),
                ("Remove voice name", self.remove_alias),
            ],
            4,
        )
        self.aliases = copy.deepcopy(self.config["aliases"])
        self.prefixes = copy.deepcopy(self.config["app_prefixes"])
        self.app_names = {}
        self.alias_tree = ttk.Treeview(
            p, columns=("voice", "app"), show="headings", height=7
        )
        self.alias_tree.heading("voice", text="Say this")
        self.alias_tree.heading("app", text="Application")
        self.alias_tree.grid(row=5, column=0, columnspan=3, sticky="ew")
        self.alias_tree.bind("<<TreeviewSelect>>", self.select_alias)
        self.refresh_aliases()
        self.close_words = tk.StringVar(value=", ".join(self.config["close_words"]))
        ttk.Label(p, text="Close words (comma separated)").grid(row=6, column=0, sticky="w")
        ttk.Entry(p, textvariable=self.close_words).grid(row=6, column=1, sticky="ew", pady=7)
        self.note(p, "Say a close word before an app name to close it, for example “close Firefox” or “quit Firefox”.", 7)

    def refresh_aliases(self):
        self.alias_tree.delete(*self.alias_tree.get_children())
        for name, identity in sorted(self.aliases.items()):
            phrase = " ".join(filter(None, (self.prefixes.get(name, "open"), name)))
            self.alias_tree.insert(
                "",
                "end",
                iid=name,
                values=(phrase, self.app_names.get(identity, identity)),
            )

    def select_alias(self, _):
        selection = self.alias_tree.selection()
        if selection:
            name = selection[0]
            self.app_alias.set(name)
            self.launch_word.set(self.prefixes.get(name, "open"))

    def show_apps(self, result):
        self.apps_tree.delete(*self.apps_tree.get_children())
        self.app_names = {app["id"]: app["name"] for app in result["apps"]}
        for app in result["apps"]:
            self.apps_tree.insert("", "end", values=(app["name"], app["id"]))
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
        dialog = tk.Toplevel(self.root)
        dialog.title("Add a program")
        dialog.transient(self.root)
        ttk.Label(dialog, text="Programs found on the desktop, in Downloads and portable-app folders:").pack(
            anchor="w", padx=12, pady=(12, 6))
        listing = tk.Listbox(dialog, height=min(12, len(candidates)), width=90)
        for path in candidates:
            listing.insert("end", str(path))
        listing.selection_set(0)
        listing.pack(fill="both", expand=True, padx=12)

        def choose(path=None):
            dialog.destroy()
            self.name_program(path) if path else self.browse_program()

        row = ttk.Frame(dialog)
        row.pack(anchor="e", padx=12, pady=12)
        ttk.Button(row, text="Add selected",
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
        name = simpledialog.askstring("Add a program", "Name to show and say:",
                                      initialvalue=Path(path).stem, parent=self.root)
        if not name:
            return

        def add():
            from .. import app_catalog
            from .system import com_context
            with com_context():
                return app_catalog.add(path, name)

        self.submit(add, lambda _: self.call("apps", self.show_apps))

    def assign_alias(self):
        selected = self.apps_tree.selection()
        name = self.app_alias.get().strip()
        if not selected or not name:
            messagebox.showinfo(
                "Voice name",
                "Choose an application and enter a voice name.",
                parent=self.root,
            )
            return
        self.aliases[name] = self.apps_tree.item(selected[0], "values")[1]
        self.prefixes[name] = self.launch_word.get().strip()
        self.refresh_aliases()

    def remove_alias(self):
        for name in self.alias_tree.selection():
            self.aliases.pop(name, None)
            self.prefixes.pop(name, None)
        self.refresh_aliases()

    def build_commands(self, p):
        text = "\n\n".join(
            group["title"] + "\n" + " · ".join(group["examples"]) for group in REFERENCE
        )
        self.note(p, text, 0)
        self.note(
            p,
            'Custom phrases map to an existing command, e.g. {"quiet please": "mute volume"}. Shell commands are never accepted.',
            1,
        )
        self.commands_text = tk.Text(p, height=7)
        self.commands_text.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.commands_text.insert("1.0", json.dumps(self.config["commands"], indent=2))
        self.note(p, "Open and close every installed app", 3)
        self.application_commands = tk.Text(p, height=12, wrap="word", state="disabled")
        self.application_commands.grid(row=4, column=0, columnspan=3, sticky="nsew")

    def show_app_commands(self, result):
        self.application_commands.configure(state="normal")
        self.application_commands.delete("1.0", "end")
        text = "\n\n".join(row["title"] + "\n" + " · ".join(row["examples"])
                            for row in result.get("applications", []))
        self.application_commands.insert("1.0", text)
        self.application_commands.configure(state="disabled")

    def build_recording(self, p):
        self.record_state = tk.StringVar(value="Recorder ready")
        ttk.Label(p, textvariable=self.record_state, wraplength=910).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )
        self.display_choice = self.field(p, "Display", "recording", "monitor", 1)
        ttk.Button(
            p,
            text="Refresh displays",
            command=lambda: self.call("monitors", self.load_displays),
        ).grid(row=1, column=2, padx=8)
        self.field(p, "Save folder", "recording", "output_dir", 2)
        ttk.Button(p, text="Browse…", command=self.browse).grid(row=2, column=2, padx=8)
        self.field(
            p,
            "Format",
            "recording",
            "codec",
            3,
            {
                "H.264 · easy sharing": "h264",
                "HEVC · smaller files": "hevc",
                "AV1 · recent GPUs": "av1",
                "H.264 · CPU compatibility": "libx264",
            },
        )
        self.field(
            p,
            "Encoder",
            "recording",
            "encoder",
            4,
            {
                "Automatic · GPU": "auto",
                "NVIDIA · NVENC": "nvenc",
                "AMD · AMF": "amf",
                "Intel · Quick Sync": "qsv",
                "CPU · H.264 only": "cpu",
            },
        )
        ttk.Label(p, text="Frames per second").grid(row=5, column=0, sticky="w")
        self.fps = tk.DoubleVar(value=self.config["recording"]["fps"])
        self.fps_label = tk.StringVar()
        ttk.Scale(
            p, from_=1, to=60, variable=self.fps, command=lambda _: self.estimate()
        ).grid(row=5, column=1, sticky="ew", pady=7)
        ttk.Label(p, textvariable=self.fps_label, width=10).grid(row=5, column=2)
        self.field(
            p,
            "Size control",
            "recording",
            "rate_control",
            6,
            {"Storage budget": "bitrate", "Quality preset": "quality"},
        )
        self.bitrate_widget = self.field(
            p, "Video budget (1–100 Mbps)", "recording", "bitrate_mbps", 7
        )
        self.quality_widget = self.field(
            p,
            "Quality preset",
            "recording",
            "quality_preset",
            8,
            {"Medium": "medium", "High": "high", "Very high": "very_high", "Maximum": "ultra"},
        )
        self.field(
            p,
            "Color",
            "recording",
            "color_mode",
            9,
            {"Automatic": "auto", "SDR": "sdr", "HDR": "hdr"},
        )
        self.check(
            p,
            "Include system sound (Windows default output)",
            "recording",
            "system_audio",
            10,
        )
        self.check(
            p,
            "Include microphone (Windows default input)",
            "recording",
            "microphone",
            11,
        )
        self.check(
            p,
            "I reviewed the display, sound and output settings",
            "recording",
            "configured",
            12,
        )
        self.size_label = tk.StringVar()
        ttk.Label(p, textvariable=self.size_label, wraplength=910).grid(
            row=13, column=0, columnspan=3, sticky="w", pady=6
        )
        self.buttons(
            p,
            [
                ("Start recording", lambda: self.call("record_start")),
                ("Stop and save", lambda: self.call("record_stop")),
                ("Open folder", self.open_folder),
            ],
            14,
        )
        for key in ("bitrate_mbps", "rate_control"):
            self.variable("recording", key).trace_add(
                "write", lambda *_: self.estimate()
            )
        self.estimate()

    def estimate(self):
        self.fps_label.set(str(round(self.fps.get())) + " fps")
        quality = self.variable("recording", "rate_control").get() == "quality"
        self.bitrate_widget.configure(state="disabled" if quality else "normal")
        self.quality_widget.configure(state="readonly" if quality else "disabled")
        try:
            budget = int(self.variable("recording", "bitrate_mbps").get())
            text = f"Video budget: about {storage_estimate({'bitrate_mbps': budget}):g} MB/min, plus sound and container overhead. Actual size varies."
        except ValueError:
            text = "Enter a whole-number video budget."
        if self.variable("recording", "rate_control").get() == "quality":
            text = "Quality mode varies file size with motion. Choose bitrate for a predictable storage budget."
        self.size_label.set(text)

    def load_displays(self, result):
        self.display_choice.configure(values=[d["name"] for d in result["monitors"]])

    def browse(self):
        directory = filedialog.askdirectory(
            parent=self.root, title="Save recordings in"
        )
        if directory:
            self.variable("recording", "output_dir").set(directory)

    def open_folder(self):
        path = Path(self.variable("recording", "output_dir").get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def build_replay(self, p):
        self.replay_state = tk.StringVar(value="Replay off")
        ttk.Label(p, textvariable=self.replay_state, wraplength=910).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=10
        )
        self.note(
            p,
            "Replay uses the recording display and encoding controls. RAM writes video only when you save. Choosing Disk writes unsaved screen and sound continuously, roughly 1–10 GB/hour depending on bitrate. Deleted files are not securely erased; crash leftovers are cleaned on next start.",
            1,
        )
        self.field(p, "History (10–1800 seconds)", "replay", "seconds", 2)
        self.check(p, "Include game / system sound", "replay", "game_audio", 3)
        self.check(p, "Include microphone", "replay", "microphone", 4)
        self.check(p, "Play a sound when a replay is saved", "replay", "sound", 5)
        self.check(p, "Start replay with the service", "replay", "autostart", 6)
        self.field(p, "Storage", "replay", "storage", 7, choices={"RAM": "ram", "Disk (continuous writes)": "disk"})
        self.field(p, "Encoded-history budget (64–16384 MiB)", "replay", "max_mb", 8)
        self.field(p, "Private disk-buffer directory (blank: app data)", "replay", "buffer_dir", 9)
        self.field(p, "Sound tracks", "replay", "audio_tracks", 10,
                   choices={"One mixed track": "mixed", "Separate tracks (Windows 11)": "separate"})
        self.separate_app = tk.StringVar(value=", ".join(self.config["replay"]["separate_apps"]))
        ttk.Label(p, text="Program on its own track").grid(row=11, column=0, sticky="w")
        ttk.Entry(p, textvariable=self.separate_app).grid(row=11, column=1, sticky="ew", pady=7)
        self.note(
            p,
            "Separate tracks keep the game, one program and your microphone apart, so a podcast in Firefox can be muted in the clip editor. "
            "Enter the program's file name without .exe, for example firefox, and start it before replay. Needs Windows 11; Windows 10 keeps one mixed track.",
            12,
        )
        self.buttons(
            p,
            [
                ("Start replay", lambda: self.call("replay_start")),
                ("Save clip", lambda: self.call("replay_save")),
                ("Stop replay", lambda: self.call("replay_stop")),
            ],
            13,
        )
        self.note(
            p,
            "Save finalizes the current second, so a clip can include up to one extra second. Disk mode needs the Rust helper and a local NTFS/ReFS disk. Pending segments and files pinned by a save share its size cap; a save can shorten history or stop replay if that cap is exhausted. RAM has additional in-flight and saved-snapshot memory. Save settings before starting replay.",
            14,
        )

    def build_privacy(self, p):
        self.note(
            p,
            "Jarvis recognizes speech locally. It does not send audio to a cloud service. Wake mode listens continuously while unmuted. Recording and replay can capture private conversations if their microphone switches are enabled.",
            0,
        )
        self.check(
            p,
            "I consent to local microphone access for voice control",
            "privacy",
            "onboarded",
            1,
        )
        self.note(
            p,
            "Saving consent enables voice control if the service is running. Mute closes the microphone worker and its loaded speech models. Closing Settings keeps the service available in the notification area; Quit service stops all owned capture processes.",
            2,
        )
        self.buttons(p, [("Mute now", lambda: self.call("mute", muted=True))], 3)
        self.note(
            p,
            "Settings and logs are stored in your local application-data folder. Portable ZIP mode carries the program files; your settings stay in your Windows account. No Discord moderation integration is included in this release.",
            4,
        )

    def build_diagnostics(self, p):
        self.note(
            p,
            "These checks read dependencies and model paths. They do not open the microphone, capture your desktop or change Windows settings.",
            0,
        )
        self.diagnostic_text = tk.Text(p, height=24, wrap="word")
        self.diagnostic_text.grid(row=2, column=0, columnspan=3, sticky="nsew")

        def show(result):
            self.diagnostic_text.delete("1.0", "end")
            self.diagnostic_text.insert("1.0", json.dumps(result, indent=2))

        from .diagnostics import report

        self.buttons(p, [("Run diagnostics", lambda: self.submit(report, show))], 1)

    def settings(self):
        value = copy.deepcopy(self.config)
        for (section, key), variable in self.variables.items():
            target = value[section] if section else value
            previous = target[key]
            raw = variable.get()
            target[key] = (
                int(raw)
                if type(previous) is int
                else float(raw)
                if type(previous) is float
                else raw
            )
        value["activation"]["wake_aliases"] = [
            name.strip() for name in self.wake_aliases.get().split(",") if name.strip()
        ]
        value["recording"]["fps"] = round(self.fps.get())
        value["aliases"] = dict(self.aliases)
        value["app_prefixes"] = dict(self.prefixes)
        value["close_words"] = [word.strip() for word in self.close_words.get().split(",") if word.strip()]
        value["replay"]["separate_apps"] = [
            name.strip().removesuffix(".exe") for name in self.separate_app.get().split(",") if name.strip()
        ]
        value["commands"] = json.loads(self.commands_text.get("1.0", "end"))
        return config.validate(value)

    def save(self):
        try:
            value = self.settings()
        except (ValueError, TypeError, KeyError) as exc:
            messagebox.showerror("Check settings", str(exc), parent=self.root)
            return

        def run():
            from .startup import set_startup

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
            set_startup(value["startup"])
            return value, restart_note(result)

        def saved(outcome):
            self.config, note = outcome
            messagebox.showinfo("Jarvis", "Settings saved." + note, parent=self.root)

        self.submit(run, saved)

    def close(self):
        self.stop.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--page", default="dashboard")
    args = parser.parse_args(argv)
    root = tk.Tk()
    Window(root, args.page)
    root.mainloop()
    return 0
