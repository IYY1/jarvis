"""Clips page: a compact library and a small editor (see clipedit.py).

The preview plays the original files and skips removed sections, so what you
see follows the edit without rendering anything. Exports run on a thread and
never touch the original. GStreamer plays one audio track (the first); the
Sound switches (off mutes a track) apply to the export, which says so in the editor.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

from gi.repository import Gdk, Gio, GLib, Gtk

from . import clipedit
from .transcriber import release_unused_memory

LIBRARY_ROWS = 8


def clock(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes):02d}:{rest:04.1f}"


def losslesscut() -> list[str] | None:
    """How to launch LosslessCut if it is installed (it stays a separate app)."""
    if shutil.which("losslesscut"):
        return ["losslesscut"]
    if shutil.which("flatpak"):
        found = subprocess.run(["flatpak", "info", "no.mifi.losslesscut"], capture_output=True)
        if found.returncode == 0:
            return ["flatpak", "run", "no.mifi.losslesscut"]
    return None


class Timeline(Gtk.DrawingArea):
    """Kept segments, marks and the playhead on the edited timeline. Click to seek."""

    def __init__(self, on_seek: Callable[[float], None]):
        super().__init__()
        self.edit: clipedit.Edit | None = None
        self.position = 0.0
        self.marks: list[float | None] = [None, None]
        self.on_seek = on_seek
        self.set_content_height(44)
        self.set_hexpand(True)
        self.set_focusable(True)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Edit timeline; click to move the playhead"])
        self.set_draw_func(self._draw)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda _g, _n, x, _y: self._seek(x))
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-update", lambda g, dx, _dy: self._seek(g.get_start_point()[1] + dx))
        self.add_controller(drag)

    def _seek(self, x: float) -> None:
        if self.edit and self.get_width() > 0:
            self.on_seek(max(0.0, min(1.0, x / self.get_width())) * self.edit.duration)

    def _draw(self, _area, cr, width, height) -> None:
        if not self.edit or self.edit.duration <= 0:
            return
        total = self.edit.duration
        x_of = lambda t: t / total * width
        colors = [(0.76, 0.68, 1.0), (0.55, 0.47, 0.8)]   # alternate segments: accent / muted accent
        t = 0.0
        for i, segment in enumerate(self.edit.segments):
            cr.set_source_rgb(*colors[i % 2])
            cr.rectangle(x_of(t), 10, max(1.0, x_of(segment.length) - 1), height - 20)
            cr.fill()
            source = self.edit.sources[segment.source]
            cr.set_source_rgba(0.08, 0.07, 0.11, 0.55)     # keyframes: where Fast cuts can start
            for key in source.keyframes:
                if segment.start <= key <= segment.end:
                    cr.rectangle(x_of(t + key - segment.start), height - 14, 1, 4)
            cr.fill()
            t += segment.length
        if None not in self.marks:
            a, b = sorted(self.marks)
            cr.set_source_rgba(0.91, 0.77, 0.42, 0.35)     # gold: marked section
            cr.rectangle(x_of(a), 4, max(2.0, x_of(b - a)), height - 8)
            cr.fill()
        cr.set_source_rgb(0.91, 0.77, 0.42)
        for mark in self.marks:
            if mark is not None:
                cr.rectangle(x_of(mark) - 1, 2, 2, height - 4)
        cr.fill()
        cr.set_source_rgb(0.95, 0.94, 0.98)                # playhead
        cr.rectangle(x_of(self.position) - 1, 0, 3, height)
        cr.fill()


def _release_memory() -> bool:
    release_unused_memory()
    return False



def describe(track: dict) -> str:
    """A track's format, as LosslessCut shows it: "AAC · stereo · 48 kHz"."""
    codecs = {"aac": "AAC", "opus": "Opus", "flac": "FLAC", "mp3": "MP3", "vorbis": "Vorbis", "ac3": "AC-3", "eac3": "E-AC-3"}
    layouts = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}
    parts = [codecs.get(str(track.get("codec_name")), str(track.get("codec_name", "audio")).upper())]
    if track.get("channels"):
        parts.append(layouts.get(int(track["channels"]), f"{track['channels']} channels"))
    if str(track.get("sample_rate", "")).isdigit():
        parts.append(f"{int(track['sample_rate']) / 1000:g} kHz")
    return " · ".join(parts)

