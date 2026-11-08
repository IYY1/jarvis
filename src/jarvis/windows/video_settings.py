"""Recording/replay video controls shared by the native Windows pages."""
import threading
import tkinter as tk
from tkinter import ttk

from ..encoding import REPLAY_VIDEO, replay_fit_seconds, storage_estimate, auto_bitrate

PRESETS = {
    "Recommended": ("best", "native", 60, "bitrate", "very_high"),
    "High quality · 10-bit": ("ten", "native", 60, "quality", "ultra"),
    "High frame rate · 120 fps": ("best", "native", 120, "bitrate", "very_high"),
    "High frame rate · 240 fps": ("best", "native", 240, "bitrate", "very_high"),
    "Small files · 1080p": ("best", "1080", 30, "bitrate", "very_high"),
    "Compatible · H.264": ("h264", "native", 60, "bitrate", "very_high"),
}


def sound_tracks(window):
    """How many sound tracks a replay keeps with the settings on screen (see
    encoding.audio_plan): the sources, plus "All sound" when they are separate."""
    v = window.variable
    if v("replay", "sources").get() == "specific":
        programs = [n for n in getattr(window, "separate_app", tk.StringVar()).get().split(",") if n.strip()]
        sources = bool(v("replay", "game_track").get()) + len(programs) + bool(v("replay", "microphone").get())
    else:
        sources = bool(v("replay", "game_audio").get()) + bool(v("replay", "microphone").get())
    if sources > 1:
        return sources + 1 if v("replay", "audio_tracks").get() == "separate" else 1
    return sources


