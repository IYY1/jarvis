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
from .encoding import CODECS, CODEC_LABELS, QUALITY_PRESETS, storage_estimate


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
        self.player = None          # Gtk.Video, created on the first Play so GStreamer loads only then
        self._playing = ""
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
        self._commands_page()
        self._applications_page()
        self._recording_page()
        self._clips_page()
        self._voice_page()
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
            "dashboard": "Overview", "commands": "Commands", "applications": "Applications", "clips": "Clips",
            "recording": "Recorder", "voice": "Voice & microphone", "privacy": "Privacy & startup",
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
        body = self._page("dashboard", "A quieter way to control your desktop.",
                          "Stay in your flow. Open an app, switch workspaces, or record a moment.")
        self.connection_card = self._card("The background service is offline",
                                         "Start the local service to configure Jarvis. Microphone access waits for your privacy choice.")
        actions = _box(8, True)
        self.start_button = _button("Start service", self._start_service, "primary")
        actions.append(self.start_button)
        actions.append(_button("Reconnect", self.refresh))
        self.connection_card.append(actions)
        body.append(self.connection_card)

        self.onboard_banner = self._card("Make the microphone yours",
            "Choose when Jarvis may listen. Complete the privacy setup before the first microphone session.")
        self.onboard_banner.append(_button("Review privacy & activation", lambda: self.stack.set_visible_child_name("privacy"), "primary"))
        body.append(self.onboard_banner)
        self.onboard_banner.set_visible(False)

        status_row = _box(16, True)
        status_row.set_homogeneous(True)
        voice = self._card("VOICE CONTROL")
        voice.set_hexpand(True)
        self.voice_badge = _label("Service offline", "state-title")
        voice.append(self.voice_badge)
        self.voice_message = _label("Waiting for the service.", "caption")
        voice.append(self.voice_message)
        self.listen_button = _button("Listen once", self._listen, "primary")
        self.listen_button.set_tooltip_text("Open one bounded command session (Ctrl+Space)")
        voice.append(self.listen_button)
        status_row.append(voice)
        recording = self._card("SCREEN RECORDING")
        recording.set_hexpand(True)
        self.record_badge = _label("Not recording", "state-title")
        self.record_message = _label("Choose a capture source before your first recording.", "caption")
        self.record_message.set_selectable(True)
        recording.append(self.record_badge)
        recording.append(self.record_message)
        recording.append(_button("Open recorder", lambda: self.stack.set_visible_child_name("recording")))
        status_row.append(recording)
        body.append(status_row)

        ideas = self._card("Small phrases. Useful actions.", "After activation, try one of the supported commands.")
        for phrase, detail in [("“Open Firefox”", "Find and launch an installed app"),
                               ("“Close Firefox”", "Close the app's windows normally"),
                               ("“Go to workspace three”", "Move around Hyprland"),
                               ("“Start recording”", "Capture without leaving your game")]:
            line = _box(4)
            line.append(_label(phrase, "example"))
            line.append(_label(detail, "caption"))
            ideas.append(line)
        ideas.append(_button("Explore all commands →", lambda: self.stack.set_visible_child_name("commands")))
        body.append(ideas)
        body.append(_label("Closing this window releases the settings UI. Voice and recording status come from the background service.", "caption"))

    def _commands_page(self) -> None:
        body = self._page("commands", "Say it your way.", "Find supported phrases and add shortcuts to the words you use most.")
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
        body = self._page("applications", "Give your apps familiar names.", "Open, focus or close your installed desktop applications. Aliases work with all three commands.")
        self.app_search = Gtk.SearchEntry()
        self.app_search.set_placeholder_text("Search installed applications")
        self.app_search.connect("search-changed", lambda _entry: self._render_apps())
        body.append(self.app_search)
        self.app_grammar_notice = _label("", "caption")
        self.app_grammar_notice.set_visible(False)
        body.append(self.app_grammar_notice)
        self.app_list = _box(12)
        body.append(self.app_list)
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

    def _recording_page(self) -> None:
        body = self._page("recording", "Keep the moment.", "Set up once, then start and stop recording with your voice.")
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
        self.replay_seconds = self._spin(10, 600, 10)
        self._row(replay, "Replay length (seconds)", self.replay_seconds)
        self.replay_storage = Gtk.DropDown.new_from_strings(["RAM", "Disk (continuous writes)"])
        self._row(replay, "Replay storage", self.replay_storage, "Disk requires the Rust controller and a real disk; tmpfs is refused.")
        self.replay_max_mb = self._spin(64, 16384, 64)
        self._row(replay, "Disk buffer budget (MiB)", self.replay_max_mb,
                  "Disk only: replay stops when the buffer reaches this size, checked every 250 ms. At least 512 MiB, "
                  "because gpu-screen-recorder writes 256 MiB buffer files. RAM replay is not capped on Linux.")
        self.replay_storage.connect("notify::selected", lambda *_: self._replay_storage_changed())
        self.replay_buffer_dir = self._entry("Default private app-data directory")
        self._row(replay, "Private disk-buffer directory", self.replay_buffer_dir, "Optional dedicated directory. Unsaved buffers are deleted on stop and stale buffers on next start.")
        self.replay_game = Gtk.Switch()
        self._row(replay, "Game sound", self.replay_game, "Everything you hear, from your default output.")
        self.replay_mic = Gtk.Switch()
        self._row(replay, "Microphone", self.replay_mic, "Your voice from the default input, mixed into the same track.")
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
        self._row(capture, "Use a preconfigured combined audio source", self.combined_audio, "Enable only after creating a PipeWire source mixing microphone and system audio. Then select that source below.")
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
        quality = self._card("Picture & storage", "Applies to recordings and replay. Hardware formats require GPU Screen Recorder and a GPU that supports the selected format. No CPU fallback.")
        self.codec = Gtk.DropDown.new_from_strings(CODEC_LABELS)
        self._row(quality, "Video format", self.codec,
                  "AV1 saves the most space at similar quality on supported GPUs. HEVC is a useful middle ground; H.264 is easiest to share and edit. HDR uses AV1 or HEVC 10-bit.")
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
        self.quality_preset = Gtk.DropDown.new_from_strings(["Compact", "Balanced", "Sharp"])
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
        quality.append(_label("Presets select GPU AV1 and a bitrate budget. Check that your GPU and player support AV1 before recording. Existing settings change only when saved.", "caption"))
        self.sound_feedback = Gtk.Switch()
        self._row(quality, "Recording sounds", self.sound_feedback, "Short local tones on start, save and error.")
        body.append(quality)
        card, legacy = self._advanced("Legacy encoder tuning", "Only used for H.264 CPU / legacy VA-API in quality mode; GPU presets use the quality choice above.")
        self.quality = self._spin(0, 51, 1)
        self._row(legacy, "H.264 quality value", self.quality, "Lower values improve quality and increase file size. Your existing value is preserved.")
        body.append(card)
        self.codec.connect("notify::selected", lambda *_: self._encoding_changed())
        self.rate_control.connect("notify::selected", lambda *_: self._encoding_changed())
        self.bitrate.connect("value-changed", lambda *_: self._encoding_changed())
        self.fps.connect("value-changed", lambda *_: self._encoding_changed())
        self.replay_seconds.connect("value-changed", lambda *_: self._encoding_changed())
        body.append(_button("Save recorder settings", self._save_recording, "primary"))
        body.append(_label("Opening the recorder never starts capture. Stop & save requests a graceful finalization; wait for the saved-file confirmation.", "caption"))

    def _clips_page(self) -> None:
        body = self._page("clips", "Your clips.", "Saved replays and recordings, newest first. Play them right here.")
        self.player_card = self._card("Now playing")
        self.player_title = _label("", "caption")
        self.player_card.append(self.player_title)
        self.player_card.append(_label("HDR color depends on player support. If colors look washed out, use Open in your player "
                                       "with HDR output or HDR-to-SDR tone mapping enabled.", "caption"))
        player_actions = _box(8, True)
        player_actions.append(_button("Close player", self._close_player))
        player_actions.append(_button("Open in your player", lambda: self._launch_clip(False)))
        player_actions.append(_button("Show in folder", lambda: self._launch_clip(True)))
        self.player_card.append(player_actions)
        self.player_card.set_visible(False)
        body.append(self.player_card)
        library = self._card("Library")
        self.clips_caption = _label("Your saved clips appear here.", "caption")
        library.append(self.clips_caption)
        self.clip_list = _box(8)
        library.append(self.clip_list)
        library.append(_button("Refresh", self._refresh_clips))
        body.append(library)
        self.stack.connect("notify::visible-child-name", lambda *_: self._clips_page_shown())

    def _clips_page_shown(self) -> None:
        if self.stack.get_visible_child_name() == "clips":
            self._refresh_clips()
        else:
            self._close_player()   # leaving the page releases the video and its pipeline

    def _refresh_clips(self) -> None:
        folder = Path(str(self.config.get("recording", {}).get("output_dir", "~/Videos/Murmur"))).expanduser()
        self._work(lambda: clips(folder), lambda found: self._render_clips(folder, found if isinstance(found, list) else []))

    def _render_clips(self, folder: Path, found: list) -> None:
        self._clear(self.clip_list)
        self.clips_caption.set_text(f"{len(found)} clips in {folder}" if found else
                                    f"No clips in {folder} yet. Say “clip that” while instant replay is on.")
        for clip in found:
            row = _box(12, True)
            row.add_css_class("app-row")
            labels = _box(4)
            labels.set_hexpand(True)
            labels.append(_label(clip["name"], "row-title"))
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(clip["modified"]))   # no locale month names in English text
            labels.append(_label(f"{when} · {clip['bytes'] / 1048576:.1f} MB", "caption"))
            play = _button("Play", lambda path=clip["path"]: self._play_clip(path))
            play.set_valign(Gtk.Align.CENTER)
            play.update_property([Gtk.AccessibleProperty.LABEL], [f"Play {clip['name']}"])
            row.append(labels)
            row.append(play)
            self.clip_list.append(row)

    def _play_clip(self, path: str) -> None:
        if self.player is None:
            self.player = Gtk.Video(autoplay=True)
            self.player.set_size_request(-1, 360)
            self.player_card.insert_child_after(self.player, self.player_title)
        self.player.set_filename(path)
        stream = self.player.get_media_stream()
        if stream is not None:
            stream.connect("notify::error", self._player_error)
        self._playing = path
        self.player_title.set_text(Path(path).name)
        self.player_card.set_visible(True)

    def _player_error(self, stream: Any, _pspec: Any) -> None:
        if stream.get_error() is not None:
            self._notice("This clip can't play here. Install gst-plugins-good for MP4 playback, or use “Open in your player”.", True)

    def _close_player(self) -> None:
        if self.player is not None:
            self.player.set_file(None)   # drops the media stream and its GStreamer pipeline
        self.player_card.set_visible(False)

    def _launch_clip(self, folder: bool) -> None:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(self._playing))
        (launcher.open_containing_folder if folder else launcher.launch)(self, None, None)

    def _voice_page(self) -> None:
        body = self._page("voice", "A microphone on your terms.", "Hands-free activation uses a small local recognizer. Push-to-talk is available when you prefer it.")
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
                  "Light uses Whisper tiny: about 280 MB at most while you give commands instead of about 550 MB, "
                  "and answers a little faster. It may mishear unusual names more often; test it with your voice.")
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
        body = self._page("privacy", "You decide when it listens.", "Local processing, a visible state, and an immediate microphone mute.")
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
        titles = {"paused": "Microphone paused", "idle": "Waiting for your wake phrase", "listening": "Listening to your command", "processing": "Working on it", "error": "Voice needs attention"}
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
        self.sensitivity.set_value(float(activation.get("sensitivity", .5)))
        self.timeout.set_value(float(activation.get("command_timeout", 8)))
        self.confidence.set_value(float(activation.get("confidence", .35)))
        self.vad_rms.set_value(float(audio.get("vad_rms", .005)))
        recognition = self.config.get("recognition", {})
        self.closest_match.set_active(bool(recognition.get("closest_match", True)))
        self.accurate.set_active(bool(recognition.get("accurate", True)))
        self.speech_model.set_selected(1 if recognition.get("speech_model") == "light" else 0)
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
        self.replay_sound.set_active(bool(replay.get("sound", True)))
        self._populate_record_devices()
        self.fps.set_value(float(recorder.get("fps", 30)))
        codecs = CODECS
        self.codec.set_selected(codecs.index(recorder.get("codec")) if recorder.get("codec") in codecs else 0)
        self.quality.set_value(float(recorder.get("quality", 23)))
        self.rate_control.set_selected(1 if recorder.get("rate_control") == "bitrate" else 0)
        self.bitrate.set_value(float(recorder.get("bitrate_mbps", 8)))
        self.quality_preset.set_selected(QUALITY_PRESETS.index(recorder.get("quality_preset", "very_high")))
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
        visible = [app for app in self._apps if needle in f"{app.get('name', '')} {app.get('id', '')}".casefold()]
        self.app_list.append(_label(f"{len(visible)} installed applications" if self._apps else "Connect to discover installed applications.", "caption"))
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
                                    "noise_suppression": self.noise_suppression.get_active()},
                    "audio": {"device": device, "vad_rms": self.vad_rms.get_value(),
                              "adaptive_gate": self.adaptive_gate.get_active()}, "activation": {
            "mode": "wake_word" if self.activation.get_selected() == 1 else "push_to_talk",
            "wake_phrase": self.wake_phrase.get_text().strip().lower(),
            "wake_aliases": [name.strip() for name in self.wake_aliases.get_text().split(",") if name.strip()], "sensitivity": self.sensitivity.get_value(),
            "command_timeout": self.timeout.get_value_as_int(), "confidence": self.confidence.get_value(),
        }}, "Voice settings saved. Use Listen once, or unmute to enable the selected activation mode.")

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
        self._save({"recording": {"output_dir": self.output_dir.get_text().strip(), "monitor": monitor,
            "system_audio": self.system_audio.get_active(), "microphone": self.record_mic.get_active(),
            "configured": self.capture_configured.get_active(), "sound_feedback": self.sound_feedback.get_active(),
            "combined_audio_source": self.combined_audio.get_active(),
            "color_mode": ["auto", "hdr", "sdr"][self.color_mode.get_selected()],
            "audio_device": source, "fps": int(round(self.fps.get_value())),
            "rate_control": "bitrate" if self.rate_control.get_selected() == 1 else "quality",
            "bitrate_mbps": self.bitrate.get_value_as_int(),
            "quality_preset": QUALITY_PRESETS[self.quality_preset.get_selected()],
            "codec": CODECS[self.codec.get_selected()], "quality": self.quality.get_value_as_int()},
            "replay": {"seconds": self.replay_seconds.get_value_as_int(), "autostart": self.replay_autostart.get_active(),
                       "game_audio": self.replay_game.get_active(), "microphone": self.replay_mic.get_active(),
                       "sound": self.replay_sound.get_active(),
                       "storage": "disk" if self.replay_storage.get_selected() == 1 else "ram",
                       "max_mb": self.replay_max_mb.get_value_as_int(), "buffer_dir": self.replay_buffer_dir.get_text().strip()}},
            "Recorder settings saved. Start recording explicitly, or say “Start recording”.")

    def _replay_storage_changed(self) -> None:
        # Disk needs room for gpu-screen-recorder's 256 MiB files. Raise, never
        # lower, so a larger saved budget is kept.
        if self.replay_storage.get_selected() == 1:
            self.replay_max_mb.set_value(max(512, self.replay_max_mb.get_value()))

    def _encoding_changed(self) -> None:
        budget = self.rate_control.get_selected() == 1
        gpu = self.codec.get_selected() >= 2
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
        self.codec.set_selected(CODECS.index("av1"))
        self.rate_control.set_selected(1)
        self.bitrate.set_value(bitrate)
        self._notice(f"Selected AV1 GPU, {fps} fps, {bitrate} Mbps. Save to apply; hardware support is required.")

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
                 "listening": "Listening", "processing": "Working", "error": "Needs attention"}
        self.state_pill.set_text(pills.get(state, state.capitalize()))
        for name in pills:
            self.state_pill.remove_css_class(name)
        self.state_pill.add_css_class(state)
        self.voice_badge.remove_css_class("active")
        self.voice_badge.remove_css_class("error")
        if state in ("listening", "processing", "error"):
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