class ClipsPage:
    """Builds the Clips page into `body` and owns the player and exports."""

    def __init__(self, window: Any, body: Any, card: Callable, label: Callable, button: Callable, box: Callable,
                 notice: Callable[[str, bool], Any]):
        self.window, self._card, self._label, self._button, self._box = window, card, label, button, box
        self.notice = notice
        self.folder = Path.home()
        self.found: list[dict] = []
        self.show_all = False
        self.edit: clipedit.Edit | None = None
        self.loaded = -1          # index of the source in the player
        self.export: clipedit.Export | None = None
        self.player: Any = None
        self.result: Path | None = None
        self.track_checks: list[Any] = []
        self.track_details: list[Any] = []

        self.editor = card("Editor")
        self.editor.set_visible(False)
        self.title = label("", "row-title")
        self.editor.append(self.title)
        self.video_slot = box(0)
        self.editor.append(self.video_slot)
        self.timeline = Timeline(self.seek)
        self.editor.append(self.timeline)
        self.time = label("", "caption")
        self.editor.append(self.time)
        tools = box(8, True)
        for text, tip, action in (
                ("Mark in", "Start of a section (I)", lambda: self.mark(0)),
                ("Mark out", "End of a section (O)", lambda: self.mark(1)),
                ("Remove section", "Cut out the marked section (Delete)", self.remove),
                ("Keep only section", "Trim to the marked section (K)", self.keep),
                ("Split", "Split at the playhead (S)", self.split),
                ("Undo", "Undo the last change (Ctrl+Z)", self.undo)):
            b = button(text, action)
            b.set_tooltip_text(tip)
            tools.append(b)
        self.editor.append(tools)
        join = box(8, True)
        self.join_choice = Gtk.DropDown.new_from_strings(["Join another clip…"])
        self.join_choice.update_property([Gtk.AccessibleProperty.LABEL], ["Clip to join at the end"])
        join.append(self.join_choice)
        join.append(button("Join at end", self.join))
        self.editor.append(join)
        self.tracks_box = box(8)
        self.editor.append(self.tracks_box)
        self.mix = Gtk.CheckButton(label="Combine the remaining tracks into one (plays in every player)", active=True)
        self.mix.set_visible(False)
        self.editor.append(self.mix)
        export_row = box(8, True)
        self.mode = Gtk.DropDown.new_from_strings(["Fast · original quality, cuts on keyframes",
                                                   "Exact · cuts on the frame, re-encodes video"])
        self.mode.update_property([Gtk.AccessibleProperty.LABEL], ["Export mode"])
        self.mode.connect("notify::selected", lambda *_: self.refresh())
        export_row.append(self.mode)
        self.export_button = button("Export copy", self.start_export, "primary")
        export_row.append(self.export_button)
        self.cancel_button = button("Cancel export", self.cancel_export)
        self.cancel_button.set_visible(False)
        export_row.append(self.cancel_button)
        self.editor.append(export_row)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_visible(False)
        self.editor.append(self.progress)
        self.status = label("", "caption")
        self.status.set_selectable(True)
        self.editor.append(self.status)
        after = box(8, True)
        self.open_result = button("Open export", lambda: self.launch(self.result, False))
        self.show_result = button("Show in folder", lambda: self.launch(self.result, True))
        for b in (self.open_result, self.show_result):
            b.set_visible(False)
            after.append(b)
        self.lossless = button("Open in LosslessCut", self.open_losslesscut)
        self.lossless.set_tooltip_text("LosslessCut is a separate, more complete editor (GPL-2.0), if installed")
        self.lossless.set_visible(False)
        after.append(self.lossless)
        after.append(button("Close editor", self.close))
        self.editor.append(after)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.editor.add_controller(keys)
        body.append(self.editor)

        library = card("Library")
        self.caption = label("Your saved clips appear here.", "caption")
        library.append(self.caption)
        self.list = box(0)
        self.list.add_css_class("group")
        library.append(self.list)
        more = box(8, True)
        self.more = button("Show all", self._toggle_more)
        more.append(self.more)
        more.append(button("Refresh", lambda: self.window._refresh_clips()))
        library.append(more)
        body.append(library)

    # Library ---------------------------------------------------------------

    def render(self, folder: Path, found: list) -> None:
        self.folder, self.found = folder, found
        child = self.list.get_first_child()
        while child:
            self.list.remove(child)
            child = self.list.get_first_child()
        self.caption.set_text(f"{len(found)} clips in {folder}" if found else
                              f"No clips in {folder} yet. Say “clip that” while instant replay is on.")
        shown = found if self.show_all else found[:LIBRARY_ROWS]
        self.more.set_visible(len(found) > LIBRARY_ROWS)
        self.more.set_label("Show fewer" if self.show_all else f"Show all {len(found)}")
        for clip in shown:
            row = self._box(12, True)
            row.add_css_class("row")
            labels = self._box(2)
            labels.set_hexpand(True)
            name = self._label(clip["name"], "row-title", wrap=False)
            name.set_ellipsize(3)   # Pango.EllipsizeMode.END
            labels.append(name)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(clip["modified"]))
            labels.append(self._label(f"{when} · {clip['bytes'] / 1048576:.1f} MB", "caption"))
            edit = self._button("Open", lambda path=clip["path"]: self.open(path))
            edit.update_property([Gtk.AccessibleProperty.LABEL], [f"Open {clip['name']} in the editor"])
            edit.set_valign(Gtk.Align.CENTER)
            row.append(labels)
            row.append(edit)
            self.list.append(row)
        names = ["Join another clip…", *(c["name"] for c in found)]
        self.join_choice.set_model(Gtk.StringList.new(names))

    def _toggle_more(self) -> None:
        self.show_all = not self.show_all
        self.render(self.folder, self.found)

    # Editor ----------------------------------------------------------------

    def open(self, path: str) -> None:
        self.notice("Reading the clip…", False)
        def probe() -> None:
            try:
                source = clipedit.probe(path)
                GLib.idle_add(self._opened, source)
            except (clipedit.EditError, OSError, ValueError) as error:
                GLib.idle_add(self.notice, f"Can't open {Path(path).name}: {error}", True)
        threading.Thread(target=probe, daemon=True).start()

    def _opened(self, source: clipedit.Source) -> bool:
        self.edit = clipedit.Edit.of(source)
        self.timeline.edit = self.edit
        self.timeline.marks = [None, None]
        self.result = None
        self.title.set_text(Path(source.path).name)
        self._tracks(source)
        self.loaded = -1
        self._load(0, 0.0)
        self.editor.set_visible(True)
        self.lossless.set_visible(losslesscut() is not None)
        self.refresh()
        self.notice("Opened in the editor. The original file is never changed.", False)
        self.timeline.grab_focus()
        return False

    def _tracks(self, source: clipedit.Source) -> None:
        """The Sound list, as in LosslessCut: one row per track with its name and
        format, a Keep switch, and Only this. Silent tracks are found after opening."""
        child = self.tracks_box.get_first_child()
        while child:
            self.tracks_box.remove(child)
            child = self.tracks_box.get_first_child()
        self.track_checks, self.track_details = [], []
        self.mix.set_visible(len(source.audio) > 1)
        if not source.audio:
            self.tracks_box.append(self._label("This clip has no sound.", "caption"))
            return
        header = self._box(8, True)
        title = self._label(f"Sound tracks · {len(source.audio)}", "row-title")
        title.set_hexpand(True)
        title.set_valign(Gtk.Align.CENTER)
        header.append(title)
        if len(source.audio) > 1:
            header.append(self._button("Keep all", lambda: self._keep(lambda _i: True)))
            header.append(self._button("Mute all", lambda: self._keep(lambda _i: False)))
        self.tracks_box.append(header)
        rows = self._box(0)
        rows.add_css_class("group")
        for index, track in enumerate(source.audio):
            row = self._box(12, True)
            row.add_css_class("row")
            info = self._box(2)
            info.set_hexpand(True)
            info.set_valign(Gtk.Align.CENTER)
            info.append(self._label(track["name"], "row-title"))
            detail = self._label(describe(track), "caption")
            info.append(detail)
            row.append(info)
            switch = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
            switch.update_property([Gtk.AccessibleProperty.LABEL], [f"Keep {track['name']}"])
            switch.connect("notify::active", lambda *_: self.refresh())
            if len(source.audio) > 1:
                only = self._button("Only this", lambda i=index: self._keep(lambda j: j == i), "quiet")
                only.set_valign(Gtk.Align.CENTER)
                only.update_property([Gtk.AccessibleProperty.LABEL], [f"Keep only {track['name']}"])
                row.append(only)
            row.append(switch)
            self.track_checks.append(switch)
            self.track_details.append(detail)
            rows.append(row)
        self.tracks_box.append(rows)
        if len(source.audio) > 1:
            self.tracks_box.append(self._label("Turn a track off to mute it in the exported clip, for example Firefox "
                                               "behind the game while Discord stays. The original keeps every track. "
                                               "The preview plays the first track only.", "caption"))
            self._find_silence(source)
        else:
            self.tracks_box.append(self._label("One track: sound mixed while recording can't be separated afterwards. "
                                               "Choose Separate tracks in Capture → Instant replay, and each app gets "
                                               "its own track in future clips.", "caption"))

    def _keep(self, keep: Callable[[int], bool]) -> None:
        for index, switch in enumerate(self.track_checks):
            switch.set_active(keep(index))

    def _find_silence(self, source: clipedit.Source) -> None:
        """Mark tracks with no sound (an app that stayed quiet) and start them off."""
        def measure() -> None:
            try:
                peaks = clipedit.loudness(source.path, len(source.audio))
            except (OSError, subprocess.SubprocessError):
                return
            GLib.idle_add(show, peaks)
        def show(peaks: list) -> bool:
            if not self.edit or self.edit.sources[0].path != source.path or len(peaks) != len(self.track_checks):
                return False   # another clip is open now
            for switch, detail, peak, track in zip(self.track_checks, self.track_details, peaks, source.audio):
                if peak is not None and peak <= clipedit.SILENT_DB:
                    detail.set_text(f"{describe(track)} · no sound in this clip")
                    switch.set_active(False)
            return False
        threading.Thread(target=measure, name="jarvis-clip-loudness", daemon=True).start()

    def _load(self, index: int, at: float, play: bool = False) -> None:
        if self.player is None:
            self.player = Gtk.Video(autoplay=False)
            self.player.set_size_request(-1, 300)
            self.player.set_hexpand(True)
            self.video_slot.append(self.player)
        if index != self.loaded:
            self.loaded = index
            self.player.set_filename(self.edit.sources[index].path)
            stream = self.player.get_media_stream()
            if stream is not None:
                stream.connect("notify::timestamp", self._tick)
                stream.connect("notify::error", self._error)
                def prepared(s: Any, _p: Any) -> None:
                    if s.is_prepared():
                        s.seek(int(at * 1e6))
                        if play:
                            s.play()
                stream.connect("notify::prepared", prepared)
        stream = self.player.get_media_stream()
        if stream is not None and stream.is_prepared():
            stream.seek(int(at * 1e6))

    def _error(self, stream: Any, _pspec: Any) -> None:
        if stream.get_error() is not None:
            self.notice("This clip can't play here; editing and export still work. For playback install "
                        "gst-plugins-good, or use Open export / your own player.", True)

    def _tick(self, stream: Any, _pspec: Any) -> None:
        """Follow the player on the edit timeline, skipping removed sections."""
        if self.edit is None or self.loaded < 0:
            return
        now = stream.get_timestamp() / 1e6
        t = 0.0
        for i, s in enumerate(self.edit.segments):
            if s.source == self.loaded and s.start - 0.05 <= now < s.end:
                self.timeline.position = t + max(0.0, now - s.start)
                break
            t += s.length
        else:
            if stream.get_playing():
                following = next(((i, s) for i, s in enumerate(self.edit.segments)
                                  if s.source != self.loaded or s.start > now), None)
                if following is None:
                    stream.pause()
                else:
                    self._load(following[1].source, following[1].start, play=True)
                    return
        self.timeline.queue_draw()
        self.time.set_text(self._summary())

    def seek(self, t: float) -> None:
        if self.edit is None:
            return
        index, at = self.edit.locate(t)
        self.timeline.position = t
        self._load(self.edit.segments[index].source, at)
        self.timeline.queue_draw()
        self.time.set_text(self._summary())

    def mark(self, which: int) -> None:
        if self.edit:
            self.timeline.marks[which] = self.timeline.position
            self.refresh()

    def _marked(self) -> tuple[float, float] | None:
        if None in self.timeline.marks:
            self.notice("Mark in and Mark out first (I and O while the timeline has focus).", True)
            return None
        return tuple(sorted(self.timeline.marks))

    def _change(self, action: Callable[[], None]) -> None:
        try:
            action()
        except clipedit.EditError as error:
            self.notice(str(error), True)
            return
        self.timeline.marks = [None, None]
        self.timeline.position = min(self.timeline.position, self.edit.duration)
        self.refresh()

    def remove(self) -> None:
        marked = self._marked()
        if marked:
            self._change(lambda: self.edit.remove_range(*marked))

    def keep(self) -> None:
        marked = self._marked()
        if marked:
            self._change(lambda: self.edit.trim(*marked))

    def split(self) -> None:
        if self.edit:
            self._change(lambda: self.edit.split(self.timeline.position))

    def undo(self) -> None:
        if self.edit and self.edit.undo():
            self.refresh()

    def join(self) -> None:
        choice = self.join_choice.get_selected()
        if not self.edit or choice == 0 or choice > len(self.found):
            self.notice("Choose a clip to join from the list first.", True)
            return
        path = self.found[choice - 1]["path"]
        def probe() -> None:
            try:
                source = clipedit.probe(path)
                GLib.idle_add(lambda: (self._change(lambda: self.edit.append(source)), False)[1])
            except (clipedit.EditError, OSError, ValueError) as error:
                GLib.idle_add(self.notice, f"Can't join {Path(path).name}: {error}", True)
        threading.Thread(target=probe, daemon=True).start()

    def _key(self, _controller, keyval, _code, state) -> bool:
        if state & Gdk.ModifierType.CONTROL_MASK:
            if keyval == Gdk.KEY_z:
                self.undo()
                return True
            return False
        actions = {Gdk.KEY_i: lambda: self.mark(0), Gdk.KEY_o: lambda: self.mark(1), Gdk.KEY_s: self.split,
                   Gdk.KEY_k: self.keep, Gdk.KEY_Delete: self.remove}
        if keyval in actions and isinstance(self.window.get_focus(), Timeline):
            actions[keyval]()
            return True
        return False

    def _tracks_chosen(self) -> list[int]:
        return [i for i, check in enumerate(self.track_checks) if check.get_active()]

    def _summary(self) -> str:
        if not self.edit:
            return ""
        parts = [f"{clock(self.timeline.position)} / {clock(self.edit.duration)}",
                 f"{len(self.edit.segments)} part{'s' if len(self.edit.segments) != 1 else ''}"]
        if None not in self.timeline.marks:
            a, b = sorted(self.timeline.marks)
            parts.append(f"marked {clock(a)}–{clock(b)}")
        if self.mode.get_selected() == 0:
            fast = self.edit.fast_duration()
            if fast - self.edit.duration > 0.05:
                parts.append(f"Fast export starts parts on keyframes: {clock(fast)} long")
        return " · ".join(parts)

    def refresh(self) -> None:
        if not self.edit:
            return
        self.timeline.queue_draw()
        self.time.set_text(self._summary())
        tracks = self._tracks_chosen()
        problems = self.edit.fast_problems(tracks) if self.mode.get_selected() == 0 else []
        busy = self.export is not None
        self.export_button.set_sensitive(not busy and not problems)
        self.mix.set_sensitive(len(tracks) > 1)
        muted = [s for s in self.track_checks if not s.get_active()]
        if muted and len(muted) == len(self.track_checks):
            self.status.set_text("Every track is off, so the export will be silent.")
            return
        if problems:
            self.status.set_text("Fast export needs matching clips: " + "; ".join(problems[:3]) +
                                 ". Choose Exact, which converts them.")
        elif not busy and not self.result:
            self.status.set_text("Exports go beside the first clip as “… edit N”. The original is not changed.")

    # Export ----------------------------------------------------------------

    def start_export(self) -> None:
        if not self.edit or self.export is not None:
            return
        tracks = self._tracks_chosen()
        mode = "fast" if self.mode.get_selected() == 0 else "exact"
        mix = self.mix.get_active() and len(tracks) > 1
        try:
            self.export = clipedit.Export(self.edit, mode, tracks, mix,
                                          progress=lambda f: GLib.idle_add(self.progress.set_fraction, f))
        except clipedit.EditError as error:
            self.notice(str(error), True)
            return
        self.result = None
        for b in (self.open_result, self.show_result):
            b.set_visible(False)
        self.progress.set_fraction(0)
        self.progress.set_visible(True)
        self.cancel_button.set_visible(True)
        self.status.set_text(f"Exporting {self.export.output.name}…" +
                             (" Re-encoding takes a while for long clips." if mode == "exact" else ""))
        self.refresh()
        export = self.export
        def run() -> None:
            try:
                result, error = export.run(), None
            except (clipedit.EditError, OSError) as failure:
                result, error = None, str(failure)
            GLib.idle_add(self._finished, result, error)
        threading.Thread(target=run, daemon=True).start()

    def _finished(self, result: Path | None, error: str | None) -> bool:
        self.export = None
        self.cancel_button.set_visible(False)
        self.progress.set_visible(error is None)
        if error:
            self.status.set_text(error)
            self.notice(error, True)
        else:
            self.result = result
            self.status.set_text(f"Saved {result.name} beside the original.")
            self.notice(f"Saved {result}", False)
            for b in (self.open_result, self.show_result):
                b.set_visible(True)
            self.window._refresh_clips()
        self.refresh()
        return False

    def cancel_export(self) -> None:
        if self.export:
            self.export.cancel()

    def open_losslesscut(self) -> None:
        argv = losslesscut()
        if argv and self.edit:
            subprocess.Popen([*argv, self.edit.sources[0].path], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def launch(self, path: Path | None, folder: bool) -> None:
        if path is None:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
        (launcher.open_containing_folder if folder else launcher.launch)(self.window, None, None)

    def close(self) -> None:
        """Release the player and its GStreamer pipeline (also on leaving the page)."""
        if self.export:
            return   # keep an export running; the page stays usable
        if self.player is not None:
            self.player.set_file(None)
            self.video_slot.remove(self.player)
            self.player = None
            # The C allocator keeps GStreamer's freed decoder and frame memory
            # (about 50 MiB for a 1440p clip); hand it back once its threads stop.
            GLib.timeout_add(1500, _release_memory)
        self.loaded = -1
        self.editor.set_visible(False)
        self.edit = None
