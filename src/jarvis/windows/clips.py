"""Native Windows clip sound selection. Exports copies through bundled FFmpeg."""
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .. import clipedit


class ClipsPanel:
    def __init__(self, window, page):
        self.window = window
        self.source = None
        self.export = None
        self.loading = False
        self.generation = 0
        self.tracks = []
        self.touched = set()
        self.files = []
        self.scanning = False
        window.subtitle(page, "Keep the sound you want and export a new copy. The original is never changed.")
        library = window.group(page, "Clips", "Open a saved recording or replay. Already mixed sound cannot be separated.")
        window.buttons(library, [("Open clip…", self.choose), ("Refresh recordings", self.refresh),
                                 ("Open recordings folder", window.open_folder)])
        self.library = ttk.Treeview(library, columns=("file",), show="headings", height=5, selectmode="browse")
        self.library.heading("file", text="Saved recordings and replays · newest first")
        self.library.column("file", width=520, stretch=True)
        window.line(library, self.library)
        self.library.bind("<Double-1>", self.open_selected)
        self.library.bind("<Return>", self.open_selected)
        window.buttons(library, [("Open selected", self.open_selected)])

        editor = window.group(page, "Sound tracks", "Separate tracks must be enabled before capture. Windows 11 separates one program from the rest of system sound.")
        self.name = tk.StringVar(value="Open a clip to choose its tracks.")
        window.line(editor, ttk.Label(editor, textvariable=self.name, style="Card.TLabel", wraplength=520))
        window.buttons(editor, [("Keep all", lambda: self.keep_all(True)), ("Mute all", lambda: self.keep_all(False))])
        self.track_box = ttk.Frame(editor, style="Card.TFrame")
        window.line(editor, self.track_box)
        self.track_box.columnconfigure(0, weight=1)
        self.mix = tk.BooleanVar(value=False)
        window.row(editor, "Mix kept tracks into one", ttk.Checkbutton(editor, variable=self.mix),
                   "Leave off to keep the selected tracks separate.")

        output = window.group(page, "Export copy")
        self.start, self.end = tk.StringVar(value="0"), tk.StringVar(value="0")
        window.row(output, "Start (seconds)", ttk.Entry(output, textvariable=self.start, width=12))
        window.row(output, "End (seconds)", ttk.Entry(output, textvariable=self.end, width=12))
        self.mode = tk.StringVar(value="Fast (keyframes)")
        window.row(output, "Cut mode", ttk.Combobox(output, textvariable=self.mode,
                   values=("Fast (keyframes)", "Exact (re-encode)"), state="readonly", width=24),
                   "Fast copies video and may begin at an earlier keyframe. Exact re-encodes on the CPU on Windows.")
        self.progress = tk.StringVar(value="")
        window.line(output, ttk.Label(output, textvariable=self.progress, style="Caption.TLabel", wraplength=520))
        window.buttons(output, [("Export copy", self.export_copy, "Primary.TButton"), ("Cancel export", self.cancel)])

    def work(self, run, done):
        # Probing and exports have their own worker; settings saves stay responsive.
        def worker():
            try:
                outcome = run(), None
            except Exception as error:
                outcome = None, str(error)
            self.window.post((done, outcome, None))
        threading.Thread(target=worker, name="jarvis-clip-edit", daemon=True).start()

    def refresh(self):
        if self.scanning:
            return
        self.scanning = True
        folder = Path(self.window.variable("recording", "output_dir").get()).expanduser()
        def scan():
            files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
                     and not p.name.startswith(".")]
            return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:200]
        def done(outcome):
            self.scanning = False
            files, error = outcome
            if error:
                self.window.say(error, error=True)
                return
            self.files = files
            self.library.delete(*self.library.get_children())
            for i, path in enumerate(files):
                self.library.insert("", "end", iid=str(i), values=(str(path.relative_to(folder)),))
            if not files:
                self.window.say("No recordings found. Open clip… can choose a file from another folder.")
        self.work(scan, done)

    def open_selected(self, _event=None):
        selected = self.library.selection()
        if selected:
            self.open(self.files[int(selected[0])])

    def choose(self):
        path = filedialog.askopenfilename(parent=self.window.root, title="Open a clip",
                filetypes=[("Video clips", "*.mp4 *.mkv *.webm *.mov"), ("All files", "*.*")])
        if path:
            self.open(path)

    def open(self, path):
        if self.loading or self.export is not None:
            self.window.say("Wait for the clip to open, or finish or cancel the export first.")
            return
        self.loading = True
        self.name.set("Opening " + Path(path).name + "…")
        self.work(lambda: clipedit.probe(path), self.loaded)

    def loaded(self, outcome):
        self.loading = False
        source, error = outcome
        if error:
            self.name.set(Path(self.source.path).name if self.source else "Open a clip to choose its tracks.")
            self.window.say(error, error=True)
            return
        self.source = source
        self.generation += 1
        generation = self.generation
        self.name.set(f"{Path(source.path).name} · {source.duration:.1f} seconds")
        self.start.set("0")
        self.end.set(str(source.duration))
        self.progress.set("Checking for silent tracks…" if source.audio else "This clip has no sound tracks.")
        for widget in self.track_box.winfo_children():
            widget.destroy()
        self.tracks, self.touched = [], set()
        for i, track in enumerate(source.audio):
            keep = tk.BooleanVar(value=True)
            details = tk.StringVar(value="Checking sound…")
            self.tracks.append((keep, details))
            title = f"{track['name']} · {track.get('codec_name', 'audio')} · {track.get('channels', '?')} channels · {track.get('sample_rate', '?')} Hz"
            ttk.Checkbutton(self.track_box, text=title, variable=keep,
                            command=lambda i=i: self.touched.add(i)).grid(row=i * 2, column=0, sticky="w", pady=(6, 0))
            ttk.Button(self.track_box, text="Only this", command=lambda i=i: self.only(i)).grid(row=i * 2, column=1, padx=(12, 0))
            ttk.Label(self.track_box, textvariable=details, style="Caption.TLabel").grid(row=i * 2 + 1, column=0, sticky="w", padx=(24, 0), pady=(0, 6))
        if source.audio:
            def checked(outcome):
                if generation != self.generation:
                    return
                peaks, error = outcome
                if error:
                    for _keep, detail in self.tracks:
                        detail.set("Sound level could not be checked.")
                else:
                    for i, peak in enumerate(peaks):
                        silent = peak is not None and peak <= clipedit.SILENT_DB
                        self.tracks[i][1].set("No sound in this clip" if silent else "Sound present" if peak is not None else "Sound level unknown")
                        if silent and i not in self.touched:
                            self.tracks[i][0].set(False)
                self.progress.set("Choose the tracks to keep, then Export copy.")
            self.work(lambda: clipedit.loudness(source.path, len(source.audio)), checked)

    def only(self, index):
        for i, (keep, _detail) in enumerate(self.tracks):
            keep.set(i == index)
        self.touched.update(range(len(self.tracks)))

    def keep_all(self, value):
        for keep, _detail in self.tracks:
            keep.set(value)
        self.touched.update(range(len(self.tracks)))

    def selection(self):
        return [i for i, (keep, _detail) in enumerate(self.tracks) if keep.get()]

    def export_copy(self):
        if self.source is None or self.loading or self.export is not None:
            self.window.say("Open a clip and finish or cancel any export first.")
            return
        try:
            start, end = float(self.start.get()), float(self.end.get())
            if not 0 <= start < end <= self.source.duration:
                raise ValueError("Start and end must be within the clip, with end after start.")
            edit = clipedit.Edit.of(self.source)
            edit.trim(start, end)
            exporter = clipedit.Export(edit, "fast" if self.mode.get().startswith("Fast") else "exact",
                                       self.selection(), self.mix.get())
        except (ValueError, OSError) as error:
            self.window.say(str(error), error=True)
            return
        self.export = exporter
        self.progress.set("Exporting copy…")
        def done(outcome):
            self.export = None
            path, error = outcome
            self.progress.set("Export stopped." if error else "Saved: " + str(path))
            self.window.say(error if error else "Saved a new copy: " + str(path), error=bool(error))
        self.work(exporter.run, done)

    def cancel(self):
        if self.export is not None:
            self.export.cancel()

    def close(self):
        self.generation += 1
        self.cancel()
