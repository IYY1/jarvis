"""Native GTK4 settings. The daemon, microphone and models live elsewhere.

All IPC and discovery run outside GTK's main thread. A socket subscription pushes
status changes; opening this window never starts the daemon or opens a microphone.
"""
from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk
except (ImportError, ValueError) as _gtk_error:
    Gtk = None
    GTK_ERROR = str(_gtk_error)
else:
    GTK_ERROR = ""

from . import client
from .commands import normalize
from .encoding import GPU_CODECS, QUALITY_PRESETS, storage_estimate


def _merge(base: dict, patch: dict) -> dict:
    """Merge a settings patch into the current daemon config, preserving siblings."""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def clips(folder: Path, limit: int = 50) -> list[dict]:
    """Saved videos in the recordings folder, newest first; hidden partial files are skipped."""
    try:
        entries = [entry for entry in os.scandir(folder) if entry.is_file() and not entry.name.startswith(".")
                   and entry.name.lower().endswith((".mp4", ".mkv"))]
    except OSError:
        return []
    entries.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    return [{"name": entry.name, "path": entry.path, "modified": entry.stat().st_mtime, "bytes": entry.stat().st_size}
            for entry in entries[:limit]]


def _label(text: str, css: str = "", wrap: bool = True) -> Any:
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_wrap(wrap)
    if css:
        widget.add_css_class(css)
    return widget


def _box(spacing: int = 12, horizontal: bool = False) -> Any:
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL if horizontal else Gtk.Orientation.VERTICAL,
                   spacing=spacing)


def _button(text: str, callback: Callable, css: str = "") -> Any:
    widget = Gtk.Button(label=text)
    widget.connect("clicked", lambda _button: callback())
    if css:
        widget.add_css_class(css)
    return widget


def _audio_summary(config: dict, recording: bool, replay: bool) -> str:
    """Which audio the running capture keeps, so microphone capture is never a surprise."""
    parts = []
    if replay:
        r = config.get("replay", {})
        kept = [name for name, on in (("game and desktop sound", r.get("game_audio")), ("microphone", r.get("microphone"))) if on]
        parts.append("Replay keeps " + (" and ".join(kept) if kept else "no sound"))
    if recording:
        c = config.get("recording", {})
        kept = [name for name, on in (("system sound", c.get("system_audio")), ("microphone", c.get("microphone"))) if on]
        parts.append("Recording keeps " + (" and ".join(kept) if kept else "no sound"))
    return " · ".join(parts)


PAGES = {"dashboard", "commands", "applications", "recording", "clips", "voice", "privacy"}


