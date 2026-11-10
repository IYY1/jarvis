"""The Home page: what Jarvis is doing, and everything used often, on one screen."""

from __future__ import annotations
import tkinter as tk
from tkinter import ttk

from .. import palettes

GLYPH_RECORD, GLYPH_REPLAY, GLYPH_SHOT = "", "", ""
PHRASES = (("“Clip that”", "Save the replay as a clip"), ("“Take a screenshot”", "Save a picture of the screen"),
           ("“Open Firefox”", "Open or focus an app"), ("“Turn the volume down”", "Change the volume"))
SHOT_READY = "Say “take a screenshot”, or press Take."


class Home:
    def __init__(self, window, page, facts):
        self.window = window
        self.online = False
        self.onboarding(page, facts)
        self.hero(page)
        self.tiles(page)
        self.lower(page)

    def place(self, page, widget, pady=(12, 0)):
        widget.grid(row=page.next_row, column=0, sticky="ew", pady=pady)
        page.next_row += 1

    def panel(self, parent, padding=(18, 14)):
        card = ttk.Frame(parent, style="Panel.TFrame", padding=padding)
        card.columnconfigure(0, weight=1)
        card.next_row = 0
        return card

    def heading(self, card, text):
        ttk.Label(card, text=text, style="CardTitle.TLabel").grid(row=card.next_row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        card.next_row += 1
        card.after_heading = True

    def onboarding(self, page, facts):
        w = self.window
        w.onboard_card = card = self.panel(page)
        self.heading(card, "Allow the microphone")
        for title, text in facts:
            w.line(card, f"{title}. {text}", caption=True)
        w.consent = tk.BooleanVar(value=w.config["privacy"]["onboarded"])
        w.line(card, ttk.Checkbutton(card, text="  I understand when Jarvis opens the microphone", variable=w.consent))
        w.buttons(card, [("Allow and turn on the wake word", w.onboard, "Primary.TButton")])
        self.place(page, card)
        if w.config["privacy"]["onboarded"]:
            card.grid_remove()

    def hero(self, page):
        w = self.window
        card = self.panel(page, padding=(20, 16))
        card.columnconfigure(0, weight=0)
        card.columnconfigure(1, weight=1)
        self.dot = w.tk_widget(tk.Canvas(card, width=12, height=12, highlightthickness=0, borderwidth=0), background="card")
        self.dot_item = self.dot.create_oval(1, 1, 11, 11, width=0)
        self.dot.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        self.paint("error-text")
        w.voice_state = tk.StringVar(value="Connecting…")
        w.voice_title = ttk.Label(card, textvariable=w.voice_state, style="State.TLabel")
        w.voice_title.grid(row=0, column=1, sticky="w")
        w.voice_message = tk.StringVar()
        w.wrapping(ttk.Label(card, textvariable=w.voice_message, style="Caption.TLabel", justify="left")).grid(
            row=1, column=1, sticky="ew")
        # Dictation is a voice action too, so it sits beside Listen instead of in a tile of its own.
        w.dictate_button = ttk.Button(card, text="Start dictation", command=lambda: w.voice_controls.toggle())
        w.dictate_button.grid(row=0, column=2, rowspan=2, padx=(16, 0))
        self.action = ttk.Button(card, text="Start Jarvis", style="Primary.TButton", command=self.act, width=14)
        self.action.grid(row=0, column=3, rowspan=2, padx=(8, 0))
        self.place(page, card)

    def paint(self, role=None):
        """Color the status dot; with no role, repaint it after a theme change."""
        self.dot_role = role or self.dot_role
        self.dot.itemconfigure(self.dot_item, fill=palettes.roles(self.window.palette)[self.dot_role])

    def tile(self, parent, column, glyph, title):
        """One feature: an icon, its state and its buttons."""
        card = self.panel(parent, padding=(16, 14))
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0 if column == 2 else 6))
        card.rowconfigure(1, weight=1)
        top = ttk.Frame(card, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text=glyph, style="Glyph.TLabel").pack(side="left", padx=(0, 10))
        ttk.Label(top, text=title, style="CardTitle.TLabel").pack(side="left")
        state = tk.StringVar()
        self.window.wrapping(ttk.Label(card, textvariable=state, style="Caption.TLabel", justify="left")).grid(
            row=1, column=0, sticky="new", pady=(10, 12))
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=2, column=0, sticky="ew")
        return state, actions

    def tiles(self, page):
        w = self.window
        row = ttk.Frame(page)
        for column in range(3):
            row.columnconfigure(column, weight=1, uniform="tile")
        self.place(page, row)

        # Most used first: the replay, then recording, then screenshots.
        self.replay_text, actions = self.tile(row, 0, GLYPH_REPLAY, "Instant replay")
        self.replay_button = ttk.Button(actions, text="Start", width=8, command=self.replay_action)
        self.replay_button.pack(side="left", padx=(0, 6))
        self.replay_stop = ttk.Button(actions, text="Stop", width=8, command=lambda: w.call("replay_stop"))
        self.replay_buttons, self.replay_stops = [self.replay_button], [self.replay_stop]

        self.record_text, actions = self.tile(row, 1, GLYPH_RECORD, "Recording")
        self.record_button = ttk.Button(actions, text="Start", width=8, command=self.toggle_recording)
        self.record_button.pack(side="left", padx=(0, 6))
        self.record_buttons = [self.record_button]
        ttk.Button(actions, text="Folder", width=8, command=w.open_folder).pack(side="left")

        self.shot_text, actions = self.tile(row, 2, GLYPH_SHOT, "Screenshot")
        self.shot_button = ttk.Button(actions, text="Take", width=8, command=lambda: w.call("screenshot"))
        self.shot_button.pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Folder", width=8, command=w.open_screenshot_folder).pack(side="left")
        self.shot_text.set(SHOT_READY)

    def lower(self, page):
        w = self.window
        row = ttk.Frame(page)
        row.columnconfigure(0, weight=1, uniform="half")
        row.columnconfigure(1, weight=1, uniform="half")
        self.place(page, row, pady=(12, 0))

        quick = self.panel(row)
        quick.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.heading(quick, "Quick settings")
        w.arabic_enabled = tk.BooleanVar(value=w.config["recognition"]["language"] == "ar_en")
        w.row(quick, "Arabic commands and dictation",
              ttk.Checkbutton(quick, variable=w.arabic_enabled, command=lambda: w.voice_controls.toggle_arabic()))
        w.switch(quick, "Start replay with Jarvis", "replay", "autostart")
        w.switch(quick, "Start Jarvis when I sign in", "", "startup")

        say = self.panel(row)
        say.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.heading(say, "Try saying")
        for phrase, detail in PHRASES:
            w.row(say, phrase, ttk.Label(say, text=detail, style="Caption.TLabel"))

    # Actions ---------------------------------------------------------------

    def act(self):
        w = self.window
        if not self.online:
            from .startup import start_service
            w.submit(start_service, lambda _: w.root.after(600, w.refresh_status))
        elif w.status.get("muted", True):
            w.toggle_mute()
        else:
            w.listen()

    def toggle_recording(self):
        w = self.window
        if w.status.get("recording", {}).get("state") == "recording":
            w.call("record_stop")
        else:
            w.start_recording()

    def replay_action(self):
        on = self.window.status.get("replay", {}).get("state") == "on"
        self.window.call("replay_save" if on else "replay_start")

    # State -----------------------------------------------------------------

    def went_offline(self):
        w = self.window
        self.online = False
        w.voice_state.set("Jarvis isn't running")
        w.voice_message.set("Start it to use your voice. Settings you change here are still saved.")
        self.paint("error-text")
        self.action.configure(text="Start Jarvis", style="Primary.TButton")
        self.record_text.set("Start Jarvis to record your screen.")
        self.replay_text.set("Start Jarvis to keep the last moments of your screen.")
        self.shot_text.set("Start Jarvis to take screenshots.")
        self.shot_button.configure(state="disabled")

    def update(self, status):
        self.online = True
        state = status.get("state", "paused")
        self.paint({"idle": "live", "listening": "accent", "processing": "accent", "dictating": "accent",
                    "error": "error-text"}.get(state, "text-muted"))
        muted = bool(status.get("muted", True))
        self.action.configure(text="Unmute" if muted else "Cancel" if state == "listening" else "Listen now")
        self.show_recording(status.get("recording", {}))
        if str(self.shot_button.cget("state")) == "disabled":   # only when it changes: this runs every status
            self.shot_button.configure(state="normal")
            self.shot_text.set(SHOT_READY)
        self.show_replay(status.get("replay", {}))

    def show_recording(self, recording):
        state = recording.get("state", "idle")
        running = state == "recording"
        for button in self.record_buttons:
            button.configure(text="Stop" if running else "Start", style="Danger.TButton" if running else "TButton",
                             state="disabled" if state == "stopping" else "normal")
        message = str(recording.get("message") or "")
        self.record_text.set(message if state == "error" else {"recording": "Recording your screen.", "stopping": "Finishing the file…"}
                             .get(state, "Not recording."))

    def show_replay(self, replay):
        on = replay.get("state") == "on"
        for button in self.replay_buttons:
            button.configure(text="Save clip" if on else "Start", style="Primary.TButton" if on else "TButton")
        for stop in self.replay_stops:
            stop.pack(side="left", padx=(0, 8)) if on else stop.pack_forget()
        message = str(replay.get("message") or "")
        seconds = self.window.config["replay"]["seconds"]
        length = f"{seconds // 60} minutes" if seconds % 60 == 0 and seconds >= 120 else f"{seconds} seconds"
        warning = next((part for part in message.split(" · ") if "budget" in part and ("fit" in part or "kept" in part)), "")
        if replay.get("state") == "error":
            self.replay_text.set(message)
        elif on:
            self.replay_text.set(warning or f"Keeping the last {length}. Say “clip that” to save it.")
        else:
            self.replay_text.set(f"Off. Keeps the last {length} of your screen, ready to save.")