class VideoSettings:
    def __init__(self, window, card):
        self.window, self.formats, self.panels, self.presets = window, {}, {}, {}
        self.applying = False
        self.testing = False
        self.display = {"width": window.root.winfo_screenwidth(), "height": window.root.winfo_screenheight()}
        window.switch(card, "Replay has its own video settings", "replay", "own_video",
                      "Off: replay uses recording settings. On: edit the Replay profile below.")
        self.profile = tk.StringVar(value="Recording")
        picker = ttk.Combobox(card, values=("Recording", "Replay"), textvariable=self.profile, state="readonly", width=20)
        picker.bind("<<ComboboxSelected>>", lambda _: self.show())
        window.row(card, "Settings for", picker)
        self.holder = ttk.Frame(card, style="Card.TFrame")
        window.wide(card, self.holder)
        self.holder.columnconfigure(0, weight=1)
        for section in ("recording", "replay"):
            p = ttk.Frame(self.holder, style="Card.TFrame")
            p.columnconfigure(0, weight=1)
            p.next_row = 0
            p.grid(row=0, column=0, sticky="ew")
            self.panels[section] = p
            preset = tk.StringVar(value="Custom")
            self.presets[section] = preset
            pick = ttk.Combobox(p, values=(*PRESETS, "Custom"), textvariable=preset, state="readonly", width=29)
            pick.bind("<<ComboboxSelected>>", lambda _, section=section: self.apply(section))
            window.row(p, "Preset", pick, "Pick one and you're done. Every detail is below if you want it.")
            more = ttk.Frame(p, style="Card.TFrame")
            more.columnconfigure(0, weight=1)
            more.next_row = 0
            p = self.details(p, more)
            window.choice(p, "Format", section, "codec", {
                "Automatic · best for this GPU": "auto",
                "H.264 · easy sharing": "h264", "HEVC": "hevc", "AV1": "av1",
                "HEVC 10-bit · SDR": "hevc_10bit", "AV1 10-bit · SDR": "av1_10bit",
                **({"H.264 · CPU compatibility": "libx264"} if section == "recording" else {})})
            window.choice(p, "Resolution limit", section, "resolution", {"Display's own": "native", "4K": "2160", "2K": "1440", "1080p": "1080", "720p": "720"},
                          "Keeps aspect ratio and never scales up. Scaling uses CPU processing; encoding stays on the selected GPU.")
            window.number(p, "Frames per second", section, "fps", 1, 240, 1,
                          "1–240 fps. Actual capture rate depends on the display, game and encoder.")
            window.choice(p, "Size control", section, "rate_control", {"Fixed bitrate": "bitrate", "Consistent quality": "quality"})
            window.switch(p, "Match bitrate to size and frame rate", section, "bitrate_auto")
            window.number(p, "Bitrate (Mbps)", section, "bitrate_mbps", 1, 500, 1, "Used when automatic bitrate is off.")
            window.choice(p, "Quality", section, "quality_preset", {"Compact": "medium", "Balanced": "high", "Sharp": "very_high", "Maximum": "ultra"})
            for key in REPLAY_VIDEO:
                window.variable(section, key).trace_add("write", lambda *_, section=section: self.edited(section))
            self.match(section)
        self.summary = tk.StringVar()
        window.line(card, ttk.Label(card, textvariable=self.summary, style="Row.TLabel", wraplength=640))
        self.toggle = window.buttons(card, [("Show all options", self.fold)]).winfo_children()[0]
        more = ttk.Frame(card, style="Card.TFrame")
        more.columnconfigure(0, weight=1)
        more.next_row = 0
        self.details(card, more)
        window.choice(more, "Encoder", "recording", "encoder", {"Automatic · GPU": "auto", "NVIDIA · NVENC": "nvenc", "AMD · AMF": "amf", "Intel · Quick Sync": "qsv", "CPU · H.264 only": "cpu"})
        self.info = tk.StringVar(value="Test formats on the display's GPU before choosing AV1 or 10-bit.")
        window.line(more, ttk.Label(more, textvariable=self.info, style="Caption.TLabel", wraplength=640))
        window.buttons(more, [("Test GPU formats", self.test_formats)])
        self.show()

    def details(self, parent, frame):
        """Technical options, folded away until asked for."""
        self.folds = getattr(self, "folds", []) + [frame]
        frame.grid(row=parent.next_row, column=0, columnspan=2, sticky="ew")
        parent.next_row += 1
        frame.grid_remove()
        return frame

    def fold(self):
        opening = not self.folds[0].winfo_manager() or not self.folds[0].grid_info()
        for frame in self.folds:
            frame.grid() if opening else frame.grid_remove()
        self.toggle.configure(text="Hide options" if opening else "Show all options")

    def match(self, section):
        """Name the preset the saved settings already are, instead of calling them Custom."""
        v = self.window.variable
        for name, (kind, resolution, fps, rate, quality) in PRESETS.items():
            codec = str(v(section, "codec").get())
            codecs = {"best": ("auto", "av1", "hevc", "h264"), "ten": ("av1_10bit", "hevc_10bit")}.get(kind, (kind,))
            if (codec in codecs and str(v(section, "resolution").get()) == resolution and int(float(v(section, "fps").get())) == fps
                    and v(section, "rate_control").get() == rate and bool(v(section, "bitrate_auto").get())):
                self.presets[section].set(name)
                return

    def show(self):
        key = self.profile.get().lower()
        for name, panel in self.panels.items():
            panel.grid() if name == key else panel.grid_remove()
        self.estimate()

    def apply(self, section):
        choice = self.presets[section].get()
        if choice not in PRESETS:
            return
        kind, resolution, fps, rate, quality = PRESETS[choice]
        if kind == "best":
            codec = "auto"   # chosen when capture starts, so it needs no format test first
        elif kind == "ten":
            codec = next((c for c in ("av1_10bit", "hevc_10bit") if self.formats.get(c)), "av1_10bit")
        else:
            codec = kind
        self.applying = True
        self.window.variable("recording", "encoder").set("auto")
        for key, value in dict(codec=codec, resolution=resolution, fps=fps, rate_control=rate,
                               quality_preset=quality, bitrate_auto=True).items():
            self.window.variable(section, key).set(value)
        self.applying = False
        self.window.changed(300)
        self.estimate()

    def edited(self, section):
        if not self.applying:
            self.presets[section].set("Custom")
        self.estimate()

    def estimate(self):
        if not hasattr(self, "summary"):
            return
        section = self.profile.get().lower()
        try:
            cfg = {key: self.window.variable(section, key).get() for key in REPLAY_VIDEO}
            cfg['fps'], cfg['bitrate_mbps'] = int(float(cfg['fps'])), int(float(cfg['bitrate_mbps']))
            mbps = auto_bitrate(cfg, self.display['width'], self.display['height']) if cfg['bitrate_auto'] else cfg['bitrate_mbps']
            self.summary.set(f"{section.capitalize()}: {cfg['fps']} fps · {mbps} Mbps · about {storage_estimate({'bitrate_mbps': mbps}):g} MB/min of video. Quality mode varies.")
            self.show_replay_fit()
        except (ValueError, TypeError, tk.TclError):
            self.summary.set("Enter valid whole-number frame rate and bitrate values.")

    def show_replay_fit(self):
        """How much replay the budget really holds, at the settings replay will use."""
        window = self.window
        v = window.variable
        section = "replay" if v("replay", "own_video").get() else "recording"
        cfg = {key: v(section, key).get() for key in REPLAY_VIDEO}
        cfg["fps"], cfg["bitrate_mbps"] = int(float(cfg["fps"])), int(float(cfg["bitrate_mbps"]))
        if cfg["bitrate_auto"]:
            cfg["bitrate_mbps"] = auto_bitrate(cfg, self.display["width"], self.display["height"])
        tracks = sound_tracks(window)
        seconds, budget = int(float(v("replay", "seconds").get())), int(float(v("replay", "max_mb").get()))
        fit = replay_fit_seconds(cfg, budget, tracks)
        window.replay_fit.set(
            "Its size can't be predicted in Consistent quality mode." if fit is None else
            f"At {cfg['bitrate_mbps']} Mbps the budget holds the full {seconds} seconds." if fit >= seconds else
            f"At {cfg['bitrate_mbps']} Mbps only about {fit} of {seconds} seconds fit. Raise the budget or lower the bitrate.")

    def test_formats(self):
        if self.testing:
            return
        try:
            monitor = self.window.variable("recording", "monitor").get()
            cfg = dict(self.window.settings()['recording'], encoder="auto", color_mode="sdr")
        except ValueError as error:
            self.window.say(str(error), error=True)
            return
        self.testing = True
        self.info.set("Testing the selected display's GPU…")
        def run():
            from .displays import sources
            from .encoding import select_encoder
            from ..recorder import RecorderError
            displays = sources()
            display = next((d for d in displays if d['name'] == monitor), None)
            if display is None:
                raise ValueError("Choose a connected display first.")
            # These are synthetic encoder tests; no display pixels are read.
            probe_display = dict(display, colorManagementPreset="srgb", hdr_known=True)
            found = {}
            for codec in ("h264", "hevc", "av1", "hevc_10bit", "av1_10bit"):
                try:
                    select_encoder(dict(cfg, codec=codec), probe_display, codec.removesuffix('_10bit'))
                    found[codec] = True
                except RecorderError:
                    found[codec] = False
            return display, found
        def done(outcome):
            self.testing = False
            result, error = outcome
            if error:
                self.info.set(error)
                self.window.say(error, error=True)
                return
            self.display, self.formats = result
            self.info.set("Passed GPU tests: " + (", ".join(c.replace('_', ' ') for c, yes in self.formats.items() if yes) or "none; check the graphics driver"))
            self.estimate()
        def worker():
            try: result = run(), None
            except Exception as error: result = None, str(error)
            self.window.post((done, result, None))
        threading.Thread(target=worker, name="jarvis-format-test", daemon=True).start()