class JarvisWindow(Gtk.ApplicationWindow if Gtk else object):
    def __init__(self, application: Any, page: str = "dashboard") -> None:
        super().__init__(application=application, title="Jarvis")
        self.set_default_size(1080, 780)
        self.set_size_request(720, 540)
        self.add_css_class("murmur")
        self.config: dict = {}
        self.status: dict = {}
        self.connected = False
        self.closed = False
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._command_rows: list[tuple[Any, str]] = []
        self._command_data: list[dict] = []
        self._command_page = 0
        self._devices: list[dict] = []
        self._monitors: list[dict] = []
        self._recording_devices: list[dict] = []
        self._shown_recording_devices: list[dict] = []
        self._apps: list[dict] = []
        self._last_clip = None
        self._grammar_signature: tuple | None = None
        self._test_active = False
        self._closing_test = False
        self._save_lock = threading.Lock()
        self._build(page)
        self._install_actions()
        self.connect("close-request", self._close)
        self.refresh()

    def _build(self, page: str) -> None:
        outer = _box(0)
        self.set_child(outer)
        header = Gtk.HeaderBar()
        identity = _box(4)
        identity.append(_label("JARVIS", "brand"))
        identity.append(_label("Your desktop, in a few words", "caption"))
        header.set_title_widget(identity)
        self.mute_button = _button("Mute microphone", self._toggle_mute, "mute")
        self.mute_button.set_tooltip_text("Immediately close the voice-control microphone (Ctrl+M)")
        self.mute_button.set_sensitive(False)
        header.pack_end(self.mute_button)
        self.state_pill = _label("Connecting", "pill", wrap=False)
        self.state_pill.set_valign(Gtk.Align.CENTER)
        header.pack_start(self.state_pill)
        self.replay_pill = _label("Replay on", "pill", wrap=False)
        self.replay_pill.add_css_class("replay")
        self.replay_pill.set_valign(Gtk.Align.CENTER)
        self.replay_pill.set_visible(False)
        header.pack_start(self.replay_pill)
        outer.append(header)

        shell = _box(0, True)
        shell.set_vexpand(True)
        outer.append(shell)
        rail = _box(24)
        rail.add_css_class("rail")   # not "sidebar": GtkStackSidebar carries that class too
        rail.set_size_request(196, -1)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        navigation = Gtk.StackSidebar(stack=self.stack)
        navigation.set_vexpand(True)
        rail.append(navigation)
        rail.append(_label("LOCAL BY DEFAULT", "eyebrow"))
        rail_note = _label("Voice stays on your device.\nThe window can close; the service stays available.", "caption")
        rail_note.set_max_width_chars(23)
        rail.append(rail_note)
        shell.append(rail)
        shell.append(self.stack)

        self._dashboard()
        self._voice_page()
        self._commands_page()
        self._applications_page()
        self._recording_page()
        self._clips_page()
        self._privacy_page()
        self.stack.set_visible_child_name(page if page in PAGES else "dashboard")

        self.notice = _label("Connecting to the local service…", "notice")
        self.notice.set_selectable(True)
        outer.append(self.notice)
        for widget in (self.listen_button, self.record_start, self.record_stop, self.test_button,
                       self.replay_save, self.replay_toggle):
            widget.set_sensitive(False)   # until the service answers

    def _page(self, name: str, title: str, subtitle: str) -> Any:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = _box(24)
        body.add_css_class("page")
        heading = _box(8)
        heading.append(_label(title, "page-title"))
        heading.append(_label(subtitle, "subtitle"))
        body.append(heading)
        scroller.set_child(body)
        self.stack.add_titled(scroller, name, {
            "dashboard": "Overview", "commands": "Commands", "applications": "Apps", "clips": "Clips",
            "recording": "Capture", "voice": "Voice", "privacy": "Privacy",
        }[name])
        return body

    def _card(self, title: str, subtitle: str = "") -> Any:
        card = _box(12)
        card.add_css_class("card")
        card.append(_label(title, "section-title"))
        if subtitle:
            card.append(_label(subtitle, "caption"))
        return card

    def _advanced(self, title: str, subtitle: str) -> tuple[Any, Any]:
        """A card whose settings stay folded away until asked for."""
        card = _box(12)
        card.add_css_class("card")
        expander = Gtk.Expander()
        expander.set_label_widget(_label(title, "section-title"))
        content = _box(12)
        content.set_margin_top(12)
        content.append(_label(subtitle, "caption"))
        expander.set_child(content)
        card.append(expander)
        return card, content

    def _row(self, parent: Any, title: str, widget: Any, detail: str = "") -> Any:
        row = _box(16, True)
        labels = _box(4)
        labels.set_hexpand(True)
        labels.set_valign(Gtk.Align.CENTER)
        labels.append(_label(title, "row-title"))
        if detail:
            labels.append(_label(detail, "caption"))
        widget.set_valign(Gtk.Align.CENTER)
        if isinstance(widget, (Gtk.Entry, Gtk.DropDown)):   # one input column per card
            widget.set_hexpand(False)
            widget.set_size_request(280, -1)
        widget.update_property([Gtk.AccessibleProperty.LABEL], [title])
        row.append(labels)
        row.append(widget)
        parent.append(row)
        return widget

    def _entry(self, placeholder: str = "") -> Any:
        entry = Gtk.Entry()
        entry.set_placeholder_text(placeholder)
        entry.set_width_chars(24)
        entry.set_hexpand(True)
        return entry

    def _spin(self, minimum: float, maximum: float, step: float, digits: int = 0) -> Any:
        spin = Gtk.SpinButton.new_with_range(minimum, maximum, step)
        spin.set_digits(digits)
        spin.set_width_chars(6)
        return spin

    def _dashboard(self) -> None:
        body = self._page("dashboard", "Overview", "What Jarvis is doing now, and the controls you reach for most.")
        self.connection_card = self._card("The background service is offline",
                                         "Start the local service to configure Jarvis. Microphone access waits for your privacy choice.")
        actions = _box(8, True)
        self.start_button = _button("Start service", self._start_service, "primary")
        actions.append(self.start_button)
        actions.append(_button("Reconnect", self.refresh))
        self.connection_card.append(actions)
        body.append(self.connection_card)

        self.onboard_banner = self._card("Allow the microphone first",
            "Choose when Jarvis may listen. Complete the privacy setup before the first microphone session.")
        self.onboard_banner.append(_button("Review privacy & activation", lambda: self.stack.set_visible_child_name("privacy"), "primary"))
        body.append(self.onboard_banner)
        self.onboard_banner.set_visible(False)

        status_row = _box(16, True)
        status_row.set_homogeneous(True)
        voice = self._card("VOICE")
        voice.set_hexpand(True)
        self.voice_badge = _label("Service offline", "state-title")
        voice.append(self.voice_badge)
        self.voice_message = _label("Waiting for the service.", "caption")
        self.voice_message.set_selectable(True)
        voice.append(self.voice_message)
        voice_actions = _box(8, True)
        self.listen_button = _button("Listen once", self._listen, "primary")
        self.listen_button.set_tooltip_text("Open one bounded command session (Ctrl+Space)")
        voice_actions.append(self.listen_button)
        self.overview_dictate = _button("Start dictation", self._toggle_dictation)
        self.overview_dictate.set_tooltip_text("Type what you say into the focused text field (Ctrl+D)")
        voice_actions.append(self.overview_dictate)
        self.overview_language = _button("Turn on Arabic", self._toggle_arabic)
        self.overview_language.set_tooltip_text("Switch between English only and Arabic and English")
        voice_actions.append(self.overview_language)
        voice.append(voice_actions)
        self.overview_dictation = _label("Dictation is off.", "caption")
        self.overview_dictation.set_selectable(True)
        voice.append(self.overview_dictation)
        self.overview_copy = _button("Copy text that was not typed", self._copy_held)
        self.overview_copy.set_visible(False)
        voice.append(self.overview_copy)
        status_row.append(voice)

        capture = self._card("CAPTURE")
        capture.set_hexpand(True)
        self.record_badge = _label("Not recording", "state-title")
        self.record_message = _label("Choose a display before your first recording.", "caption")
        self.record_message.set_selectable(True)
        capture.append(self.record_badge)
        capture.append(self.record_message)
        self.overview_replay = _label("Replay off", "row-title")
        capture.append(self.overview_replay)
        self.overview_audio = _label("", "caption")
        capture.append(self.overview_audio)
        capture_actions = _box(8, True)
        self.overview_save = _button("Save replay", lambda: self._action("replay_save"), "primary")
        self.overview_save.set_tooltip_text("Save the replay buffer as a clip (“clip that”)")
        capture_actions.append(self.overview_save)
        self.overview_record = _button("Start recording", self._overview_record)
        capture_actions.append(self.overview_record)
        capture_actions.append(_button("Clips", lambda: self.stack.set_visible_child_name("clips")))
        capture.append(capture_actions)
        status_row.append(capture)
        body.append(status_row)

        recent = self._card("RECENT")
        self.recent_label = _label("Nothing yet. Actions and errors from voice, shortcuts and this window appear here.", "caption")
        self.recent_label.set_selectable(True)
        recent.append(self.recent_label)
        body.append(recent)

        ideas = self._card("Try saying", "After “Jarvis” (or your wake name).")
        grid = Gtk.Grid(column_spacing=24, row_spacing=8)
        for i, (phrase, detail) in enumerate([("“Open Firefox”", "Open or focus an app"),
                                              ("“Clip the last thirty seconds”", "Save part of the replay"),
                                              ("“Listen to me”", "Dictate into the focused field"),
                                              ("“Go to workspace three”", "Move around Hyprland")]):
            line = _box(2)
            line.append(_label(phrase, "example"))
            line.append(_label(detail, "caption"))
            grid.attach(line, i % 2, i // 2, 1, 1)
        ideas.append(grid)
        ideas.append(_button("All commands →", lambda: self.stack.set_visible_child_name("commands")))
        body.append(ideas)

    def _overview_record(self) -> None:
        state = self.status.get("recording", {}).get("state", "idle")
        self._action("record_stop" if state == "recording" else "record_start")

    def _dictation_buttons(self) -> list:
        return [self.dictation_button, self.overview_dictate]

    def _dictation_labels(self) -> list:
        return [self.dictation_state, self.overview_dictation]

    def _held_buttons(self) -> list:
        return [self.dictation_copy, self.overview_copy]

    def _commands_page(self) -> None:
        body = self._page("commands", "Commands", "Every supported phrase, in English and Arabic, and shortcuts to the words you use most.")
        self.command_search = Gtk.SearchEntry()
        self.command_search.set_placeholder_text("Search actions or example phrases")
        self.command_search.connect("search-changed", self._filter_commands)
        body.append(self.command_search)
        self.command_list = _box(12)
        body.append(self.command_list)
        navigation = _box(8, True)
        self.command_previous = _button("Previous", lambda: self._change_command_page(-1))
        self.command_next = _button("Next", lambda: self._change_command_page(1))
        self.command_count = _label("", "caption")
        navigation.append(self.command_previous)
        navigation.append(self.command_count)
        navigation.append(self.command_next)
        body.append(navigation)
        custom = self._card("Your command shortcuts", "A shortcut maps a whole phrase to a supported command. No shell commands are accepted.")
        self.phrase_list = _box(8)
        custom.append(self.phrase_list)
        new = _box(8)
        self.new_phrase = self._entry("Your phrase, for example: capture that")
        self.new_phrase.update_property([Gtk.AccessibleProperty.LABEL], ["Your command shortcut phrase"])
        self.canonical_phrase = self._entry("Supported command, for example: start recording")
        self.canonical_phrase.update_property([Gtk.AccessibleProperty.LABEL], ["Built-in command for shortcut"])
        new.append(self.new_phrase)
        new.append(self.canonical_phrase)
        custom.append(new)
        custom.append(_button("Add command shortcut", self._add_phrase))
        body.append(custom)

    def _applications_page(self) -> None:
        body = self._page("applications", "Apps", "Open, focus or close apps by voice. Give them names you'd say, or add apps Jarvis didn't find.")
        self.app_search = Gtk.SearchEntry()
        self.app_search.set_placeholder_text("Search installed applications")
        self.app_search.connect("search-changed", lambda _entry: self._render_apps())
        body.append(self.app_search)
        self.app_grammar_notice = _label("", "caption")
        self.app_grammar_notice.set_visible(False)
        body.append(self.app_grammar_notice)
        self.app_list = _box(12)
        body.append(self.app_list)
        adding = self._card("Add an app", "Jarvis finds apps that have a launcher entry: desktop entries, Flatpak "
                            "and Snap apps on Linux; Start menu and Store apps on Windows. Programs without one, "
                            "such as AppImages, portable programs or scripts, can be added here. Jarvis creates a "
                            "normal launcher entry, so the app also appears in your app menu.")
        self.add_app_name = self._entry("Name to say, for example: Obsidian")
        self._row(adding, "Name", self.add_app_name)
        self.add_app_path = ""
        pick = _box(8, True)
        pick.append(_button("Choose a program or AppImage…", self._choose_app_file))
        self.add_app_file = _label("No file chosen", "caption")
        pick.append(self.add_app_file)
        adding.append(pick)
        adding.append(_button("Add app", self._add_app, "primary"))
        self.added_apps = _box(8)
        adding.append(self.added_apps)
        self.appimage_hints = _box(8)
        adding.append(self.appimage_hints)
        body.append(adding)
        aliases = self._card("Application aliases", "One alias per line: a phrase and its installed desktop application. Removing an alias keeps the app installed.")
        self.alias_list = _box(8)
        aliases.append(self.alias_list)
        self.alias_name = self._entry("Alias, for example: web browser")
        self.alias_name.update_property([Gtk.AccessibleProperty.LABEL], ["Application alias phrase"])
        self.alias_app = Gtk.DropDown.new_from_strings(["Load installed applications first"])
        self.alias_app.set_hexpand(True)
        # Type to find an app instead of scrolling through every desktop entry.
        self.alias_app.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.alias_app.set_enable_search(True)
        self._alias_model_ids: tuple = ()
        self.alias_app.update_property([Gtk.AccessibleProperty.LABEL], ["Installed application for alias"])
        self.alias_prefix = self._entry("Launch word (leave empty to say only the app name)")
        self.alias_prefix.set_text("open")
        self._row(aliases, "Launch word", self.alias_prefix, "Change open to another word, or clear it. The wake name is still required.")
        aliases.append(self.alias_name)
        aliases.append(self.alias_app)
        aliases.append(_button("Add application alias", self._add_alias))
        body.append(aliases)
        closing = self._card("Close words", "What you say before an app name to close it, for example “close Firefox” or “quit Firefox”.")
        self.close_words = self._entry("close, quit")
        self._row(closing, "Words that close an app", self.close_words,
                  "Separate words with commas; up to eight. They replace “close”, so keep it in the list to still say “close”.")
        closing.append(_button("Save close words", self._save_close_words))
        body.append(closing)

    def _recording_page(self) -> None:
        body = self._page("recording", "Capture", "Recording, instant replay and the sound they keep. Set up once, then use your voice.")
        advanced_card, advanced = self._advanced("Advanced capture settings",
            "Replay storage, audio routing for recordings, the exact video format and legacy encoder tuning.")
        live = self._card("Capture status")
        self.recorder_status = _label("Not recording", "state-title")
        self.recorder_file = _label("Your saved file will appear here.", "caption")
        self.recorder_file.set_selectable(True)
        live.append(self.recorder_status)
        live.append(self.recorder_file)
        controls = _box(8, True)
        self.record_start = _button("Start recording", lambda: self._action("record_start"), "primary")
        self.record_stop = _button("Stop & save", lambda: self._action("record_stop"))
        controls.append(self.record_start)
        controls.append(self.record_stop)
        live.append(controls)
        body.append(live)
        replay = self._card("Instant replay", "Keeps recent screen and sound using the display below. "
                            "RAM writes video only when you save. Choosing Disk writes unsaved screen and sound continuously; "
                            "it can survive a crash until cleanup on next start. Disk writes follow bitrate (roughly 1–10 GB/hour).")
        self.replay_status = _label("Replay off", "state-title")
        self.replay_detail = _label("Start replay to keep the last moments ready to save.", "caption")
        self.replay_detail.set_selectable(True)
        replay.append(self.replay_status)
        replay.append(self.replay_detail)
        replay_controls = _box(8, True)
        self.replay_save = _button("Save replay", lambda: self._action("replay_save"), "primary")
        self.replay_toggle = _button("Start replay", lambda: self._action(
            "replay_stop" if self.status.get("replay", {}).get("state") == "on" else "replay_start"))
        replay_controls.append(self.replay_save)
        replay_controls.append(self.replay_toggle)
        replay.append(replay_controls)
        self.replay_seconds = self._spin(10, 1800, 10)
        self._row(replay, "Replay length (seconds)", self.replay_seconds,
                  "Up to 30 minutes (1800). Say “clip that” for all of it, or “clip the last 30 seconds” / “clip the last 2 minutes”. "
                  "Long RAM replays hold the whole buffer in memory (often 1–3 GB for 20 minutes of gameplay); choose Disk for long replays.")
        self.replay_storage = Gtk.DropDown.new_from_strings(["RAM", "Disk (continuous writes)"])
        self._row(advanced, "Replay storage", self.replay_storage, "Disk requires the Rust controller and a real disk; tmpfs is refused.")
        self.replay_max_mb = self._spin(64, 16384, 64)
        self._row(advanced, "Disk buffer budget (MiB)", self.replay_max_mb,
                  "Disk only: replay stops when the buffer reaches this size, checked every 250 ms. At least 512 MiB, "
                  "because gpu-screen-recorder writes 256 MiB buffer files. RAM replay is not capped on Linux.")
        self.replay_storage.connect("notify::selected", lambda *_: self._replay_storage_changed())
        self.replay_buffer_dir = self._entry("Default private app-data directory")
        self._row(advanced, "Private disk-buffer directory", self.replay_buffer_dir, "Optional dedicated directory. Unsaved buffers are deleted on stop and stale buffers on next start.")
        self.replay_game = Gtk.Switch()
        self._row(replay, "Game sound", self.replay_game, "Everything you hear, from your default output.")
        self.replay_mic = Gtk.Switch()
        self._row(replay, "Microphone", self.replay_mic, "Your voice from the default input.")
        self.replay_tracks = Gtk.DropDown.new_from_strings(["One mixed track", "Separate tracks"])
        self._row(replay, "Sound tracks", self.replay_tracks,
                  "One mixed track plays everywhere. Separate tracks keep the game, each app below and your microphone "
                  "apart, so you can mute a podcast or your voice in Clips later. Sound that was mixed while "
                  "recording can't be separated afterwards. Linux only; Windows replays keep one track.")
        self.replay_apps = self._entry("Firefox")
        self._row(replay, "Apps on their own tracks", self.replay_apps,
                  "Up to four, separated by commas, named as gpu-screen-recorder lists them. The game track then "
                  "holds everything except these apps, so muting one later leaves the game intact.")
        playing = _box(8, True)
        playing.append(_button("Show apps playing sound now", self._list_app_audio))
        self.replay_apps_found = _label("", "caption")
        self.replay_apps_found.set_selectable(True)
        playing.append(self.replay_apps_found)
        replay.append(playing)
        self._replay_apps_rows = [self.replay_apps.get_parent(), playing]
        self.replay_tracks.connect("notify::selected", lambda *_: self._replay_tracks_changed())
        self.replay_sound = Gtk.Switch()
        self._row(replay, "Clip sound", self.replay_sound, "A short chirp when a clip is saved; a low tone if saving fails.")
        self.replay_autostart = Gtk.Switch()
        self._row(replay, "Start with Jarvis", self.replay_autostart, "Turn replay on whenever the background service starts.")
        body.append(replay)
        capture = self._card("Capture source", "The recorder uses an existing Wayland capture backend. Start can fail if session permissions or dependencies are missing.")
        self.monitor = Gtk.DropDown.new_from_strings(["No displays discovered"])
        self.monitor.set_hexpand(True)
        self._row(capture, "Display", self.monitor)
        self.color_mode = Gtk.DropDown.new_from_strings(["Automatic", "Preserve HDR (10-bit)", "SDR"])
        self._row(capture, "Color / HDR", self.color_mode,
                  "Applies to recording and replay. Automatic detects the display HDR preset when capture starts. "
                  "Choose Preserve HDR for fullscreen HDR passthrough or older Hyprland versions. "
                  "Restart capture after changing display HDR. HDR uses AV1 when selected, otherwise HEVC, through GPU Screen Recorder.")
        capture.append(_label("HDR clips need an HDR-aware player and display, or a player that tone maps to SDR. "
                              "The built-in preview may not display HDR correctly; use Open in your player. "
                              "SDR capture requires display HDR to be off; Jarvis does not tone map direct monitor capture.", "caption"))
        self.output_dir = self._entry("~/Videos/Murmur")
        self._row(capture, "Save recordings to", self.output_dir)
        self.system_audio = Gtk.Switch()
        self._row(capture, "System audio", self.system_audio, "Record game and application sound")
        self.record_mic = Gtk.Switch()
        self._row(capture, "Microphone audio", self.record_mic, "Include your voice in the saved video")
        self.combined_audio = Gtk.Switch()
        self._row(advanced, "Use a preconfigured combined audio source", self.combined_audio, "Enable only after creating a PipeWire source mixing microphone and system audio. Then select that source below.")
        self.combined_audio.connect("notify::active", lambda control, _pspec: self._combined_audio_changed())
        self.record_device = Gtk.DropDown.new_from_strings(["Default audio source"])
        self.record_device.set_hexpand(True)
        self._row(capture, "Recording audio source", self.record_device, "The backend accepts one source. Use a preconfigured combined source to record both game and microphone audio.")
        self.system_audio.connect("notify::active", lambda control, _pspec: self._record_audio_changed(control))
        self.record_mic.connect("notify::active", lambda control, _pspec: self._record_audio_changed(control))
        self.capture_configured = Gtk.CheckButton(label="I tested capture permission and completed recorder setup")
        capture.append(self.capture_configured)
        capture.append(_label("Verify a short recording in your desktop session before enabling this. Initial capture permissions may require system UI. This setting cannot bypass a portal or compositor permission prompt.", "caption"))
        body.append(capture)
        quality = self._card("Picture & storage", "Applies to recordings and replay. Formats are encoded on the GPU "
                             "of the chosen display; each one is tested there, not assumed. There is no silent CPU fallback.")
        self.legacy_banner = _box(8)
        self.legacy_banner.add_css_class("card")
        self.legacy_text = _label("", "caption")
        self.legacy_banner.append(self.legacy_text)
        self.legacy_banner.append(_button("Use the recommended format", lambda: self.format_choice.set_selected(0)))
        self.legacy_banner.set_visible(False)
        quality.append(self.legacy_banner)
        self.format_choice = Gtk.DropDown.new_from_strings(self.FORMAT_CHOICES)
        self._row(quality, "Video format", self.format_choice,
                  "Recommended: good quality in small files, for everyday clips. "
                  "High quality: the best picture this GPU can record (10-bit, maximum quality); files are larger, most of all in fast-moving scenes. "
                  "Compatible: plays and edits on almost any phone, PC or editor. "
                  "Custom: choose the exact format and quality yourself.")
        self.format_detail = _label("Testing which formats this display's GPU can encode…", "caption")
        quality.append(self.format_detail)
        self._codec_options: list[str] = []
        self._formats: dict = {}
        self.fps = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 60, 1)
        self.fps.set_digits(0)
        self.fps.set_size_request(220, -1)
        self.fps.set_hexpand(True)
        for value in (15, 30, 60):
            self.fps.add_mark(value, Gtk.PositionType.BOTTOM, str(value))
        self._row(quality, "Frames per second", self.fps,
                  "30 is a good starting point for smaller, lighter recordings. 60 makes fast motion smoother and uses more encoding work.")
        self.rate_control = Gtk.DropDown.new_from_strings(["Consistent quality · variable size", "Storage budget · Mbps"])
        self._row(quality, "Size control", self.rate_control)
        self.quality_preset = Gtk.DropDown.new_from_strings(["Compact", "Balanced", "Sharp", "Maximum"])
        self._row(quality, "GPU quality", self.quality_preset)
        self.bitrate = self._spin(1, 100, 1)
        self._row(quality, "Video bitrate (Mbps)", self.bitrate,
                  "Start at 8 Mbps for 1080p; increase for higher resolutions or busy scenes. At a fixed bitrate, more frames share the same storage budget.")
        self.storage_hint = _label("", "caption")
        quality.append(self.storage_hint)
        presets = _box(8, True)
        for title, fps, bitrate in (("Small", 30, 4), ("Balanced", 30, 8), ("Smooth", 60, 16)):
            presets.append(_button(title, lambda fps=fps, bitrate=bitrate: self._recording_preset(fps, bitrate)))
        quality.append(presets)
        quality.append(_label("Presets select the recommended format and a bitrate budget. Existing settings change only when saved.", "caption"))
        self.sound_feedback = Gtk.Switch()
        self._row(quality, "Recording sounds", self.sound_feedback, "Short local tones on start, save and error.")
        body.append(quality)
        legacy = advanced
        self.codec = Gtk.DropDown.new_from_strings(["AV1"])
        self._row(legacy, "Exact format (with Choose a format…)", self.codec,
                  "AV1 10-bit: the best quality for its size, without banding in dark scenes and gradients; "
                  "files are only slightly larger than 8-bit AV1, but older phones, GPUs and some editors can't play them. "
                  "AV1 GPU: smallest files; needs a recent GPU and player. HEVC GPU: smaller than H.264, widely "
                  "supported by hardware, some editors and browsers lack it. H.264 GPU: the most compatible.")
        self.format_tested = _label("", "caption")
        legacy.append(self.format_tested)
        legacy.append(_button("Test formats again", lambda: self._probe_formats(force=True)))
        self.quality = self._spin(0, 51, 1)
        self._row(legacy, "H.264 quality value", self.quality, "Lower values improve quality and increase file size. Your existing value is preserved.")
        body.append(advanced_card)
        self.codec.connect("notify::selected", lambda *_: self._encoding_changed())
        self.format_choice.connect("notify::selected", lambda *_: self._format_changed())
        self.monitor.connect("notify::selected", lambda *_: self._probe_formats())
        self.rate_control.connect("notify::selected", lambda *_: self._encoding_changed())
        self.bitrate.connect("value-changed", lambda *_: self._encoding_changed())
        self.fps.connect("value-changed", lambda *_: self._encoding_changed())
        self.replay_seconds.connect("value-changed", lambda *_: self._encoding_changed())
        body.append(_button("Save recorder settings", self._save_recording, "primary"))
        body.append(_label("Opening the recorder never starts capture. Stop & save requests a graceful finalization; wait for the saved-file confirmation.", "caption"))

    def _clips_page(self) -> None:
        from .clips_ui import ClipsPage
        body = self._page("clips", "Clips", "Watch, trim and join your replays and recordings. "
                          "Edits are saved as new files; originals never change.")
        self.clips_page = ClipsPage(self, body, self._card, _label, _button, _box, self._notice)
        self.stack.connect("notify::visible-child-name", lambda *_: self._clips_page_shown())

    def _clips_page_shown(self) -> None:
        if self.stack.get_visible_child_name() == "clips":
            self._refresh_clips()
        else:
            self.clips_page.close()   # leaving the page releases the video and its pipeline

    def _refresh_clips(self) -> None:
        folder = Path(str(self.config.get("recording", {}).get("output_dir", "~/Videos/Murmur"))).expanduser()
        self._work(lambda: clips(folder), lambda found: self.clips_page.render(folder, found if isinstance(found, list) else []))

    def _voice_page(self) -> None:
        body = self._page("voice", "Voice", "Wake word, language, dictation and the microphone. Everything is recognized on this computer.")
        self.voice_grammar_notice = _label("", "caption")
        self.voice_grammar_notice.set_visible(False)
        body.append(self.voice_grammar_notice)
        activation = self._card("Activation", "Hands-free mode keeps the microphone and small wake model resident while unmuted. Command recognition runs only during short command sessions. Push-to-talk opens input only for a command.")
        self.activation = Gtk.DropDown.new_from_strings(["Push-to-talk / listen once", "Wake word (continuous microphone)"])
        self._row(activation, "Listen when", self.activation)
        self.wake_phrase = self._entry("hey jarvis")
        self._row(activation, "Primary wake name", self.wake_phrase, "Keep it short and distinct from everyday game audio")
        self.wake_aliases = self._entry("Jarvis, Alfred")
        self._row(activation, "Additional wake names", self.wake_aliases, "Separate names with commas. Every name works; up to eight names including the primary.")
        body.append(activation)
        language = self._card("Language", "Choose what Jarvis understands. Arabic adds a local Arabic model "
                              "(Moonshine Streaming Tiny, about 120 MB) beside the English one; English keeps working.")
        self.language = Gtk.DropDown.new_from_strings(["English", "Arabic and English"])
        self._row(language, "Commands and dictation", self.language,
                  "Arabic commands are a fixed list (see Commands → Arabic) in Saudi/Najdi and formal wording. "
                  "Dictation writes any Arabic speech, but only listed commands ever run.")
        self.arabic_status = _label("", "caption")
        language.append(self.arabic_status)
        arabic_actions = _box(8, True)
        self.arabic_install = _button("Download the Arabic model (about 118 MB)", self._download_arabic, "primary")
        arabic_actions.append(self.arabic_install)
        self.arabic_file = _button("Install from a file…", self._install_arabic)
        arabic_actions.append(self.arabic_file)
        language.append(arabic_actions)
        body.append(language)
        dictation = self._card("Dictation", "Say “Jarvis, listen to me” (or “اسمعني”), press Start below or your "
                               "shortcut. Jarvis types what you say into the text field you are in, and never presses "
                               "Enter. A bar at the top of the screen shows it is listening and has a Stop button.")
        self.dictation_button = _button("Start dictation", self._toggle_dictation, "primary")
        self.dictation_button.set_tooltip_text("Start or stop typing what you say into the focused text field (Ctrl+D)")
        dictation.append(self.dictation_button)
        self.dictation_state = _label("Dictation is off.", "caption")
        self.dictation_state.set_selectable(True)
        dictation.append(self.dictation_state)
        self.dictation_copy = _button("Copy text that was not typed", self._copy_held)
        self.dictation_copy.set_visible(False)
        dictation.append(self.dictation_copy)
        self.dictation_language = Gtk.DropDown.new_from_strings(["The phrase you say", "Always Arabic", "Always English"])
        self._row(dictation, "Dictation language", self.dictation_language,
                  "The phrase you say: اسمعني dictates Arabic, “listen” dictates English (the button dictates Arabic "
                  "while Arabic is on). Arabic writes Najdi, Hijazi and other dialects as spoken; English words inside "
                  "Arabic speech usually come out in Arabic script.")
        self.stop_phrases = self._entry("stop dictation, وقف الكتابة")
        self._row(dictation, "Stop phrases", self.stop_phrases,
                  "Ending a sentence with one of these stops dictation; the phrase itself is not typed. Separate with commas.")
        self.dictation_idle = self._spin(5, 300, 5)
        self._row(dictation, "Stop after quiet (seconds)", self.dictation_idle)
        self.keyboard_fallback = Gtk.Switch()
        self._row(dictation, "Also type into apps without text-field support", self.keyboard_fallback,
                  "Most apps tell Jarvis when a text field is focused and whether it is a password field. Some "
                  "(many terminals, games, Electron apps, X11 apps) don't; with this on, Jarvis types into them like "
                  "a keyboard, but it cannot see whether that field is a password. Known password prompts are skipped.")
        dictation.append(_label("Shortcut: bind a key in Hyprland to “jarvis dictate” (for example "
                                "bind = SUPER, D, exec, jarvis dictate). Jarvis never edits your Hyprland "
                                "configuration. Focus changes stop dictation without typing into the new window.", "caption"))
        body.append(dictation)
        microphone = self._card("Microphone", "The level test opens the microphone only while the test is enabled. It stops when this window closes.")
        self.device = Gtk.DropDown.new_from_strings(["Default microphone"])
        self.device.set_hexpand(True)
        self._row(microphone, "Input device", self.device)
        self.level = Gtk.LevelBar.new_for_interval(0, 1)
        self.level.set_hexpand(True)
        self.level.update_property([Gtk.AccessibleProperty.LABEL], ["Microphone input level"])
        self.level.set_tooltip_text("Microphone input level; updated only by level-test events")
        microphone.append(self.level)
        self.level_text = _label("Input test is off", "caption")
        microphone.append(self.level_text)
        self.test_button = _button("Test microphone", self._toggle_test)
        microphone.append(self.test_button)
        microphone.append(_button("Refresh microphones & displays", self._refresh_devices))
        body.append(microphone)
        recognizer = self._card("Recognizer", "Vosk listens for your wake names. After it, Whisper writes down your command; "
                                "it copes much better with accents. Whisper loads when you say the wake name and unloads a "
                                "minute after your last command. One-time download of about 790 MB. Everything stays on this computer.")
        self.engine_label = _label("Recognizer: not running", "row-title")
        recognizer.append(self.engine_label)
        self.install_model_button = _button("Download voice models (about 790 MB)", self._install_model, "primary")
        recognizer.append(self.install_model_button)
        body.append(recognizer)
        card, tuning = self._advanced("Fine-tuning", "Matching, noise handling and sensitivity. The defaults suit most rooms; change one at a time and test with your own voice.")
        self.accurate = Gtk.Switch()
        self._row(tuning, "Accurate model for commands", self.accurate,
                  "Turn off to save memory; commands then use the small Vosk model only.")
        self.speech_model = Gtk.DropDown.new_from_strings(["Small (most accurate)", "Light (less memory)"])
        self._row(tuning, "Command model", self.speech_model,
                  "Light (default) uses Whisper tiny: about 280 MB at most while you give commands instead of about "
                  "550 MB, and answers a little faster. Choose Small if unusual names are misheard.")
        self.noise_suppression = Gtk.Switch()
        self._row(tuning, "Noise suppression", self.noise_suppression,
                  "Cleans steady noise (fans, hum) from command audio. It can't remove other voices, and in tests "
                  "it slightly lowered accuracy, so it's off by default. Try it if your room is noisy.")
        self.adaptive_gate = Gtk.Switch()
        self._row(tuning, "Ignore background sound", self.adaptive_gate,
                  "Raises the speech threshold automatically when there is steady sound in the room (TV, game, music), "
                  "so only a voice close to the microphone counts.")
        self.closest_match = Gtk.Switch()
        self._row(tuning, "Closest-match guessing", self.closest_match,
                  "When a command isn't heard exactly, run the closest one, but only if it is clearly closer than "
                  "any other command. The status shows what was heard.")
        self.sensitivity = self._spin(0, 1, .05, 2)
        self._row(tuning, "Wake sensitivity", self.sensitivity, "Higher sensitivity detects quieter activations but may trigger more often")
        self.timeout = self._spin(2, 15, 1)
        self._row(tuning, "Maximum command length (seconds)", self.timeout)
        self.confidence = self._spin(0, 1, 0.05, 2)
        self._row(tuning, "Minimum command score", self.confidence, "Grammar posterior score, not a correctness probability. Tune using your own voice and room audio.")
        self.vad_rms = self._spin(0.0001, 0.5, 0.001, 4)
        self._row(tuning, "Speech energy threshold", self.vad_rms, "Higher values reject more quiet noise; test in your usual room")
        body.append(card)
        body.append(_button("Save voice settings", self._save_voice, "primary"))
        body.append(_label("Hyprland can bind a key to “jarvis listen”. The command reference and README include the optional user-level binding; Jarvis never edits your Hyprland configuration.", "caption"))

    def _privacy_page(self) -> None:
        body = self._page("privacy", "Privacy", "When Jarvis may listen, what stays local, and starting with your session.")
        privacy = self._card("Before the first microphone session")
        for title, text in [
            ("Audio stays local", "Voice processing does not require a cloud account, subscription, or API key."),
            ("Choose an activation mode", "Hands-free wake-word mode is the default after consent. It keeps the microphone and small local wake model resident while unmuted. Push-to-talk is an optional alternative for bounded sessions."),
            ("Your microphone is not an archive", "Microphone command audio is discarded after processing. Command audio and transcripts are not saved by default. Screen recordings may include microphone audio only when you enable that recorder option."),
            ("Mute is always close", "Use Mute microphone or Ctrl+M to close the voice input. This control does not mute audio deliberately included in a screen recording; stop the recording to end that capture."),
        ]:
            item = _box(4)
            item.append(_label(title, "row-title"))
            item.append(_label(text, "caption"))
            privacy.append(item)
        self.consent = Gtk.CheckButton(label="I understand when Jarvis opens the microphone")
        privacy.append(self.consent)
        self.onboard_button = _button("Save privacy choice & enable hands-free activation", self._onboard, "primary")
        privacy.append(self.onboard_button)
        body.append(privacy)
        startup = self._card("Availability")
        self.startup = Gtk.Switch()
        self._row(startup, "Start at login", self.startup, "Enable or disable the installed user service. Install the optional service first; see README.")
        startup.append(_button("Save startup preference", self._save_startup))
        body.append(startup)
        body.append(_label("No arbitrary spoken shell commands are supported. Actions are limited to the command reference and validated desktop integrations.", "caption"))

    def _install_actions(self) -> None:
        application = self.get_application()
        for name, callback, accelerators in [
            ("mute", self._toggle_mute, ["<Control>m"]),
            ("listen", self._listen, ["<Control>space"]),
            ("dictate", self._toggle_dictation, ["<Control>d"]),
            ("commands", lambda: self.stack.set_visible_child_name("commands"), ["<Control>k"]),
            ("close", self.close, ["<Control>w"]),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _parameter, cb=callback: cb())
            self.add_action(action)
            application.set_accels_for_action(f"win.{name}", accelerators)

    def _notice(self, text: str, error: bool = False) -> bool:
        if not self.closed:
            self.notice.set_text(text)
            if error:
                self.notice.add_css_class("error")
            else:
                self.notice.remove_css_class("error")
        return False

    def _work(self, function: Callable[[], Any], callback: Callable[[Any], Any]) -> None:
        def run() -> None:
            try:
                value = function()
            except Exception as error:
                value = {"ok": False, "error": str(error)}
            def finish() -> bool:
                if not self.closed:
                    callback(value)
                return False
            GLib.idle_add(finish)
        threading.Thread(target=run, name="murmur-ui-request", daemon=True).start()

    def refresh(self) -> None:
        def fetch() -> dict:
            status = client.request("status")
            if not status.get("ok"):
                return status
            result = {"ok": True, "status": status.get("status", {})}
            for op in ("config", "apps", "commands", "devices", "monitors", "recording_devices"):
                response = client.request(op)
                result[op] = response
            return result
        self._work(fetch, self._loaded)

    def _loaded(self, result: dict) -> None:
        if not result.get("ok"):
            self._disconnected(result.get("error", "The service is unavailable."))
            return
        self.connected = True
        self.connection_card.set_visible(False)
        response = result.get("config", {})
        if response.get("ok"):
            self.config = response.get("config", {})
            self._load_settings()
        self._apply_devices(result.get("devices", {}), result.get("monitors", {}), result.get("recording_devices", {}))
        self._apps = result.get("apps", {}).get("apps", [])
        pairs = {row["app_id"]: row["examples"] for row in result.get("commands", {}).get("applications", [])}
        for app in self._apps:
            app["voice_commands"] = pairs.get(app["id"], [])
        self._grammar_signature = None
        self.app_grammar_notice.set_visible(False)
        self.voice_grammar_notice.set_visible(False)
        self._render_apps()
        self._render_added_apps()
        self._render_aliases()
        self._render_commands([*result.get("commands", {}).get("commands", []), *result.get("commands", {}).get("applications", [])])
        self._render_phrases()
        self._set_status(result)
        self._notice("Connected to the local service. Settings are saved only when you choose Save.")
        self._start_watch()

    def _disconnected(self, error: str) -> None:
        self.connected = False
        self.connection_card.set_visible(True)
        self.voice_badge.set_text("Service offline")
        self._show_state("offline")
        self.voice_message.set_text("Start the service or reconnect to see its state.")
        self.mute_button.set_sensitive(False)
        self.listen_button.set_sensitive(False)
        self.record_start.set_sensitive(False)
        self.record_stop.set_sensitive(False)
        self.test_button.set_sensitive(False)
        self._show_test(False)
        self.record_badge.set_text("Status unavailable")
        self.recorder_status.set_text("Status unavailable")
        self.record_message.set_text("Reconnect to the service to verify recording status.")
        self._notice(error, True)

    def _start_watch(self) -> None:
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._watch_stop = threading.Event()
        def watch() -> None:
            try:
                client.watch(lambda event: GLib.idle_add(self._set_status, event), self._watch_stop)
                if not self._watch_stop.is_set():
                    GLib.idle_add(self._disconnected, "Service connection closed. Use Reconnect to restore status.")
            except Exception as error:
                if not self._watch_stop.is_set():
                    GLib.idle_add(self._disconnected, f"Service connection closed: {error}. Use Reconnect.")
        self._watch_thread = threading.Thread(target=watch, name="murmur-ui-events", daemon=True)
        self._watch_thread.start()

    def _set_status(self, event: dict, validate_grammar: bool = True) -> bool:
        if self.closed:
            return False
        if not event.get("ok", True):
            self._notice(str(event.get("error", "Service error")), True)
            return False
        status = event.get("status", event)
        if not isinstance(status, dict):
            return False
        self.status = status
        state = status.get("state", "paused")
        muted = bool(status.get("muted", True))
        if validate_grammar:
            self._update_speech_support(status)
        titles = {"paused": "Microphone paused", "idle": "Waiting for your wake phrase", "listening": "Listening to your command",
                  "processing": "Working on it", "dictating": "Dictating", "error": "Voice needs attention"}
        self.voice_badge.set_text(titles.get(state, state.capitalize()))
        self._show_state(state)
        self.voice_message.set_text(str(status.get("message", "")) or "Use Listen once to start a bounded command session.")
        self.mute_button.set_label("Unmute microphone" if muted else "Mute microphone")
        self.mute_button.set_sensitive(self.connected)
        onboarded = bool(self.config.get("privacy", {}).get("onboarded"))
        self.onboard_banner.set_visible(not onboarded)
        can_listen = self.connected and onboarded and bool(status.get("voice_available", True))
        self.listen_button.set_sensitive(can_listen and state != "processing")
        self.listen_button.set_label("Cancel command" if state == "listening" else "Listen once")
        self.test_button.set_sensitive(self.connected and onboarded)
        if "mic_test" in status and bool(status["mic_test"]) != self._test_active:
            self._show_test(bool(status["mic_test"]))   # the service ends a test after 15 seconds
        engine = status.get("engine")
        if engine:
            names = {"vosk": "Vosk (accurate model not installed or off)", "pocketsphinx": "PocketSphinx (basic)",
                     "vosk+whisper-small": "Vosk + Whisper small (most accurate)",
                     "vosk+whisper-tiny": "Vosk + Whisper tiny (light)"}
            self.engine_label.set_text("Recognizer: " + names.get(engine, engine))
        if engine in ("vosk+whisper-small", "vosk+whisper-tiny", "vosk+parakeet"):
            self.install_model_button.set_visible(False)
        if self._test_active:
            value = min(1.0, max(0.0, float(status.get("level", 0))))
            self.level.set_value(value)
            gate = status.get("gate")
            gate_text = f" · speech threshold {float(gate):.1%}" if gate else ""
            self.level_text.set_text(f"Input test active · level {value:.0%}{gate_text}")
        recording = status.get("recording", {})
        rec_state = recording.get("state", "idle")
        rec_title = {"recording": "Recording", "stopping": "Finalizing your video…", "error": "Recording needs attention", "idle": "Not recording"}.get(rec_state, rec_state.capitalize())
        self.record_badge.set_text(rec_title)
        self.recorder_status.set_text(rec_title)
        message = str(recording.get("message") or "")
        filename = str(recording.get("file") or "")
        detail = "\n".join(value for value in (message, filename) if value)
        self.record_message.set_text(detail or "Your recorder is ready to configure.")
        self.recorder_file.set_text(detail or "Your saved file will appear here.")
        self.record_start.set_sensitive(self.connected and rec_state in {"idle", "error"})
        self.record_stop.set_sensitive(self.connected and rec_state == "recording")
        dictation = status.get("dictation") or {}
        dictating = bool(dictation.get("active"))
        for button in self._dictation_buttons():
            button.set_label("Stop dictation" if dictating else "Start dictation")
            button.set_sensitive(self.connected and onboarded and (dictating or state in ("idle", "paused")))
        held = str(dictation.get("held") or "")
        typed = int(dictation.get("inserted") or 0)
        detail = dictation.get("message") or ("Dictating · speak now" if dictating else "Dictation is off.")
        if typed:
            detail += f" · {typed} characters typed"
        for label in self._dictation_labels():
            label.set_text(detail + (f"\nNot typed: {held}" if held else ""))
        for button in self._held_buttons():
            button.set_visible(bool(held))
        replay = status.get("replay", {})
        replay_state = replay.get("state", "off")
        self.replay_pill.set_visible(replay_state == "on")
        if replay.get("file") and replay.get("file") != self._last_clip:
            self._last_clip = replay.get("file")
            if self.stack.get_visible_child_name() == "clips":
                self._refresh_clips()
        self.replay_status.remove_css_class("active")
        if replay_state == "on":
            self.replay_status.add_css_class("active")
        self.replay_status.set_text({"on": "Replay on", "stopping": "Stopping replay…",
                                     "error": "Replay needs attention"}.get(replay_state, "Replay off"))
        replay_detail = "\n".join(str(value) for value in (replay.get("message"), replay.get("file")) if value)
        self.replay_detail.set_text(replay_detail or "Start replay to keep the last moments ready to save.")
        self.replay_save.set_sensitive(self.connected and replay_state == "on")
        self.overview_replay.set_text({"on": f"Replay on · last {int(self.config.get('replay', {}).get('seconds', 120))} s kept",
                                       "stopping": "Stopping replay…", "error": "Replay needs attention"}.get(replay_state, "Replay off"))
        self.overview_save.set_sensitive(self.connected and replay_state == "on")
        self.overview_record.set_label("Stop recording" if rec_state == "recording" else "Start recording")
        self.overview_record.set_sensitive(self.connected and rec_state in {"idle", "error", "recording"})
        self.overview_audio.set_text(_audio_summary(self.config, rec_state == "recording", replay_state == "on"))
        message = str(status.get("message") or "")
        if message and message != getattr(self, "_recent", ""):
            self._recent = message
            self.recent_label.set_text(time.strftime("%H:%M  ") + message)
        self.replay_toggle.set_label("Stop replay" if replay_state == "on" else "Start replay")
        self.replay_toggle.set_sensitive(self.connected and replay_state in ("on", "off", "error"))
        return False

    def _update_speech_support(self, status: dict) -> None:
        """Apply the worker's completed dictionary check without app-list polling.

        The service publishes an empty list before the model loads, so only a
        ready command/wake state establishes speech support. Level-only events
        reuse the signature and never rebuild the application widgets.
        """
        if not status.get("voice_available") or status.get("state") not in {"idle", "listening", "processing"}:
            return
        message = str(status.get("message") or "").casefold()
        if "loading" in message or "opening microphone" in message:
            return
        unsupported = status.get("unsupported_phrases")
        if not isinstance(unsupported, list):
            return
        excluded = frozenset(str(phrase).strip().casefold() for phrase in unsupported)
        aliases = self.config.get("aliases", {})
        app_signature = tuple((str(app.get("id", "")), str(app.get("name", "")), tuple(app.get("aliases", []))) for app in self._apps)
        signature = (excluded, app_signature, tuple(sorted(aliases.items())))
        if signature == self._grammar_signature:
            return
        self._grammar_signature = signature
        for app in self._apps:
            app_id = app.get("id", "")
            names = [str(app.get("name", ""))]
            custom_aliases = list(dict.fromkeys([*app.get("aliases", []), *(name for name, target in aliases.items() if target == app_id)]))
            candidates = [*names, *custom_aliases]
            supported = []
            for name in candidates:
                try:
                    phrase = "open " + normalize(name)
                except ValueError:
                    continue
                if phrase != "open " and phrase not in excluded:
                    supported.append(name)
            app["speakable"] = bool(supported)
            app["name_speakable"] = bool(names[0] in supported)
            app["voice_aliases"] = [name for name in custom_aliases if name in supported]
        summary = f"{len(excluded)} phrases are unavailable in the current speech dictionary. Add app aliases using common English words."
        unspeakable = [str(app.get("name", "")) for app in self._apps
                       if not app.get("name_speakable") and not app.get("voice_aliases")]
        if unspeakable:
            summary += f" Example: give “{unspeakable[0]}” an alias that describes it in ordinary words."
        for notice in (self.app_grammar_notice, self.voice_grammar_notice):
            notice.set_text(summary if excluded else "")
            notice.set_visible(bool(excluded))
        self._render_apps()

    def _load_settings(self) -> None:
        activation = self.config.get("activation", {})
        audio = self.config.get("audio", {})
        recorder = self.config.get("recording", {})
        self.activation.set_selected(0 if activation.get("mode") == "push_to_talk" else 1)
        self.wake_phrase.set_text(str(activation.get("wake_phrase", "hey jarvis")))
        self.wake_aliases.set_text(", ".join(activation.get("wake_aliases", [])))
        self.close_words.set_text(", ".join(self.config.get("close_words", ["close"])))
        self.sensitivity.set_value(float(activation.get("sensitivity", .5)))
        self.timeout.set_value(float(activation.get("command_timeout", 8)))
        self.confidence.set_value(float(activation.get("confidence", .35)))
        self.vad_rms.set_value(float(audio.get("vad_rms", .005)))
        recognition = self.config.get("recognition", {})
        self.closest_match.set_active(bool(recognition.get("closest_match", True)))
        self.accurate.set_active(bool(recognition.get("accurate", True)))
        self.speech_model.set_selected(0 if recognition.get("speech_model") == "small" else 1)
        self._show_language()
        dictation = self.config.get("dictation", {})
        self.dictation_language.set_selected({"auto": 0, "ar": 1, "en": 2}.get(dictation.get("language", "auto"), 0))
        self.stop_phrases.set_text(", ".join(dictation.get("stop_phrases", [])))
        self.dictation_idle.set_value(float(dictation.get("idle_seconds", 20)))
        self.keyboard_fallback.set_active(bool(dictation.get("keyboard_fallback", True)))
        self._show_arabic_model()
        self.noise_suppression.set_active(bool(recognition.get("noise_suppression", False)))
        self.adaptive_gate.set_active(bool(self.config.get("audio", {}).get("adaptive_gate", True)))
        self.output_dir.set_text(str(recorder.get("output_dir", "~/Videos/Murmur")))
        modes = ["auto", "hdr", "sdr"]
        self.color_mode.set_selected(modes.index(recorder.get("color_mode", "auto")))
        self.combined_audio.set_active(bool(recorder.get("combined_audio_source", False)))
        self.system_audio.set_active(bool(recorder.get("system_audio", False)))
        self.record_mic.set_active(bool(recorder.get("microphone", False)))
        self.capture_configured.set_active(bool(recorder.get("configured", False)))
        self.sound_feedback.set_active(bool(recorder.get("sound_feedback", False)))
        replay = self.config.get("replay", {})
        self.replay_seconds.set_value(float(replay.get("seconds", 120)))
        self.replay_storage.set_selected(1 if replay.get("storage", "ram") == "disk" else 0)
        self.replay_max_mb.set_value(float(replay.get("max_mb", 256)))
        self.replay_buffer_dir.set_text(replay.get("buffer_dir", ""))
        self.replay_autostart.set_active(bool(replay.get("autostart", False)))
        self.replay_game.set_active(bool(replay.get("game_audio", True)))
        self.replay_mic.set_active(bool(replay.get("microphone", True)))
        self.replay_tracks.set_selected(1 if replay.get("audio_tracks") == "separate" else 0)
        self.replay_apps.set_text(", ".join(replay.get("separate_apps", [])))
        self._replay_tracks_changed()
        self.replay_sound.set_active(bool(replay.get("sound", True)))
        self._populate_record_devices()
        self.fps.set_value(float(recorder.get("fps", 30)))
        self.quality.set_value(float(recorder.get("quality", 23)))
        self.rate_control.set_selected(1 if recorder.get("rate_control") == "bitrate" else 0)
        self.bitrate.set_value(float(recorder.get("bitrate_mbps", 8)))
        self.quality_preset.set_selected(QUALITY_PRESETS.index(recorder.get("quality_preset", "very_high")))
        self._show_codec(recorder.get("codec", "libx264"))  # after quality: High quality needs both
        self._encoding_changed()
        self.startup.set_active(bool(self.config.get("startup", False)))
        onboarded = bool(self.config.get("privacy", {}).get("onboarded", False))
        self.consent.set_active(onboarded)
        self.onboard_button.set_label("Privacy setup complete" if onboarded else "Save privacy choice & enable hands-free activation")
        self.onboard_button.set_sensitive(not onboarded)
        from .models import describe
        models = describe(self.config)
        self.install_model_button.set_visible(not (models["model_ready"] and models["accurate_model_ready"]))

    def _apply_devices(self, devices: dict, monitors: dict, recording_devices: dict | None = None) -> None:
        if recording_devices is not None:
            self._recording_devices = recording_devices.get("devices", []) if recording_devices.get("ok") else []
        self._populate_record_devices()
        self._devices = devices.get("devices", []) if devices.get("ok") else []
        if not any(not str(device.get("id", "")) for device in self._devices):
            self._devices.insert(0, {"id": "", "name": "Default microphone"})
        selected = self.config.get("audio", {}).get("device", "")
        if selected and not any(device.get("id") == selected for device in self._devices):
            self._devices.append({"id": selected, "name": selected + " (saved input unavailable)"})
        self.device.set_model(Gtk.StringList.new([str(device.get("name", device.get("id", "Unknown input"))) for device in self._devices]))
        self.device.set_selected(next((i for i, item in enumerate(self._devices) if item.get("id") == selected), 0))
        self._monitors = monitors.get("monitors", []) if monitors.get("ok") else []
        selected_monitor = self.config.get("recording", {}).get("monitor", "")
        if selected_monitor and not any(item.get("name") == selected_monitor for item in self._monitors):
            self._monitors.append({"name": selected_monitor, "width": "?", "height": "?"})
        labels = [f"{item['name']} · {item.get('width', '?')} × {item.get('height', '?')}"
                  + (" · HDR" if item.get("colorManagementPreset") in ("hdr", "hdredid") else "")
                  for item in self._monitors]
        self.monitor.set_model(Gtk.StringList.new(labels or ["No displays discovered — reconnect in Hyprland"]))
        self.monitor.set_selected(next((i for i, item in enumerate(self._monitors) if item.get("name") == selected_monitor), 0))
        self._probe_formats()

    def _record_audio_changed(self, control: Any) -> None:
        if control.get_active() and not self.combined_audio.get_active():
            other = self.record_mic if control is self.system_audio else self.system_audio
            other.set_active(False)
        self._populate_record_devices()

    def _combined_audio_changed(self) -> None:
        if not self.combined_audio.get_active() and self.system_audio.get_active() and self.record_mic.get_active():
            self.record_mic.set_active(False)
        self._populate_record_devices()

    def _populate_record_devices(self) -> None:
        if not hasattr(self, "record_device"):
            return
        is_monitor = self.system_audio.get_active()
        self._shown_recording_devices = [{"id": "", "name": "Choose a recording audio source"}]
        self._shown_recording_devices.extend(source for source in self._recording_devices if self.combined_audio.get_active() or bool(source.get("monitor")) == is_monitor)
        selected = self.config.get("recording", {}).get("audio_device", "")
        if selected and not any(source.get("id") == selected for source in self._shown_recording_devices):
            self._shown_recording_devices.append({"id": selected, "name": selected + " (saved source)"})
        self.record_device.set_model(Gtk.StringList.new([str(source.get("name", source.get("id", "Unknown source"))) for source in self._shown_recording_devices]))
        self.record_device.set_selected(next((i for i, source in enumerate(self._shown_recording_devices) if source.get("id") == selected), 0))

    def _refresh_devices(self) -> None:
        self._work(lambda: (client.request("devices"), client.request("monitors"), client.request("recording_devices")), lambda value: self._apply_devices(*value) if isinstance(value, tuple) else self._notice(str(value.get("error")), True))

    def _clear(self, container: Any) -> None:
        while child := container.get_first_child():
            container.remove(child)

    def _render_commands(self, commands: list) -> None:
        self._command_data = commands
        self._command_page = 0
        self._render_command_page()

    def _filter_commands(self, _entry: Any = None) -> None:
        self._command_page = 0
        self._render_command_page()

    def _change_command_page(self, delta: int) -> None:
        self._command_page += delta
        self._render_command_page()

    def _render_command_page(self) -> None:
        self._clear(self.command_list)
        self._command_rows = []
        needle = self.command_search.get_text().casefold().strip()
        matches = [command for command in self._command_data if not needle or needle in " ".join([
            str(command.get("title", "")), str(command.get("category", "")),
            str(command.get("app_id", "")), *map(str, command.get("examples", []))]).casefold()]
        self._command_page = max(0, min(self._command_page, max(0, (len(matches) - 1) // 30)))
        start = self._command_page * 30
        for command in matches[start:start + 30]:
            card = self._card(str(command.get("title", "Command")), str(command.get("category", "Desktop")))
            for example in command.get("examples", []):
                card.append(_label(f"“{example}”", "example"))
            self.command_list.append(card)
        self.command_count.set_text(f"{start + 1 if matches else 0}–{min(start + 30, len(matches))} of {len(matches)} entries")
        self.command_previous.set_sensitive(self._command_page > 0)
        self.command_next.set_sensitive(start + 30 < len(matches))
        if not matches:
            self.command_list.append(_label("No matching commands." if self._command_data else "Connect to load commands for every installed app.", "caption"))

    def _render_apps(self) -> None:
        self._clear(self.app_list)
        needle = self.app_search.get_text().casefold().strip()
        matching = [app for app in self._apps if needle in f"{app.get('name', '')} {app.get('id', '')}".casefold()]
        # The same app installed twice (native + Flatpak) shows once: the entry
        # voice commands open. An alias can still pick the other copy.
        visible = [app for app in matching if not app.get("duplicate_of")]
        hidden = len(matching) - len(visible)
        summary = f"{len(visible)} apps" + (f" · {hidden} duplicate entr{'y' if hidden == 1 else 'ies'} hidden "
                                            "(for example a Flatpak copy; Jarvis opens the native one)" if hidden else "")
        self.app_list.append(_label(summary if self._apps else "Connect to discover installed applications.", "caption"))
        # A widget for every system desktop entry is unnecessary until searched.
        for app in visible[:30]:
            row = _box(4)
            row.add_css_class("app-row")
            row.append(_label(str(app.get("name", app.get("id", "Application"))), "row-title"))
            row.append(_label(str(app.get("id", "")), "caption"))
            if app.get("name_speakable") is False or app.get("speakable") is False:
                aliases = app.get("voice_aliases", [])
                detail = f"The direct name is unavailable. Say “Open {aliases[0]}” using your alias." if aliases else "This name is outside the speech dictionary. Add an alias using common English words."
                row.append(_label(detail, "caption"))
            for phrase in app.get("voice_commands", []):
                row.append(_label(f"“{phrase}”", "example"))
            row.append(self._voice_name_editor(app))
            self.app_list.append(row)
        if len(visible) > 30:
            self.app_list.append(_label("Showing the first 30 matches. Search to narrow the list.", "caption"))
        # Rebuild the alias picker only when the app list changes, so searching
        # the list above does not reset a selection the user already made.
        ids = tuple(app["id"] for app in self._apps)
        if ids != self._alias_model_ids:
            self._alias_model_ids = ids
            self.alias_app.set_model(Gtk.StringList.new([f"{app.get('name', app['id'])} ({app['id']})" for app in self._apps] or ["No applications discovered"]))

    def _render_added_apps(self) -> None:
        from . import app_catalog
        self._clear(self.added_apps)
        self._clear(self.appimage_hints)
        for item in app_catalog.added():
            row = _box(8, True)
            row.append(_label(f"{item['name']} · added by Jarvis", "caption"))
            row.append(_button("Remove", lambda identity=item["id"]: self._remove_added_app(identity)))
            self.added_apps.append(row)
        known = " ".join(str(app.get("exec", "")) for app in self._apps)
        for path in app_catalog.program_candidates(known):
            row = _box(8, True)
            row.append(_label(f"Found {path.name} in {path.parent}", "caption"))
            row.append(_button("Use this file", lambda path=path: self._use_app_file(str(path))))
            self.appimage_hints.append(row)

    def _choose_app_file(self) -> None:
        dialog = Gtk.FileDialog(title="Choose a program or AppImage")
        def chosen(source, result) -> None:
            try:
                self._use_app_file(source.open_finish(result).get_path())
            except GLib.Error:
                pass
        dialog.open(self, None, chosen)

    def _use_app_file(self, path: str) -> None:
        self.add_app_path = path
        self.add_app_file.set_text(path)
        if not self.add_app_name.get_text().strip():
            stem = Path(path).stem.replace("_", " ").replace("-", " ")
            self.add_app_name.set_text(stem.split(".AppImage")[0].split(" x86")[0].strip())

    def _add_app(self) -> None:
        from . import app_catalog
        try:
            app_catalog.add(self.add_app_path, self.add_app_name.get_text())
        except (ValueError, OSError) as error:
            self._notice(str(error), True)
            return
        self._notice(f"Added {self.add_app_name.get_text().strip()}. Say “open {self.add_app_name.get_text().strip()}”.")
        self.add_app_path = ""
        self.add_app_file.set_text("No file chosen")
        self.add_app_name.set_text("")
        self.refresh()   # the service rescans launcher entries

    def _remove_added_app(self, identity: str) -> None:
        from . import app_catalog
        try:
            app_catalog.remove(identity)
        except (ValueError, OSError) as error:
            self._notice(str(error), True)
            return
        self._notice("Removed the app Jarvis added. The program file itself was not deleted.")
        self.refresh()

    def _voice_name_editor(self, app: dict) -> Any:
        """Per-app "Add voice name": click, type a name, press Enter. Done."""
        app_id = str(app.get("id", ""))
        box = _box(8)
        names = sorted(name for name, target in self.config.get("aliases", {}).items() if target == app_id)
        if names:
            label = _label("Voice names: " + ", ".join(f"“{n}”" for n in names), "caption")
            label.set_hexpand(True)
            box.append(label)
        entry = self._entry("Voice name, e.g. " + str(app.get("name", "my app")).lower())
        entry.update_property([Gtk.AccessibleProperty.LABEL], [f"Voice name for {app.get('name', app_id)}"])
        prefix = self._entry("Launch word; empty = app name only")
        prefix.set_text("open")
        prefix.update_property([Gtk.AccessibleProperty.LABEL], ["Launch word; clear for app name only"])
        save = _button("Save", lambda: self._save_voice_name(app_id, entry, prefix), "primary")
        entry.connect("activate", lambda _entry: self._save_voice_name(app_id, entry, prefix))
        entry.set_visible(False)
        prefix.set_visible(False)
        launch_hint = _label("Launch word: change open, or clear it to say only the app name after a wake name.", "caption")
        launch_hint.set_visible(False)
        save.set_visible(False)
        def reveal() -> None:
            add.set_visible(False)
            entry.set_visible(True)
            prefix.set_visible(True)
            launch_hint.set_visible(True)
            save.set_visible(True)
            entry.grab_focus()
        add = _button("Add voice name", reveal)
        for widget in (add, entry, launch_hint, prefix, save):
            box.append(widget)
        return box

    def _save_voice_name(self, app_id: str, entry: Any, prefix: Any) -> None:
        phrase = " ".join(entry.get_text().lower().split())
        if not phrase:
            self._notice("Type the name you want to say for this app.", True)
            return
        wake = str(self.config.get("activation", {}).get("wake_phrase", "jarvis")).capitalize()
        launch = normalize(prefix.get_text())
        wording = " ".join(word for word in (launch, phrase) if word)
        self._save({"aliases": {phrase: app_id}, "app_prefixes": {phrase: launch}}, f"Saved. Say “{wake}, {wording}”.")

    def _save_close_words(self) -> None:
        words = [normalize(word) for word in self.close_words.get_text().split(",") if word.strip()]
        if not words:
            self._notice("Enter at least one word, for example close.", True)
            return
        wake = str(self.config.get("activation", {}).get("wake_phrase", "jarvis")).capitalize()
        self._save({"close_words": words}, f"Close words saved. Say “{wake}, {words[0]} Firefox”.", replace_keys=("close_words",))

    def _render_aliases(self) -> None:
        self._clear(self.alias_list)
        for name, app_id in sorted(self.config.get("aliases", {}).items()):
            row = _box(8, True)
            label = _label(f"“{name}” → {app_id}")
            label.set_hexpand(True)
            row.append(label)
            prefix = self._entry("Empty = app name only")
            prefix.set_text(self.config.get("app_prefixes", {}).get(name, "open"))
            prefix.set_width_chars(12)
            prefix.set_hexpand(False)
            prefix.update_property([Gtk.AccessibleProperty.LABEL], [f"Launch word for {name}; clear for name only"])
            row.append(prefix)
            row.append(_button("Save word", lambda phrase=name, control=prefix: self._save({"app_prefixes": {phrase: normalize(control.get_text())}}, "Launch word saved. A blank word lets you say the app name after your wake name.")))
            row.append(_button("Remove", lambda phrase=name: self._remove_mapping("aliases", phrase)))
            self.alias_list.append(row)
        if not self.config.get("aliases"):
            self.alias_list.append(_label("No custom application aliases yet.", "caption"))

    def _render_phrases(self) -> None:
        self._clear(self.phrase_list)
        for phrase, canonical in sorted(self.config.get("commands", {}).items()):
            row = _box(8, True)
            text = _label(f"“{phrase}” → “{canonical}”")
            text.set_hexpand(True)
            row.append(text)
            row.append(_button("Remove", lambda item=phrase: self._remove_mapping("commands", item)))
            self.phrase_list.append(row)
        if not self.config.get("commands"):
            self.phrase_list.append(_label("No custom command shortcuts yet.", "caption"))

    def _save(self, patch: dict, success: str = "Settings saved.", replace_keys: tuple = ()) -> None:
        if not self.connected:
            self._notice("Start or reconnect to the service before saving settings.", True)
            return
        self._notice("Saving settings…")
        def save() -> dict:
            with self._save_lock:
                current = client.request("config")
                if not current.get("ok"):
                    return current
                merged = _merge(current.get("config", {}), patch)
                for key in replace_keys:
                    merged[key] = copy.deepcopy(patch[key])
                result = client.request("save_config", config=merged)
                if result.get("ok"):
                    result["config"] = merged
                return result
        def saved(result: dict) -> None:
            if not result.get("ok"):
                self._notice(str(result.get("error", "Settings could not be saved.")), True)
                return
            self.config = result["config"]
            self._show_language()
            self._render_aliases()
            self._render_phrases()
            self._render_apps()
            if patch.get("privacy", {}).get("onboarded"):
                self.onboard_button.set_label("Privacy setup complete")
                self.onboard_button.set_sensitive(False)
                self.activation.set_selected(1)
            self._set_status({"ok": True, "status": self.status}, validate_grammar=False)
            self._notice(success)
        self._work(save, saved)

    def _save_voice(self) -> None:
        selected = self.device.get_selected()
        device = self._devices[selected].get("id", "") if selected < len(self._devices) else ""
        self._save({"recognition": {"closest_match": self.closest_match.get_active(),
                                    "accurate": self.accurate.get_active(),
                                    "speech_model": "light" if self.speech_model.get_selected() == 1 else "small",
                                    "noise_suppression": self.noise_suppression.get_active(),
                                    "language": "ar_en" if self.language.get_selected() == 1 else "en"},
                    "dictation": {"language": ("auto", "ar", "en")[self.dictation_language.get_selected()],
                                  "stop_phrases": [p.strip() for p in self.stop_phrases.get_text().split(",") if p.strip()],
                                  "idle_seconds": self.dictation_idle.get_value_as_int(),
                                  "keyboard_fallback": self.keyboard_fallback.get_active()},
                    "audio": {"device": device, "vad_rms": self.vad_rms.get_value(),
                              "adaptive_gate": self.adaptive_gate.get_active()}, "activation": {
            "mode": "wake_word" if self.activation.get_selected() == 1 else "push_to_talk",
            "wake_phrase": self.wake_phrase.get_text().strip().lower(),
            "wake_aliases": [name.strip() for name in self.wake_aliases.get_text().split(",") if name.strip()], "sensitivity": self.sensitivity.get_value(),
            "command_timeout": self.timeout.get_value_as_int(), "confidence": self.confidence.get_value(),
        }}, "Voice settings saved. Use Listen once, or unmute to enable the selected activation mode.")

    def _show_language(self) -> None:
        arabic = self.config.get("recognition", {}).get("language") == "ar_en"
        self.language.set_selected(1 if arabic else 0)
        self.overview_language.set_label("Turn off Arabic" if arabic else "Turn on Arabic")

    def _toggle_arabic(self) -> None:
        """One click between English only and Arabic and English (also: jarvis language)."""
        from .models import arabic_model_ready
        if self.config.get("recognition", {}).get("language") == "ar_en":
            self._save({"recognition": {"language": "en"}}, "Arabic is off. Jarvis understands English only.")
        elif not arabic_model_ready():
            self._notice("The Arabic model is not installed. Download it in Voice → Language first.", True)
        else:
            self._save({"recognition": {"language": "ar_en"}},
                       "Arabic is on. Say commands in Arabic or English; dictation types Arabic.")

    def _show_arabic_model(self) -> None:
        from .models import arabic_model_dir, arabic_model_ready
        ready = arabic_model_ready()
        self.arabic_status.set_text(f"Arabic model installed at {arabic_model_dir()}." if ready else
                                    "The Arabic model is not installed. It is needed for Arabic and English.")
        self.arabic_install.set_visible(not ready)
        self.arabic_file.set_visible(not ready)

    def _download_arabic(self) -> None:
        self.arabic_install.set_sensitive(False)
        self._notice("Downloading the Arabic model (about 118 MB) and checking its checksum…")
        def install() -> dict:
            from .models import install_arabic
            return {"ok": True, "message": f"Arabic model installed at {install_arabic()}. "
                                           "Choose Arabic and English, then Save."}
        def done(result: dict) -> None:
            self.arabic_install.set_sensitive(True)
            self._show_arabic_model()
            self._notice(str(result.get("message") or result.get("error")), not result.get("ok"))
        self._work(install, done)

    def _install_arabic(self) -> None:
        dialog = Gtk.FileDialog(title="Choose moonshine-streaming-tiny-ar.tar.bz2")
        def chosen(source, result) -> None:
            try:
                path = Path(source.open_finish(result).get_path())
            except GLib.Error:
                return
            self._notice("Checking and installing the Arabic model…")
            def install() -> dict:
                from .models import install_arabic
                return {"ok": True, "message": f"Arabic model installed at {install_arabic(path)}. "
                                               "Choose Arabic and English, then Save."}
            def done(result: dict) -> None:
                self._show_arabic_model()
                self._notice(str(result.get("message") or result.get("error")), not result.get("ok"))
            self._work(install, done)
        dialog.open(self, None, chosen)

    def _toggle_dictation(self) -> None:
        if not self.config.get("privacy", {}).get("onboarded"):
            self.stack.set_visible_child_name("privacy")
            self._notice("Complete the privacy setup before opening the microphone.", True)
            return
        self._action("dictation", action="toggle")

    def _copy_held(self) -> None:
        held = str(self.status.get("dictation", {}).get("held", ""))
        if held:
            self.get_clipboard().set(held)
            self._action("dictation", action="copied")
            self._notice("Copied the text that was not typed.")

    def _install_model(self) -> None:
        self.install_model_button.set_sensitive(False)
        self._notice("Downloading the voice models (about 790 MB) and checking their checksums… This can take a few minutes.")
        def install() -> dict:
            from .models import install as install_model, install_accurate, install_denoiser
            install_model()
            install_denoiser()
            path = install_accurate()
            install_accurate("whisper-tiny")
            # Restart the listener so it picks up the new model (only if it is on).
            if not self.status.get("muted", True):
                client.request("mute", muted=False)
            return {"ok": True, "message": f"Voice model installed at {path}."}
        def done(result: dict) -> None:
            self.install_model_button.set_sensitive(True)
            self.install_model_button.set_visible(not result.get("ok"))
            self._notice(str(result.get("message") or result.get("error")), not result.get("ok"))
        self._work(install, done)

    def _save_recording(self) -> None:
        selected = self.monitor.get_selected()
        monitor = self._monitors[selected]["name"] if selected < len(self._monitors) else ""
        source_index = self.record_device.get_selected()
        source = self._shown_recording_devices[source_index]["id"] if source_index < len(self._shown_recording_devices) else ""
        if (self.system_audio.get_active() or self.record_mic.get_active()) and not source:
            self._notice("Choose an explicit recording audio source, or turn audio capture off.", True)
            return
        codec = self._chosen_codec()
        if codec in GPU_CODECS and self._formats.get("formats") and not self._formats["formats"].get(codec):
            self._notice(f"{self.NAMES[codec]} did not pass the encoder test on {self._formats.get('gpu')} (the GPU of "
                         f"{monitor or 'this display'}). Choose another format.", True)
            return
        self._save({"recording": {"output_dir": self.output_dir.get_text().strip(), "monitor": monitor,
            "system_audio": self.system_audio.get_active(), "microphone": self.record_mic.get_active(),
            "configured": self.capture_configured.get_active(), "sound_feedback": self.sound_feedback.get_active(),
            "combined_audio_source": self.combined_audio.get_active(),
            "color_mode": ["auto", "hdr", "sdr"][self.color_mode.get_selected()],
            "audio_device": source, "fps": int(round(self.fps.get_value())),
            "rate_control": "bitrate" if self.rate_control.get_selected() == 1 else "quality",
            "bitrate_mbps": self.bitrate.get_value_as_int(),
            "quality_preset": QUALITY_PRESETS[self.quality_preset.get_selected()],
            "codec": codec, "quality": self.quality.get_value_as_int()},
            "replay": {"seconds": self.replay_seconds.get_value_as_int(), "autostart": self.replay_autostart.get_active(),
                       "game_audio": self.replay_game.get_active(), "microphone": self.replay_mic.get_active(),
                       "sound": self.replay_sound.get_active(),
                       "storage": "disk" if self.replay_storage.get_selected() == 1 else "ram",
                       "max_mb": self.replay_max_mb.get_value_as_int(), "buffer_dir": self.replay_buffer_dir.get_text().strip(),
                       "audio_tracks": "separate" if self.replay_tracks.get_selected() == 1 else "mixed",
                       "separate_apps": [a.strip() for a in self.replay_apps.get_text().split(",") if a.strip()]}},
            "Recorder settings saved. Start recording explicitly, or say “Start recording”.")

    def _replay_tracks_changed(self) -> None:
        for row in self._replay_apps_rows:
            row.set_visible(self.replay_tracks.get_selected() == 1)

    def _list_app_audio(self) -> None:
        def listing() -> dict:
            result = subprocess.run(["gpu-screen-recorder", "--list-application-audio"], capture_output=True, text=True, timeout=10)
            return {"ok": result.returncode == 0, "apps": sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})}
        def done(result: dict) -> None:
            apps = result.get("apps") or []
            self.replay_apps_found.set_text(("Playing now: " + ", ".join(apps)) if apps else
                                            "No app is playing sound right now (or gpu-screen-recorder is not installed). "
                                            "Start the podcast or video, then try again.")
        self._work(listing, done)

    def _replay_storage_changed(self) -> None:
        # Disk needs room for gpu-screen-recorder's 256 MiB files. Raise, never
        # lower, so a larger saved budget is kept.
        if self.replay_storage.get_selected() == 1:
            self.replay_max_mb.set_value(max(512, self.replay_max_mb.get_value()))

    LEGACY = {"libx264": "H.264 · CPU (legacy, uses far more CPU)", "h264_vaapi": "H.264 · VA-API via wf-recorder (legacy)"}
    LABELS = {"av1_10bit": "AV1 10-bit · GPU (best quality)", "av1": "AV1 · GPU",
              "hevc_10bit": "HEVC 10-bit · GPU", "hevc": "HEVC · GPU", "h264": "H.264 · GPU"}
    NAMES = {"av1_10bit": "AV1 10-bit", "av1": "AV1", "hevc_10bit": "HEVC 10-bit", "hevc": "HEVC", "h264": "H.264"}

    FORMAT_CHOICES = ["Recommended · small files", "High quality · large files",
                      "Compatible · plays everywhere", "Custom…"]
    RECOMMENDED, HIGH, COMPATIBLE, CUSTOM = range(4)

    def _high_codec(self) -> str:
        """The 10-bit format High quality uses: AV1, or HEVC on GPUs without AV1."""
        formats = self._formats.get("formats", {})
        return next((c for c in ("av1_10bit", "hevc_10bit") if formats.get(c)), "av1_10bit")

    def _show_codec(self, codec: str) -> None:
        """Put a saved codec into the picker. Legacy encoders stay selectable only
        for configurations that already use them; nothing changes until Save."""
        self._saved_codec = codec
        self._codec_options = list(self.LABELS) + ([codec] if codec in self.LEGACY else [])
        self.codec.set_model(Gtk.StringList.new([self.LABELS.get(c) or self.LEGACY[c] for c in self._codec_options]))
        self.codec.set_selected(self._codec_options.index(codec) if codec in self._codec_options else 0)
        from .encoder_probe import recommended
        best = recommended(self._formats.get("formats", {})) or "av1"
        high = (codec == self._high_codec() and self.rate_control.get_selected() == 0
                and QUALITY_PRESETS[self.quality_preset.get_selected()] == "ultra")
        self._showing = True  # reflect saved settings without changing them
        self.format_choice.set_selected(self.HIGH if high else self.RECOMMENDED if codec == best
                                        else self.COMPATIBLE if codec == "h264" else self.CUSTOM)
        self._showing = False
        self.legacy_banner.set_visible(codec in self.LEGACY)
        self.legacy_text.set_text(f"This recorder uses {self.LEGACY.get(codec, '')}. It keeps working, but the GPU "
                                  "formats record games with a small fraction of the CPU. Nothing changes until you Save.")

    def _chosen_codec(self) -> str:
        from .encoder_probe import recommended
        choice = self.format_choice.get_selected()
        if choice == self.RECOMMENDED:
            return recommended(self._formats.get("formats", {})) or "av1"
        if choice == self.HIGH:
            return self._high_codec()
        if choice == self.COMPATIBLE:
            return "h264"
        index = self.codec.get_selected()
        return self._codec_options[index] if index < len(self._codec_options) else "av1"

    def _probe_formats(self, force: bool = False) -> None:
        selected = self.monitor.get_selected()
        monitor = self._monitors[selected]["name"] if selected < len(self._monitors) else ""
        def probe() -> dict:
            from . import encoder_probe
            if force:
                encoder_probe.probe_node.cache_clear()
            return encoder_probe.formats_for_monitor(monitor)
        def done(result: dict) -> None:
            if isinstance(result, dict) and "formats" in result:
                self._formats = result
                self._show_codec(getattr(self, "_saved_codec", "libx264")) if not force else None
            self._encoding_changed()
        self._work(probe, done)

    def _format_changed(self) -> None:
        """High quality means constant quality at Maximum; leaving it for
        Recommended or Compatible returns to the default Sharp."""
        ultra = QUALITY_PRESETS.index("ultra")
        choice = self.format_choice.get_selected()
        if getattr(self, "_showing", False):
            pass
        elif choice == self.HIGH:
            self.rate_control.set_selected(0)
            self.quality_preset.set_selected(ultra)
        elif choice in (self.RECOMMENDED, self.COMPATIBLE) and self.quality_preset.get_selected() == ultra:
            self.quality_preset.set_selected(QUALITY_PRESETS.index("very_high"))
        self._encoding_changed()

    def _encoding_changed(self) -> None:
        from .encoder_probe import recommended
        formats = self._formats.get("formats", {})
        codec = self._chosen_codec()
        choice = self.format_choice.get_selected()
        self.codec.set_sensitive(choice == self.CUSTOM)
        names = self.NAMES
        if formats:
            best = recommended(formats)
            works = ", ".join(names[c] for c in ("av1", "hevc", "h264") if formats.get(c)) or "none"
            ten = ", ".join(n for c, n in (("av1_10bit", "AV1"), ("hevc_10bit", "HEVC")) if formats.get(c)) or "none"
            self.format_tested.set_text(f"Tested on {self._formats.get('gpu')}: {works} · 10-bit (SDR and HDR): {ten}.")
            usable = codec not in names or formats.get(codec)
            detail = (f"Recommended here: {names.get(best, 'none')} on {self._formats.get('gpu')}." if best else
                      "No GPU format passed the test on this display's GPU.")
            if codec in self.LEGACY:
                detail = f"Selected: {self.LEGACY[codec]}."
            elif not usable:
                detail = f"{names[codec]} did not pass the encoder test on this display's GPU; choose another format."
            elif choice == self.RECOMMENDED:
                detail = f"{names[codec]}: good quality in small files. Fine for most clips and sharing."
            elif choice == self.HIGH:
                detail = (f"{names[codec]} at Maximum quality: the best picture this GPU records, with no visible loss "
                          "and no banding. Files are larger, most of all in fast-moving scenes, and a replay keeps fewer seconds in its memory budget.")
            elif choice == self.COMPATIBLE:
                detail = f"{names[codec]}: plays and edits on almost any phone, PC or editor; larger files than AV1."
            else:
                detail = f"Selected: {names[codec]} on the GPU. " + detail
            self.format_detail.set_text(detail)
        elif shutil.which("ffmpeg") is None:
            self.format_detail.set_text("FFmpeg is missing, so formats can't be tested. Install ffmpeg.")
        budget = self.rate_control.get_selected() == 1
        gpu = codec in GPU_CODECS
        self.bitrate.set_sensitive(budget)
        self.quality_preset.set_sensitive(not budget and gpu)
        self.quality.set_sensitive(not budget and not gpu)
        if budget:
            cfg = {"bitrate_mbps": self.bitrate.get_value_as_int()}
            self.storage_hint.set_text(f"About {storage_estimate(cfg):.0f} MB/min video; "
                f"{storage_estimate(cfg, 3600) / 1000:.2f} GB/hour. "
                f"Replay video buffer: ~{storage_estimate(cfg, self.replay_seconds.get_value_as_int()):.0f} MB. "
                "Audio, container and encoder overhead are extra; these are estimates, not a hard size or RAM limit.")
        else:
            self.storage_hint.set_text("File size and replay RAM vary with scene complexity. Choose Storage budget for a predictable video bitrate.")

    def _recording_preset(self, fps=30, bitrate=8) -> None:
        self.fps.set_value(fps)
        self.format_choice.set_selected(0)
        self.rate_control.set_selected(1)
        self.bitrate.set_value(bitrate)
        self._notice(f"Selected the recommended format, {fps} fps, {bitrate} Mbps. Save to apply.")

    def _save_startup(self) -> None:
        self._action("set_startup", enabled=self.startup.get_active())

    def _onboard(self) -> None:
        if not self.consent.get_active():
            self._notice("Select the microphone acknowledgement before completing setup.", True)
            return
        self._save({"privacy": {"onboarded": True}, "activation": {"mode": "wake_word"}},
                   "Privacy setup complete. Hands-free activation is enabled; the status shows whether the wake recognizer is ready.")
        # The saved callback updates the privacy card after the daemon accepts it.

    def _add_alias(self) -> None:
        phrase = " ".join(self.alias_name.get_text().lower().split())
        index = self.alias_app.get_selected()
        if not phrase or index >= len(self._apps):
            self._notice("Enter an alias and select an installed application.", True)
            return
        self._save({"aliases": {phrase: self._apps[index]["id"]}, "app_prefixes": {phrase: normalize(self.alias_prefix.get_text())}}, "Application alias and launch word saved.")

    def _add_phrase(self) -> None:
        phrase = " ".join(self.new_phrase.get_text().lower().split())
        canonical = " ".join(self.canonical_phrase.get_text().lower().split())
        if not phrase or not canonical:
            self._notice("Enter both your phrase and its supported command.", True)
            return
        self._save({"commands": {phrase: canonical}}, "Command shortcut saved.")

    def _remove_mapping(self, kind: str, phrase: str) -> None:
        # Removal is a read-modify-write on the current daemon config, not an old UI snapshot.
        def remove() -> dict:
            with self._save_lock:
                current = client.request("config")
                if not current.get("ok"):
                    return current
                config = current["config"]
                config.setdefault(kind, {}).pop(phrase, None)
                if kind == "aliases":
                    config.setdefault("app_prefixes", {}).pop(phrase, None)
                result = client.request("save_config", config=config)
                if result.get("ok"):
                    result["config"] = config
                return result
        def removed(result: dict) -> None:
            if not result.get("ok"):
                self._notice(str(result.get("error", "Could not remove shortcut.")), True)
                return
            self.config = result["config"]
            self._show_language()
            self._render_aliases()
            self._render_phrases()
            self._notice("Shortcut removed.")
        self._work(remove, removed)

    def _action(self, op: str, **kwargs: Any) -> None:
        if not self.connected:
            self._notice("Start or reconnect to the local service first.", True)
            return
        def action() -> dict:
            result = client.request(op, **kwargs)
            if result.get("ok"):
                status = client.request("status")
                if status.get("ok"):
                    result["status"] = status.get("status", {})
            return result
        def done(result: dict) -> None:
            if not result.get("ok"):
                self._notice(str(result.get("error", "The action failed.")), True)
                return
            if "status" in result:
                self._set_status(result)
            self._notice(str(result.get("message", "Action sent to the local service.")))
        self._work(action, done)

    def _toggle_mute(self) -> None:
        if self.status.get("muted", True) and not self.config.get("privacy", {}).get("onboarded"):
            self.stack.set_visible_child_name("privacy")
            self._notice("Complete the privacy setup before opening the microphone.", True)
            return
        self._action("mute", muted=not bool(self.status.get("muted", True)))

    def _listen(self) -> None:
        if not self.config.get("privacy", {}).get("onboarded"):
            self.stack.set_visible_child_name("privacy")
            self._notice("Complete the privacy setup before opening the microphone.", True)
            return
        self._action("cancel" if self.status.get("state") == "listening" else "listen")

    def _toggle_test(self) -> None:
        if not self.config.get("privacy", {}).get("onboarded"):
            self.stack.set_visible_child_name("privacy")
            self._notice("Complete privacy setup before testing the microphone.", True)
            return
        enabled = not self._test_active
        def done(result: dict) -> None:
            if not result.get("ok"):
                self._notice(str(result.get("error", "The microphone test failed.")), True)
                return
            self._show_test(enabled)
            self._notice("Microphone test active. Stop the test when finished." if enabled else "Microphone test stopped.")
        self._work(lambda: client.request("mic_test", enabled=enabled), done)

    def _show_state(self, state: str) -> None:
        pills = {"offline": "Offline", "paused": "Mic off", "idle": "Waiting for wake word",
                 "listening": "Listening", "processing": "Working", "dictating": "Dictating", "error": "Needs attention"}
        self.state_pill.set_text(pills.get(state, state.capitalize()))
        for name in pills:
            self.state_pill.remove_css_class(name)
        self.state_pill.add_css_class(state)
        self.voice_badge.remove_css_class("active")
        self.voice_badge.remove_css_class("error")
        if state in ("listening", "processing", "dictating", "error"):
            self.voice_badge.add_css_class("error" if state == "error" else "active")

    def _show_test(self, active: bool) -> None:
        self._test_active = active
        self.test_button.set_label("Stop microphone test" if active else "Test microphone")
        if not active:
            self.level.set_value(0)
            self.level_text.set_text("Input test is off")

    def _start_service(self) -> None:
        self.start_button.set_sensitive(False)
        self._notice("Starting the local service. The microphone stays closed until setup is complete.")
        def launch() -> dict:
            started_by, log = start_service_process()
            # A finite startup wait, not an idle polling loop. Normal operation is socket-driven.
            waiter = threading.Event()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if Path(client.socket_path()).is_socket():
                    try:
                        return client.request("status")
                    except OSError:
                        pass
                waiter.wait(.1)
            where = ("journalctl --user -u murmur.service -n 50" if started_by == "systemd"
                     else f"the log at {log}")
            return {"ok": False, "error": f"The service did not become ready. Check {where}, then choose Reconnect."}
        def done(result: dict) -> None:
            self.start_button.set_sensitive(True)
            if result.get("ok"):
                self.refresh()
            else:
                self._notice(str(result.get("error", "Service startup failed.")), True)
        self._work(launch, done)

    def _close(self, _window: Any) -> bool:
        if self._test_active:
            if not self._closing_test:
                self._closing_test = True
                self._notice("Stopping the microphone test before closing…")
                def stopped(_result: dict) -> None:
                    self._test_active = False
                    self._closing_test = False
                    self.close()
                self._work(lambda: client.request("mic_test", enabled=False), stopped)
            return True
        self.closed = True
        self._watch_stop.set()
        return False


def service_log_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return state / "murmur" / "daemon.log"


def start_service_process() -> tuple[str, Path | None]:
    """Prefer the installed user service so systemd owns, logs and cleans up the
    daemon. Without it, start a detached daemon whose output goes to a private
    log file (not /dev/null) and reap it so it never lingers as a zombie."""
    systemctl = shutil.which("systemctl")
    if systemctl:
        unit = subprocess.run([systemctl, "--user", "cat", "murmur.service"],
                              capture_output=True, text=True, timeout=5, check=False)
        if unit.returncode == 0:
            subprocess.run([systemctl, "--user", "start", "murmur.service"],
                           capture_output=True, text=True, timeout=10, check=False)
            return "systemd", None
    log = service_log_path()
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if log.stat().st_size > 1024 * 1024:
            log.replace(log.with_suffix(".log.1"))  # keep one previous log, bounded size
    except FileNotFoundError:
        pass
    with open(log, "ab", buffering=0) as output:
        os.chmod(log, 0o600)
        process = subprocess.Popen([sys.executable, "-m", "murmur", "daemon"],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=True)
    threading.Thread(target=process.wait, name="murmur-daemon-reaper", daemon=True).start()
    return "process", log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis native GTK4 settings")
    parser.add_argument("--page", choices=sorted(PAGES), default="dashboard")
    args = parser.parse_args(argv)
    if Gtk is None:
        print("Jarvis settings require GTK4 and PyGObject (python-gobject on Arch, python3-gi and gir1.2-gtk-4.0 on Debian/Ubuntu).\n"
              f"Details: {GTK_ERROR}\nThe background CLI can run without the settings window.", file=sys.stderr)
        return 1
    if not Gtk.init_check():
        print("Cannot open a graphical display. Run ‘jarvis ui’ inside your Wayland desktop session. The service does not need a display window.", file=sys.stderr)
        return 1
    application = Gtk.Application(application_id="org.murmur.Controller", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
    def activate(app: Any) -> None:
        window = app.get_active_window()
        if window is None:
            display = Gdk.Display.get_default()
            provider = Gtk.CssProvider()
            provider.load_from_path(str(Path(__file__).with_name("style.css")))
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            window = JarvisWindow(app, args.page)
        window.present()
    def command_line(app: Any, command: Any) -> int:
        requested = parser.parse_args(command.get_arguments()[1:])
        activate(app)
        window = app.get_active_window()
        if window is not None:
            window.stack.set_visible_child_name(requested.page)
        return 0
    application.connect("activate", activate)
    application.connect("command-line", command_line)
    return application.run(["murmur-ui", "--page", args.page])


if __name__ == "__main__":
    raise SystemExit(main())
