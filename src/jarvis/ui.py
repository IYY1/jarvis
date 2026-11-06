"""Native GTK4 settings. The daemon, microphone and models live elsewhere.

All IPC and discovery run outside GTK's main thread. A socket subscription pushes
status changes; opening this window never starts the daemon or opens a microphone.
Settings apply as they change: each change is saved shortly after the last one
in its group, so a burst of edits makes one save (and at most one capture restart).
"""
from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
import re
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
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    gi.require_version("Pango", "1.0")
    from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango
except (ImportError, ValueError) as _gtk_error:
    Gtk = None
    GTK_ERROR = str(_gtk_error)
else:
    GTK_ERROR = ""

from . import client, palettes
from .commands import REFERENCE, normalize
from .encoding import GPU_CODECS, QUALITY_PRESETS, REPLAY_VIDEO, auto_bitrate, storage_estimate


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
    """Saved videos in the recordings folder and its game folders, newest
    first, named "Game / file" when in one; hidden partial files are skipped."""
    def videos(path: str) -> list:
        try:
            return [entry for entry in os.scandir(path) if entry.is_file() and not entry.name.startswith(".")
                    and entry.name.lower().endswith((".mp4", ".mkv"))]
        except OSError:
            return []
    entries = [(None, entry) for entry in videos(folder)]
    try:
        games = [entry for entry in os.scandir(folder) if entry.is_dir() and not entry.name.startswith(".")]
    except OSError:
        games = []
    for game in games:
        entries += [(game.name, entry) for entry in videos(game.path)]
    entries.sort(key=lambda item: item[1].stat().st_mtime, reverse=True)
    return [{"name": f"{game} / {entry.name}" if game else entry.name, "path": entry.path,
             "modified": entry.stat().st_mtime, "bytes": entry.stat().st_size} for game, entry in entries[:limit]]


def _label(text: str, css: str = "", wrap: bool = True) -> Any:
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_wrap(wrap)
    for name in css.split():
        widget.add_css_class(name)
    return widget


def _box(spacing: int = 12, horizontal: bool = False) -> Any:
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL if horizontal else Gtk.Orientation.VERTICAL,
                   spacing=spacing)


def _button(text: str, callback: Callable, css: str = "") -> Any:
    widget = Gtk.Button(label=text)
    widget.connect("clicked", lambda _button: callback())
    for name in css.split():
        widget.add_css_class(name)
    return widget


def _icon(name: str) -> Any:
    """A symbolic icon, or nothing when the icon theme lacks it (never a broken-image placeholder)."""
    display = Gdk.Display.get_default()
    if display is None or not Gtk.IconTheme.get_for_display(display).has_icon(name):
        return None
    return Gtk.Image(icon_name=name, accessible_role=Gtk.AccessibleRole.PRESENTATION)


def _fit(dropdown: Any) -> Any:
    """Long choices ("Monitor of Radeon High Definition Audio…") end in an
    ellipsis on the button instead of widening the page; the open list shows them whole."""
    def factory(ellipsize: bool) -> Any:
        def setup(_factory: Any, item: Any) -> None:
            label = Gtk.Label(xalign=0)
            if ellipsize:
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_width_chars(8)
                label.set_max_width_chars(26)
            item.set_child(label)
        def bind(_factory: Any, item: Any) -> None:
            text = item.get_item().get_string()
            item.get_child().set_text(text)
            if ellipsize:
                dropdown.set_tooltip_text(text)
        made = Gtk.SignalListItemFactory()
        made.connect("setup", setup)
        made.connect("bind", bind)
        return made
    dropdown.set_factory(factory(True))
    dropdown.set_list_factory(factory(False))
    return dropdown


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


def _duration(seconds: Any) -> str:
    """230 → "3 min 50 s"."""
    minutes, rest = divmod(int(seconds), 60)
    return " ".join(part for part in (f"{minutes} min" if minutes else "", f"{rest} s" if rest or not minutes else "") if part)


# Sidebar order: (page, title, icon).
NAVIGATION = [("dashboard", "Home", "user-home-symbolic"),
              ("voice", "Voice", "audio-input-microphone-symbolic"),
              ("commands", "Commands", "view-list-bullet-symbolic"),
              ("applications", "Apps", "view-app-grid-symbolic"),
              ("recording", "Capture", "media-record-symbolic"),
              ("clips", "Clips", "folder-videos-symbolic"),
              ("general", "General", "emblem-system-symbolic")]
PAGES = {name for name, _, _ in NAVIGATION}
PAGE_ALIASES = {"privacy": "general"}   # older links and launchers

PRIVACY_FACTS = [
    ("Everything stays on this computer", "Speech is recognized locally. No account, cloud service or API key."),
    ("It listens only for its name", "While unmuted, a small local model waits for your wake name. "
                                     "Commands are recognized only in the short session after it."),
    ("Nothing is kept", "Command audio and text are discarded after use. A recording includes your voice "
                        "only when you turn that on."),
    ("Mute is one click away", "Use Mute at the top of this window, or Ctrl+M. Muting doesn't stop a recording "
                               "that includes your microphone; stop the recording for that."),
]


if Gtk:
    class _Clamp(Gtk.Widget):
        """Centers its child and caps its width, so lines stay readable in a wide window."""
        __gtype_name__ = "JarvisClamp"

        def __init__(self, child: Any, width: int = 920) -> None:
            super().__init__()
            self._child, self._width = child, width
            child.set_parent(self)

        def do_get_request_mode(self) -> Any:
            return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

        def do_measure(self, orientation: Any, for_size: int) -> tuple:
            if orientation == Gtk.Orientation.VERTICAL and for_size > self._width:
                for_size = self._width
            minimum, natural, _, _ = self._child.measure(orientation, for_size)
            if orientation == Gtk.Orientation.HORIZONTAL:
                natural = max(minimum, min(natural, self._width))
            return minimum, natural, -1, -1

        def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
            minimum = self._child.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
            inner = max(minimum, min(width, self._width))
            offset = Gsk.Transform.new().translate(Graphene.Point().init((width - inner) // 2, 0))
            self._child.allocate(inner, height, baseline, offset)

        def do_dispose(self) -> None:
            if self._child is not None:
                self._child.unparent()
                self._child = None
            super().do_dispose()


class JarvisWindow(Gtk.ApplicationWindow if Gtk else object):
    def __init__(self, application: Any, page: str = "dashboard") -> None:
        super().__init__(application=application, title="Jarvis")
        self.set_default_size(1080, 780)
        self.set_size_request(800, 540)
        self.add_css_class("jarvis")
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
        self._loading = False         # programmatic changes are shown, not saved
        self._dirty: set[str] = set()
        self._autosave_timer = 0
        self._toast_timer = 0
        self._probe_by_user = False
        self._patches: dict[str, Callable[[], dict | None]] = {
            "voice": self._voice_patch, "replay": self._replay_patch, "recorder": self._recorder_patch,
            "video": self._video_patch, "close_words": self._close_words_patch}
        self._build(page)
        self._install_actions()
        self.connect("close-request", self._close)
        self.refresh()

    # Layout --------------------------------------------------------------

    def _build(self, page: str) -> None:
        outer = _box(0)
        self.set_child(outer)
        outer.append(self._header())
        overlay = Gtk.Overlay()
        overlay.set_vexpand(True)
        outer.append(overlay)
        shell = _box(0, True)
        overlay.set_child(shell)

        rail = _box(16)
        rail.add_css_class("rail")   # not "sidebar": GTK's own sidebar widgets carry that class
        rail.set_size_request(200, -1)
        self.nav = Gtk.ListBox()
        self.nav.add_css_class("nav")
        self.nav.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.nav.update_property([Gtk.AccessibleProperty.LABEL], ["Pages"])
        self.nav.set_vexpand(True)
        self._nav_rows: dict[str, Any] = {}
        rail.append(self.nav)
        local = _box(4)
        local.add_css_class("rail-note")
        local.append(_label("LOCAL BY DEFAULT", "eyebrow"))
        note = _label("Voice stays on this computer. Closing this window keeps Jarvis running.", "caption")
        note.set_max_width_chars(22)
        local.append(note)
        rail.append(local)
        shell.append(rail)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        shell.append(self.stack)

        self._dashboard()
        self._voice_page()
        self._commands_page()
        self._applications_page()
        self._recording_page()
        self._clips_page()
        self._general_page()
        self.nav.connect("row-selected", self._nav_selected)
        self.stack.connect("notify::visible-child-name", lambda *_: self._sync_nav())
        self._toast(overlay)
        self._wire_autosave()
        self.show_page(page)
        self._sync_nav()
        for widget in (self.listen_button, self.record_start, self.record_stop, self.test_button,
                       self.replay_save, self.replay_toggle):
            widget.set_sensitive(False)   # until the service answers

    def _header(self) -> Any:
        header = Gtk.HeaderBar()
        header.set_title_widget(_label("JARVIS", "brand", wrap=False))
        self.state_pill = _label("Connecting", "pill", wrap=False)
        self.state_pill.set_valign(Gtk.Align.CENTER)
        header.pack_start(self.state_pill)
        self.replay_pill = _label("Replay on", "pill replay", wrap=False)
        self.replay_pill.set_valign(Gtk.Align.CENTER)
        self.replay_pill.set_visible(False)
        header.pack_start(self.replay_pill)

        menu = Gio.Menu()
        files = Gio.Menu()
        files.append("Export settings…", "win.export")
        files.append("Import settings…", "win.import")
        files.append("Open backups folder", "win.backups")
        menu.append_section(None, files)
        leave = Gio.Menu()
        leave.append("Quit Jarvis…", "win.quit")
        menu.append_section(None, leave)
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        menu_button.set_tooltip_text("Menu")
        menu_button.update_property([Gtk.AccessibleProperty.LABEL], ["Main menu"])
        header.pack_end(menu_button)

        self.mute_button = Gtk.Button()
        self.mute_button.add_css_class("mute")
        content = _box(8, True)
        self.mute_icon = Gtk.Image(icon_name="audio-input-microphone-symbolic",
                                   accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.mute_text = _label("Mute", wrap=False)
        content.append(self.mute_icon)
        content.append(self.mute_text)
        self.mute_button.set_child(content)
        self.mute_button.connect("clicked", lambda _button: self._toggle_mute())
        self.mute_button.set_sensitive(False)
        header.pack_end(self.mute_button)
        return header

    def _toast(self, overlay: Any) -> None:
        """Results and errors appear in a bubble at the bottom: results fade, errors stay until dismissed."""
        self.toast = Gtk.Revealer()
        self.toast.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.toast.set_halign(Gtk.Align.CENTER)
        self.toast.set_valign(Gtk.Align.END)
        self.toast.set_margin_bottom(20)
        self.toast.set_margin_start(24)
        self.toast.set_margin_end(24)
        # A status region; Gtk.Accessible.announce() crashes GTK 4.24 without an accessibility bus.
        self.toast_bubble = Gtk.Box(spacing=12, accessible_role=Gtk.AccessibleRole.STATUS)
        self.toast_bubble.add_css_class("toast")
        self.notice = _label("", "toast-text")
        self.notice.set_max_width_chars(72)
        self.notice.set_selectable(True)
        self.notice.set_valign(Gtk.Align.CENTER)
        dismiss = Gtk.Button(icon_name="window-close-symbolic")
        dismiss.add_css_class("flat")
        dismiss.set_valign(Gtk.Align.CENTER)
        dismiss.set_tooltip_text("Dismiss")
        dismiss.update_property([Gtk.AccessibleProperty.LABEL], ["Dismiss message"])
        dismiss.connect("clicked", lambda _button: self._hide_toast())
        self.toast_bubble.append(self.notice)
        self.toast_bubble.append(dismiss)
        self.toast.set_child(self.toast_bubble)
        overlay.add_overlay(self.toast)

    def show_page(self, name: str) -> None:
        name = PAGE_ALIASES.get(name, name)
        self.stack.set_visible_child_name(name if name in PAGES else "dashboard")

    def _nav_selected(self, _list: Any, row: Any) -> None:
        if row is not None and self.stack.get_visible_child_name() != row.get_name():
            self.stack.set_visible_child_name(row.get_name())

    def _sync_nav(self) -> None:
        row = self._nav_rows.get(self.stack.get_visible_child_name())
        if row is not None and not row.is_selected():
            self.nav.select_row(row)

    def _page(self, name: str, title: str, subtitle: str) -> Any:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = _box(28)
        body.add_css_class("page")
        heading = _box(6)
        heading.append(_label(title, "page-title"))
        heading.append(_label(subtitle, "subtitle"))
        body.append(heading)
        scroller.set_child(_Clamp(body))
        self.stack.add_titled(scroller, name, title)
        icon = dict((page, icon) for page, _, icon in NAVIGATION)[name]
        row = Gtk.ListBoxRow()
        row.set_name(name)
        row.update_property([Gtk.AccessibleProperty.LABEL], [title])
        line = _box(12, True)
        image = _icon(icon)
        if image is not None:
            line.append(image)
        line.append(_label(title, wrap=False))
        row.set_child(line)
        self.nav.append(row)
        self._nav_rows[name] = row
        return body

    def _card(self, title: str, subtitle: str = "") -> Any:
        card = _box(12)
        card.add_css_class("card")
        if title:
            card.append(_label(title, "section-title"))
        if subtitle:
            card.append(_label(subtitle, "caption"))
        return card

    def _group(self, parent: Any, title: str, description: str = "") -> Any:
        """A titled list of setting rows; returns the list to add rows to."""
        section = _box(8)
        if title:
            section.append(_label(title, "group-title"))
        if description:
            section.append(_label(description, "caption"))
        rows = _box(0)
        rows.add_css_class("group")
        section.append(rows)
        parent.append(section)
        return rows

    def _advanced(self, parent: Any, title: str, description: str) -> tuple[Any, Any]:
        """A group whose rows stay folded away until asked for."""
        section = _box(8)
        expander = Gtk.Expander()
        expander.set_label_widget(_label(title, "group-title"))
        content = _box(8)
        content.set_margin_top(8)
        content.append(_label(description, "caption"))
        rows = _box(0)
        rows.add_css_class("group")
        content.append(rows)
        expander.set_child(content)
        section.append(expander)
        parent.append(section)
        return expander, rows

    def _help(self, title: str, text: str) -> Any:
        """An ⓘ button whose popover holds the details a row's one line leaves out."""
        button = Gtk.MenuButton(icon_name="help-about-symbolic")
        button.add_css_class("help")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text("More about this")
        button.update_property([Gtk.AccessibleProperty.LABEL], [f"More about {title}"])
        popover = Gtk.Popover()
        detail = _label(text, "popover-text")
        detail.set_max_width_chars(52)
        popover.set_child(detail)
        button.set_popover(popover)
        return button

    def _row(self, parent: Any, title: str, widget: Any, detail: str = "", more: str = "") -> Any:
        row = _box(16, True)
        row.add_css_class("row")
        labels = _box(2)
        labels.set_hexpand(True)
        labels.set_valign(Gtk.Align.CENTER)
        heading = _box(4, True)
        name = _label(title, "row-title")
        heading.append(name)
        if more:
            heading.append(self._help(title, more))
        labels.append(heading)
        if detail:
            labels.append(_label(detail, "caption"))
        widget.set_valign(Gtk.Align.CENTER)
        if isinstance(widget, Gtk.DropDown):
            _fit(widget)
        if isinstance(widget, (Gtk.Entry, Gtk.DropDown)):   # one input column per group
            widget.set_hexpand(False)
            widget.set_size_request(260, -1)
        widget.update_property([Gtk.AccessibleProperty.LABEL], [title])
        row.append(labels)
        row.append(widget)
        parent.append(row)
        return widget

    def _line(self, parent: Any, *widgets: Any, vertical: bool = False) -> Any:
        """A row of free-form content (buttons, captions) inside a group."""
        line = _box(8 if vertical else 12, not vertical)
        line.add_css_class("row")
        for widget in widgets:
            line.append(widget)
        parent.append(line)
        return line

    def _entry(self, placeholder: str = "") -> Any:
        entry = Gtk.Entry()
        entry.set_placeholder_text(placeholder)
        entry.set_width_chars(12)   # the minimum; entries grow with the window
        entry.set_hexpand(True)
        return entry

    def _spin(self, minimum: float, maximum: float, step: float, digits: int = 0) -> Any:
        spin = Gtk.SpinButton.new_with_range(minimum, maximum, step)
        spin.set_digits(digits)
        spin.set_width_chars(6)
        return spin

    @staticmethod
    def _actions(*buttons: Any) -> Any:
        """A row of buttons that wraps onto a second line when the window is narrow."""
        row = Gtk.FlowBox()
        row.set_selection_mode(Gtk.SelectionMode.NONE)
        row.set_max_children_per_line(len(buttons))
        row.set_column_spacing(8)
        row.set_row_spacing(8)
        for button in buttons:
            row.append(button)
            button.get_parent().set_focusable(False)   # Tab reaches the button, not its wrapper
        return row

    @staticmethod
    def _grow(widget: Any) -> Any:
        widget.set_hexpand(True)
        return widget

    # Pages ---------------------------------------------------------------

    def _dashboard(self) -> None:
        body = self._page("dashboard", "Home", "What Jarvis is doing now, and the controls you reach for most.")
        self.connection_card = self._card("Jarvis isn't running",
                                         "Start it to use your voice and change settings. The microphone stays "
                                         "closed until you allow it.")
        self.connection_card.add_css_class("callout")
        self.connection_detail = _label("", "caption")
        self.connection_detail.set_selectable(True)
        self.connection_detail.set_visible(False)
        self.connection_card.append(self.connection_detail)
        actions = _box(8, True)
        self.start_button = _button("Start Jarvis", self._start_service, "primary")
        actions.append(self.start_button)
        actions.append(_button("Try again", self.refresh))
        self.connection_card.append(actions)
        body.append(self.connection_card)

        self.onboard_banner = self._card("Allow the microphone", "Jarvis asks before it ever listens. Here is what "
                                         "allowing it means:")
        self.onboard_banner.add_css_class("callout")
        for title, text in PRIVACY_FACTS:
            fact = _box(2)
            fact.append(_label(title, "row-title"))
            fact.append(_label(text, "caption"))
            self.onboard_banner.append(fact)
        self.consent = Gtk.CheckButton(label="I understand when Jarvis opens the microphone")
        self.onboard_banner.append(self.consent)
        self.onboard_button = _button("Allow and turn on the wake word", self._onboard, "primary")
        self.onboard_button.set_halign(Gtk.Align.START)
        self.onboard_banner.append(self.onboard_button)
        body.append(self.onboard_banner)
        self.onboard_banner.set_visible(False)

        status_row = _box(16, True)
        status_row.set_homogeneous(True)
        voice = self._card("")
        voice.append(_label("VOICE", "eyebrow"))
        self.voice_badge = _label("Service offline", "state-title")
        voice.append(self.voice_badge)
        self.voice_message = _label("Waiting for the service.", "caption")
        self.voice_message.set_selectable(True)
        voice.append(self.voice_message)
        self.listen_button = _button("Listen now", self._listen, "primary")
        self.listen_button.set_tooltip_text("Hear one command now (Ctrl+Space)")
        self.overview_dictate = _button("Start dictation", self._toggle_dictation)
        self.overview_dictate.set_tooltip_text("Type what you say into the focused text field (Ctrl+D)")
        voice.append(self._actions(self.listen_button, self.overview_dictate))
        self.overview_dictation = _label("Dictation is off.", "caption")
        self.overview_dictation.set_selectable(True)
        voice.append(self.overview_dictation)
        self.overview_copy = _button("Copy text that wasn't typed", self._copy_held)
        self.overview_copy.set_halign(Gtk.Align.START)
        self.overview_copy.set_visible(False)
        voice.append(self.overview_copy)
        arabic = _box(12, True)
        arabic.add_css_class("inline-setting")
        arabic.append(self._grow(_label("Arabic commands and dictation", "row-title")))
        self.overview_arabic = Gtk.Switch()
        self.overview_arabic.set_valign(Gtk.Align.CENTER)
        self.overview_arabic.update_property([Gtk.AccessibleProperty.LABEL], ["Arabic commands and dictation"])
        self.overview_arabic.connect("notify::active", lambda *_: None if self._loading else self._toggle_arabic())
        arabic.append(self.overview_arabic)
        voice.append(arabic)
        status_row.append(voice)

        capture = self._card("")
        capture.append(_label("CAPTURE", "eyebrow"))
        self.record_badge = _label("Not recording", "state-title")
        self.record_message = _label("Choose a display before your first recording.", "caption")
        self.record_message.set_selectable(True)
        capture.append(self.record_badge)
        capture.append(self.record_message)
        self.overview_replay = _label("Replay off", "row-title")
        capture.append(self.overview_replay)
        self.overview_audio = _label("", "caption")
        capture.append(self.overview_audio)
        self.overview_save = _button("Save clip", lambda: self._action("replay_save"), "primary")
        self.overview_save.set_tooltip_text("Save the replay as a clip, like saying “clip that”")
        self.overview_record = _button("Start recording", self._overview_record)
        capture.append(self._actions(self.overview_save, self.overview_record,
                                     _button("Clips", lambda: self.show_page("clips"))))
        status_row.append(capture)
        body.append(status_row)

        ideas = self._card("Try saying", "After “Jarvis”, or another wake name.")
        phrases = Gtk.FlowBox()
        phrases.set_selection_mode(Gtk.SelectionMode.NONE)
        phrases.set_min_children_per_line(2)
        phrases.set_max_children_per_line(2)
        phrases.set_homogeneous(True)
        phrases.set_column_spacing(12)
        phrases.set_row_spacing(12)
        for phrase, detail in [("“Open Firefox”", "Open or focus an app"),
                               ("“Clip the last thirty seconds”", "Save part of the replay"),
                               ("“Listen to me”", "Dictate into the focused field"),
                               ("“Go to workspace three”", "Move around Hyprland")]:
            chip = _box(2)
            chip.add_css_class("chip")
            chip.append(_label(phrase, "example"))
            chip.append(_label(detail, "caption"))
            phrases.append(chip)
        ideas.append(phrases)
        every = _button("All commands", self._show_all_commands)
        every.set_halign(Gtk.Align.START)
        ideas.append(every)
        body.append(ideas)

    def _voice_page(self) -> None:
        body = self._page("voice", "Voice", "Wake word, language, dictation and the microphone. Everything is "
                          "recognized on this computer.")
        self.voice_grammar_notice = _label("", "caption notice-inline")
        self.voice_grammar_notice.set_visible(False)
        body.append(self.voice_grammar_notice)

        wake = self._group(body, "Wake word")
        self.activation = Gtk.DropDown.new_from_strings(["Only when I press Listen", "When I say the wake word"])
        self._row(wake, "Listen", self.activation, "",
                  "With the wake word, the microphone stays open while unmuted and a small local model listens "
                  "only for your wake names; commands are recognized in a short session after one. “Only when I "
                  "press Listen” opens the microphone for one command, from Listen now or your shortcut.")
        self.wake_phrase = self._entry("jarvis")
        self._row(wake, "Wake name", self.wake_phrase, "Short, and unlike the words in your games.")
        self.wake_aliases = self._entry("alfred, computer")
        self._row(wake, "Other names", self.wake_aliases, "Separate with commas; up to eight names in all.")

        language = self._group(body, "Language")
        self.language = Gtk.DropDown.new_from_strings(["English", "Arabic and English"])
        self._row(language, "Understands", self.language, "Arabic adds a local model of about 120 MB; English keeps working.",
                  "Arabic commands are a fixed list in Saudi/Najdi and formal wording (see Commands). Dictation "
                  "writes any Arabic speech, but only listed commands ever run. The model is Moonshine Streaming Tiny.")
        self.arabic_status = _label("", "caption")
        self.arabic_status.set_selectable(True)
        arabic_actions = _box(8, True)
        self.arabic_install = _button("Download the Arabic model (118 MB)", self._download_arabic, "primary")
        arabic_actions.append(self.arabic_install)
        self.arabic_file = _button("Install from a file…", self._install_arabic)
        arabic_actions.append(self.arabic_file)
        self._line(language, self.arabic_status, arabic_actions, vertical=True)

        dictation = self._group(body, "Dictation", "Say “Jarvis, listen to me” or “اسمعني”. Jarvis types into the "
                                "text field you're in, never presses Enter, and shows a bar with Stop at the top of "
                                "the screen.")
        self.dictation_state = self._grow(_label("Dictation is off.", "caption"))
        self.dictation_state.set_selectable(True)
        self.dictation_state.set_valign(Gtk.Align.CENTER)
        self.dictation_copy = _button("Copy text that wasn't typed", self._copy_held)
        self.dictation_copy.set_visible(False)
        self.dictation_button = _button("Start dictation", self._toggle_dictation, "primary")
        self.dictation_button.set_tooltip_text("Start or stop typing what you say into the focused text field (Ctrl+D)")
        self._line(dictation, self.dictation_state, self.dictation_copy, self.dictation_button)
        self.dictation_language = Gtk.DropDown.new_from_strings(["Match the phrase", "Always Arabic", "Always English"])
        self._row(dictation, "Language", self.dictation_language, "",
                  "Match the phrase: “اسمعني” dictates Arabic and “listen” English; the Start button dictates Arabic "
                  "while Arabic is on. Arabic is written as spoken, Najdi, Hijazi and other dialects included; "
                  "English words inside Arabic speech usually come out in Arabic script.")
        self.stop_phrases = self._entry("stop dictation, وقف الكتابة")
        self._row(dictation, "Stop phrases", self.stop_phrases, "End a sentence with one to stop. Separate with commas.")
        self.dictation_idle = self._spin(2, 300, 1)
        self._row(dictation, "Stop after quiet", self.dictation_idle, "Seconds of silence before dictation ends.")
        self.keyboard_fallback = Gtk.Switch()
        self._row(dictation, "Type into any app", self.keyboard_fallback,
                  "Also type where an app doesn't report its text fields.",
                  "Most apps tell Jarvis when a text field is focused and whether it is a password field. Some "
                  "(many terminals, games, Electron and X11 apps) don't; with this on, Jarvis types into them like a "
                  "keyboard, but it can't see whether that field is a password. Known password prompts are skipped.")
        self._line(dictation, _label("Shortcut: add bind = SUPER, D, exec, jarvis dictate to your Hyprland config "
                                     "(Jarvis never edits it). Switching windows stops dictation without typing "
                                     "into the new one.", "caption"))

        microphone = self._group(body, "Microphone")
        self.device = Gtk.DropDown.new_from_strings(["Default microphone"])
        self._row(microphone, "Input", self.device)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        refresh.set_valign(Gtk.Align.CENTER)
        refresh.set_tooltip_text("Look for microphones and displays again")
        refresh.update_property([Gtk.AccessibleProperty.LABEL], ["Refresh microphones and displays"])
        refresh.connect("clicked", lambda _button: self._refresh_devices())
        self.device.get_parent().append(refresh)
        self.level = Gtk.LevelBar.new_for_interval(0, 1)
        self.level.set_size_request(200, -1)
        self.level.set_tooltip_text("Microphone input level; updated only while the test runs")
        self._row(microphone, "Level", self.level, "The test opens the microphone for at most 15 seconds.")
        self.level.update_property([Gtk.AccessibleProperty.LABEL], ["Microphone input level"])
        self.test_button = _button("Test microphone", self._toggle_test)
        self.test_button.set_valign(Gtk.Align.CENTER)
        self.level.get_parent().append(self.test_button)
        self.level_text = _label("Input test is off", "caption")
        self._line(microphone, self.level_text)

        recognizer = self._group(body, "Recognition", "Vosk hears your wake name, then Whisper writes down the "
                                 "command; it copes much better with accents. Whisper unloads a minute after your "
                                 "last command.")
        self.engine_label = self._grow(_label("Recognizer: not running", "row-title"))
        self.engine_label.set_valign(Gtk.Align.CENTER)
        self.install_model_button = _button("Download voice models (790 MB)", self._install_model, "primary")
        self._line(recognizer, self.engine_label, self.install_model_button)

        _, tuning = self._advanced(body, "Fine-tuning", "The defaults suit most rooms. Change one setting at a "
                                   "time, then test with your own voice.")
        self.accurate = Gtk.Switch()
        self._row(tuning, "Accurate command model", self.accurate, "Off saves memory; commands then use Vosk only.")
        self.speech_model = Gtk.DropDown.new_from_strings(["Small (most accurate)", "Light (less memory)"])
        self._row(tuning, "Command model", self.speech_model, "Light uses about half the memory.",
                  "Light (the default) uses Whisper tiny: at most about 280 MB while you give commands instead of "
                  "about 550 MB, and it answers a little faster. Choose Small if unusual names are misheard.")
        self.noise_suppression = Gtk.Switch()
        self._row(tuning, "Noise suppression", self.noise_suppression, "For steady noise such as fans or hum.",
                  "It can't remove other voices, and in tests it slightly lowered accuracy, so it's off by default. "
                  "Try it if your room is noisy.")
        self.adaptive_gate = Gtk.Switch()
        self._row(tuning, "Ignore background sound", self.adaptive_gate, "Only a voice close to the microphone counts.",
                  "Raises the speech threshold on its own while there is steady sound in the room, such as a TV, "
                  "game or music.")
        self.closest_match = Gtk.Switch()
        self._row(tuning, "Closest-match guessing", self.closest_match, "Run the nearest command when one isn't heard exactly.",
                  "Only when it is clearly closer than any other command. The status shows what was heard.")
        self.sensitivity = self._spin(0, 1, .05, 2)
        self._row(tuning, "Wake sensitivity", self.sensitivity, "Higher hears quieter wake names, but triggers more often.")
        self.timeout = self._spin(2, 15, 1)
        self._row(tuning, "Longest command", self.timeout, "Seconds a command session may last.")
        self.confidence = self._spin(0, 1, 0.05, 2)
        self._row(tuning, "Minimum command score", self.confidence, "Lower accepts more, including mistakes.",
                  "A grammar posterior score, not a probability of being right. Tune it with your own voice and room.")
        self.vad_rms = self._spin(0.0001, 0.5, 0.001, 4)
        self._row(tuning, "Speech energy threshold", self.vad_rms, "Higher ignores more quiet noise.")

    def _commands_page(self) -> None:
        body = self._page("commands", "Commands", "What you can say after your wake name, and shortcuts of your own.")
        say = self._group(body, "Say this")
        grid = Gtk.FlowBox()
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_min_children_per_line(2)
        grid.set_max_children_per_line(2)
        grid.set_homogeneous(True)
        grid.set_column_spacing(24)
        grid.set_row_spacing(14)
        grid.set_hexpand(True)
        for group in REFERENCE:
            item = _box(2)
            item.append(_label(group["title"], "row-title"))
            item.append(_label(" · ".join(f"“{e}”" for e in group["examples"][:2]), "caption"))
            grid.append(item)
        self._line(say, grid)

        every = self._group(body, "Every command", "Including two phrases for every app Jarvis can open.")
        self.command_search = Gtk.SearchEntry()
        self.command_search.set_placeholder_text("Search actions or phrases")
        self.command_search.set_hexpand(True)
        self.command_search.connect("search-changed", self._filter_commands)
        self._line(every, self.command_search)
        self.command_list = Gtk.FlowBox()
        self.command_list.add_css_class("commands")
        self.command_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.command_list.set_min_children_per_line(2)
        self.command_list.set_max_children_per_line(2)
        self.command_list.set_homogeneous(True)
        self.command_list.set_column_spacing(12)
        self.command_list.set_row_spacing(12)
        self.command_list.set_hexpand(True)
        self._line(every, self.command_list)
        self.command_previous = _button("Previous", lambda: self._change_command_page(-1))
        self.command_next = _button("Next", lambda: self._change_command_page(1))
        self.command_count = self._grow(_label("", "caption"))
        self.command_count.set_xalign(0.5)
        self._line(every, self.command_previous, self.command_count, self.command_next)

        custom = self._group(body, "Your shortcuts", "Map a phrase of your own to a built-in command. Shell "
                             "commands are never run.")
        self.phrase_list = _box(0)
        custom.append(self.phrase_list)
        self.new_phrase = self._entry("Your phrase, e.g. capture that")
        self.new_phrase.update_property([Gtk.AccessibleProperty.LABEL], ["Your shortcut phrase"])
        self.canonical_phrase = self._entry("Built-in command, e.g. start recording")
        self.canonical_phrase.update_property([Gtk.AccessibleProperty.LABEL], ["Built-in command for the shortcut"])
        self.canonical_phrase.connect("activate", lambda _entry: self._add_phrase())
        self._line(custom, self.new_phrase, self.canonical_phrase, _button("Add", self._add_phrase))

    def _applications_page(self) -> None:
        body = self._page("applications", "Apps", "Apps you can open, focus or close by voice. Give them names "
                          "that are easy to say, or add programs Jarvis didn't find.")
        self.app_search = Gtk.SearchEntry()
        self.app_search.set_placeholder_text("Search apps")
        self.app_search.connect("search-changed", lambda _entry: self._render_apps())
        body.append(self.app_search)
        self.app_grammar_notice = _label("", "caption notice-inline")
        self.app_grammar_notice.set_visible(False)
        body.append(self.app_grammar_notice)
        listing = _box(8)
        self.app_count = _label("", "caption")
        listing.append(self.app_count)
        self.app_list = _box(0)
        self.app_list.add_css_class("group")
        listing.append(self.app_list)
        body.append(listing)

        aliases = self._group(body, "Your voice names", "Each name opens one app. Removing a name keeps the app "
                              "installed. The launch word comes before the name; clear it to say only the name.")
        self.alias_list = _box(0)
        aliases.append(self.alias_list)
        self.alias_name = self._entry("Name, e.g. web browser")
        self.alias_name.update_property([Gtk.AccessibleProperty.LABEL], ["Voice name"])
        self.alias_app = Gtk.DropDown.new_from_strings(["Load installed applications first"])
        self.alias_app.set_hexpand(True)
        # Type to find an app instead of scrolling through every desktop entry.
        self.alias_app.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.alias_app.set_enable_search(True)
        _fit(self.alias_app)
        self._alias_model_ids: tuple = ()
        self.alias_app.update_property([Gtk.AccessibleProperty.LABEL], ["App the name opens"])
        self.alias_prefix = Gtk.Entry()
        self.alias_prefix.set_placeholder_text("Launch word")
        self.alias_prefix.set_width_chars(10)
        self.alias_prefix.set_text("open")
        self.alias_prefix.update_property([Gtk.AccessibleProperty.LABEL], ["Launch word; clear it to say only the name"])
        self._line(aliases, self.alias_prefix, self.alias_name, self.alias_app, _button("Add", self._add_alias))

        closing = self._group(body, "Close words")
        self.close_words = self._entry("close, quit")
        self._row(closing, "Words that close an app", self.close_words,
                  "Separate with commas, up to eight. Keep “close” in the list to still say it.")

        adding = self._group(body, "Add a program", "For programs without a launcher entry, such as AppImages, "
                             "portable programs and scripts. Jarvis adds a normal launcher entry, so the program "
                             "also shows in your app menu.")
        self.add_app_name = self._entry("Name to say, e.g. Obsidian")
        self._row(adding, "Name", self.add_app_name)
        self.add_app_path = ""
        self.add_app_file = self._grow(_label("No file chosen", "caption"))
        self.add_app_file.set_valign(Gtk.Align.CENTER)
        self._line(adding, self.add_app_file, _button("Choose a file…", self._choose_app_file),
                   _button("Add program", self._add_app, "primary"))
        self.added_apps = _box(0)
        adding.append(self.added_apps)
        self.appimage_hints = _box(0)
        adding.append(self.appimage_hints)

    def _recording_page(self) -> None:
        body = self._page("recording", "Capture", "Recording, instant replay and the sound they keep. Changes apply "
                          "right away: a running recording is saved and continues in a new file, and replay "
                          "starts over empty.")
        now = self._group(body, "Now")
        record_info = self._grow(_box(2))
        self.recorder_status = _label("Not recording", "state-title small")
        self.recorder_file = _label("Your saved file will appear here.", "caption")
        self.recorder_file.set_selectable(True)
        record_info.append(self.recorder_status)
        record_info.append(self.recorder_file)
        self.record_start = _button("Start recording", self._start_recording, "primary")
        self.record_stop = _button("Stop & save", lambda: self._action("record_stop"))
        for button in (self.record_start, self.record_stop):
            button.set_valign(Gtk.Align.CENTER)
        self._line(now, record_info, self.record_start, self.record_stop)
        replay_info = self._grow(_box(2))
        self.replay_status = _label("Replay off", "state-title small")
        self.replay_detail = _label("Start replay to keep the last moments ready to save.", "caption")
        self.replay_detail.set_selectable(True)
        replay_info.append(self.replay_status)
        replay_info.append(self.replay_detail)
        self.replay_save = _button("Save clip", lambda: self._action("replay_save"), "primary")
        self.replay_toggle = _button("Start replay", lambda: self._action(
            "replay_stop" if self.status.get("replay", {}).get("state") == "on" else "replay_start"))
        for button in (self.replay_save, self.replay_toggle):
            button.set_valign(Gtk.Align.CENTER)
        self._line(now, replay_info, self.replay_save, self.replay_toggle)

        replay = self._group(body, "Instant replay", "Keeps the last moments of the display below, ready to save "
                             "as a clip.")
        self.replay_seconds = self._spin(10, 1800, 10)
        self._row(replay, "Length", self.replay_seconds, "Seconds, up to 1800 (30 minutes).",
                  "Say “clip that” for all of it, or “clip the last 30 seconds” / “clip the last 2 minutes”. A "
                  "replay kept in RAM holds all of it in memory, often 1–3 GB for 20 minutes of gameplay; choose "
                  "Disk under Advanced for long replays.")
        self.replay_game = Gtk.Switch()
        self._row(replay, "Game sound", self.replay_game, "Everything you hear, from your default output.")
        self.replay_mic = Gtk.Switch()
        self._row(replay, "Microphone", self.replay_mic, "Your voice, from the default input.")
        self.replay_tracks = Gtk.DropDown.new_from_strings(["One mixed track", "Separate tracks"])
        self._row(replay, "Sound tracks", self.replay_tracks, "Separate tracks let you mute an app or your voice later.",
                  "One mixed track plays everywhere. Separate tracks keep the game, each app below and your "
                  "microphone apart, so you can mute a podcast or your voice in Clips. Sound mixed while recording "
                  "can't be separated afterwards. Linux only; Windows replays keep one track.")
        self.replay_apps = self._entry("Firefox")
        self._row(replay, "Apps on their own track", self.replay_apps, "Up to four, separated by commas.",
                  "Named as gpu-screen-recorder lists them; use Show apps playing sound. The game track then holds "
                  "everything except these apps, so muting one later leaves the game intact.")
        self.replay_apps_found = self._grow(_label("", "caption"))
        self.replay_apps_found.set_selectable(True)
        self.replay_apps_found.set_valign(Gtk.Align.CENTER)
        playing = self._line(replay, self.replay_apps_found, _button("Show apps playing sound", self._list_app_audio))
        self._replay_apps_rows = [self.replay_apps.get_parent(), playing]
        self.replay_tracks.connect("notify::selected", lambda *_: self._replay_tracks_changed())
        self.replay_sound = Gtk.Switch()
        self._row(replay, "Clip sound", self.replay_sound, "A short chirp when a clip is saved; a low tone if it fails.")
        self.replay_autostart = Gtk.Switch()
        self._row(replay, "Start with Jarvis", self.replay_autostart, "Turn replay on whenever Jarvis starts.")

        capture = self._group(body, "Source and files")
        self.monitor = Gtk.DropDown.new_from_strings(["No displays discovered"])
        self._row(capture, "Display", self.monitor, "Recording and replay capture this display.")
        self.color_mode = Gtk.DropDown.new_from_strings(["Automatic", "Preserve HDR (10-bit)", "SDR"])
        self._row(capture, "Color", self.color_mode, "Automatic follows the display's HDR setting.",
                  "Automatic detects the display's HDR preset when capture starts. Choose Preserve HDR for "
                  "fullscreen HDR passthrough or older Hyprland versions; SDR needs display HDR off, as Jarvis "
                  "doesn't tone map. Restart capture after changing display HDR. HDR clips need an HDR-aware player "
                  "and display; the built-in preview may show them wrongly, so use Open in your player.")
        self.output_dir = self._entry("~/Videos/Jarvis")
        self._row(capture, "Save to", self.output_dir)
        self.game_folders = Gtk.Switch()
        self.game_folders.set_active(True)
        self._row(capture, "Folder for each game", self.game_folders, "Clips of a game you're playing go in its own folder.",
                  "For example Deadlock/ inside your videos folder. Steam, Heroic, Lutris and Wine games are "
                  "recognized; anything else stays in the main folder.")
        self.system_audio = Gtk.Switch()
        self._row(capture, "Recording: system sound", self.system_audio, "Game and app sound in recordings.")
        self.record_mic = Gtk.Switch()
        self._row(capture, "Recording: microphone", self.record_mic, "Your voice in recordings.")
        self.record_device = Gtk.DropDown.new_from_strings(["Default audio source"])
        self._row(capture, "Recording sound source", self.record_device, "Recordings take one source.",
                  "To record game sound and your microphone together, create a PipeWire source that mixes them, "
                  "turn on the combined source under Advanced, then pick it here.")
        self.system_audio.connect("notify::active", lambda control, _pspec: self._record_audio_changed(control))
        self.record_mic.connect("notify::active", lambda control, _pspec: self._record_audio_changed(control))
        self.sound_feedback = Gtk.Switch()
        self._row(capture, "Recording sounds", self.sound_feedback, "Short tones on start, save and error.")

        quality = self._group(body, "Picture and size", "Applies to recordings and replay, encoded on the GPU of the "
                              "chosen display and tested there first.")
        self.legacy_banner = _box(8)
        self.legacy_text = _label("", "caption")
        self.legacy_banner.append(self.legacy_text)
        use_recommended = _button("Use the Recommended preset", lambda: self.capture_preset.set_selected(self.RECOMMENDED))
        use_recommended.set_halign(Gtk.Align.START)
        self.legacy_banner.append(use_recommended)
        self._line(quality, self.legacy_banner, vertical=True)
        self.legacy_banner.get_parent().add_css_class("callout-row")
        self.legacy_banner.get_parent().set_visible(False)
        self.capture_preset = Gtk.DropDown.new_from_strings([preset[0] for preset in self.PRESETS] + ["Custom"])
        self._row(quality, "Preset", self.capture_preset, "",
                  "Recommended suits most clips. High quality keeps the most detail in larger files. High frame "
                  "rate is for fast games on high-refresh displays. Small files and Compatible are for sharing. "
                  "Custom opens the exact settings.")
        self.format_detail = _label("Testing which formats this display's GPU can encode…", "caption")
        self._line(quality, self.format_detail)
        self.replay_same = Gtk.Switch()
        self.replay_same.set_active(True)
        self._row(quality, "Replay uses the same settings", self.replay_same, "Turn off to give replay its own, lighter settings.",
                  "For example 2K 120 fps recordings with a 1080p replay.")
        self.video_profile = Gtk.DropDown.new_from_strings(["Recording", "Replay"])
        self._row(quality, "Settings for", self.video_profile, "Which one the preset and exact settings change.")
        self.video_profile.get_parent().set_visible(False)
        self._profile, self._profiles = "recording", {}
        self._codec_options: list[str] = []
        self._formats: dict = {}
        self._showing = False
        self.capture_advanced = Gtk.Expander()
        self.capture_advanced.set_label_widget(_label("Format, resolution, frame rate and bitrate", "row-title"))
        tuning = _box(0)
        tuning.add_css_class("subgroup")
        tuning.set_margin_top(8)
        self.capture_advanced.set_child(tuning)
        self._line(quality, self.capture_advanced, vertical=True)
        self.codec = Gtk.DropDown.new_from_strings(["AV1"])
        self._row(tuning, "Format", self.codec, "AV1 makes the smallest files for the quality.",
                  "AV1 is the default when the GPU encodes it. AV1 10-bit: no banding in dark scenes and gradients, "
                  "slightly larger files; older phones and some editors can't play it. HEVC: for GPUs without AV1. "
                  "H.264: plays everywhere, largest files.")
        self.resolution = Gtk.DropDown.new_from_strings(["Display's own"] + self.RESOLUTION_LABELS[1:])
        self._row(tuning, "Resolution", self.resolution, "The largest size; the picture is never stretched up.",
                  "A size above the display's records the display's own size. Smaller sizes make smaller files.")
        self.fps = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 240, 1)
        self.fps.set_digits(0)
        self.fps.set_size_request(260, -1)
        for value in (30, 60, 120, 144, 240):
            self.fps.add_mark(value, Gtk.PositionType.BOTTOM, str(value))
        self._row(tuning, "Frames per second", self.fps, "60 for smooth motion; 120+ for fast games.",
                  "30 makes small files. Higher rates need more encoder work and larger files, and the game has to "
                  "reach that rate too.")
        self.rate_control = Gtk.DropDown.new_from_strings(["Consistent quality · variable size", "Fixed bitrate · predictable size"])
        self.rate_control.set_selected(1)
        self._row(tuning, "Size control", self.rate_control)
        self.quality_preset = Gtk.DropDown.new_from_strings(self.QUALITY_NAMES)
        self._row(tuning, "Quality", self.quality_preset, "Used with consistent quality.")
        self.bitrate_auto = Gtk.Switch()
        self.bitrate_auto.set_active(True)
        self._row(tuning, "Match resolution and frame rate", self.bitrate_auto, "The bitrate follows the picture.",
                  "For example about 17 Mbps for 2K at 60 fps in AV1. Turn off to set your own.")
        self.bitrate = self._spin(1, 500, 1)
        self._row(tuning, "Bitrate (Mbps)", self.bitrate, "Used with a fixed bitrate.",
                  "About 9 Mbps suits 1080p at 60 fps; 2K at 120 fps or 4K needs 35–75 Mbps.")
        self.storage_hint = _label("", "caption")
        self.format_tested = _label("", "caption")
        test_again = _button("Test formats again", lambda: self._probe_formats(force=True))
        test_again.set_halign(Gtk.Align.START)
        self._line(tuning, self.storage_hint, self.format_tested, test_again, vertical=True)

        _, advanced = self._advanced(body, "Advanced", "Replay storage, combined audio sources and legacy tuning.")
        self.replay_storage = Gtk.DropDown.new_from_strings(["RAM", "Disk (continuous writes)"])
        self._row(advanced, "Replay storage", self.replay_storage, "RAM writes video only when you save.",
                  "Disk writes unsaved screen and sound continuously, about 1–10 GB an hour depending on bitrate, "
                  "and can survive a crash until cleanup on the next start. Disk needs the Rust controller and a "
                  "real disk; tmpfs is refused.")
        self.replay_max_mb = self._spin(64, 16384, 64)
        self._row(advanced, "Disk buffer budget (MiB)", self.replay_max_mb, "Disk only: replay stops at this size.",
                  "Checked every 250 ms. At least 512 MiB, because gpu-screen-recorder writes 256 MiB buffer files. "
                  "RAM replay is not capped on Linux.")
        self.replay_storage.connect("notify::selected", lambda *_: self._replay_storage_changed())
        self.replay_buffer_dir = self._entry("Default private app-data folder")
        self._row(advanced, "Disk buffer folder", self.replay_buffer_dir, "Optional. Unsaved buffers are deleted on stop.",
                  "Stale buffers are deleted on the next start.")
        self.combined_audio = Gtk.Switch()
        self._row(advanced, "Combined audio source", self.combined_audio, "Record game sound and microphone together.",
                  "Turn on only after creating a PipeWire source that mixes your microphone and system sound, then "
                  "pick it as the recording sound source.")
        self.combined_audio.connect("notify::active", lambda control, _pspec: self._combined_audio_changed())
        self.capture_configured = Gtk.CheckButton(label="Recorder setup is complete")
        self._line(advanced, self.capture_configured, _label("Starting a recording here completes setup; until then, "
                   "voice commands don't start recordings. A first capture can still show a permission prompt "
                   "from your desktop.", "caption"), vertical=True)
        self.quality = self._spin(0, 51, 1)
        self._row(advanced, "H.264 quality (legacy CPU recorder)", self.quality,
                  "Lower is better quality and larger files.")
        self.capture_preset.connect("notify::selected", lambda *_: self._preset_changed())
        self.replay_same.connect("notify::active", lambda *_: self._same_changed())
        self.video_profile.connect("notify::selected", lambda *_: self._profile_changed())
        self.monitor.connect("notify::selected", lambda *_: self._monitor_changed())
        for control in (self.codec, self.rate_control, self.quality_preset, self.resolution):
            control.connect("notify::selected", lambda *_: self._encoding_changed())
        self.bitrate.connect("value-changed", lambda *_: self._encoding_changed())
        self.bitrate_auto.connect("notify::active", lambda *_: self._encoding_changed())
        self.fps.connect("value-changed", lambda *_: self._encoding_changed())
        self.replay_seconds.connect("value-changed", lambda *_: self._encoding_changed())

    def _clips_page(self) -> None:
        from .clips_ui import ClipsPage
        body = self._page("clips", "Clips", "Watch, trim and join your replays and recordings. Edits are saved "
                          "as new files; originals never change.")
        self.clips_page = ClipsPage(self, body, self._card, _label, _button, _box, self._notice)
        self.stack.connect("notify::visible-child-name", lambda *_: self._clips_page_shown())

    def _clips_page_shown(self) -> None:
        if self.stack.get_visible_child_name() == "clips":
            self._refresh_clips()
        else:
            self.clips_page.close()   # leaving the page releases the video and its pipeline

    def _refresh_clips(self) -> None:
        folder = Path(str(self.config.get("recording", {}).get("output_dir", "~/Videos/Jarvis"))).expanduser()
        self._work(lambda: clips(folder), lambda found: self.clips_page.render(folder, found if isinstance(found, list) else []))

    def _general_page(self) -> None:
        body = self._page("general", "General", "Colors, privacy, startup and your settings file.")
        look = self._group(body, "Colors", "Three color combinations from Sanzo Wada's A Dictionary of Color "
                           "Combinations (1933). The choice applies at once and is kept for this window.")
        palettes_row = Gtk.FlowBox()
        palettes_row.set_selection_mode(Gtk.SelectionMode.NONE)
        palettes_row.set_max_children_per_line(3)
        palettes_row.set_homogeneous(True)
        palettes_row.set_column_spacing(12)
        palettes_row.set_row_spacing(12)
        palettes_row.set_hexpand(True)
        self._palette_buttons: dict[str, Any] = {}
        first = None
        for key, preset in palettes.PRESETS.items():
            button = Gtk.ToggleButton()
            button.add_css_class("swatch-card")
            content = _box(8)
            strip = Gtk.DrawingArea()
            strip.set_content_height(40)
            strip.set_hexpand(True)
            strip.set_overflow(Gtk.Overflow.HIDDEN)
            strip.add_css_class("swatch")
            strip.set_draw_func(self._draw_swatches, list(preset["swatches"].values()))
            content.append(strip)
            content.append(_label(preset["name"], "row-title"))
            content.append(_label(f"No. {preset['number']} · {'dark' if preset['dark'] else 'light'}", "caption"))
            button.set_child(content)
            button.set_tooltip_text(", ".join(preset["swatches"]))
            button.update_property([Gtk.AccessibleProperty.LABEL],
                                   [f"{preset['name']}, combination {preset['number']}"])
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect("toggled", lambda control, name=key: self._choose_palette(name, control))
            palettes_row.append(button)
            self._palette_buttons[key] = button
        self._line(look, palettes_row)
        self._loading = True
        self._palette_buttons[palettes.chosen()].set_active(True)
        self._loading = False

        privacy = self._group(body, "Privacy")
        for title, text in PRIVACY_FACTS:
            fact = _box(2)
            fact.append(_label(title, "row-title"))
            fact.append(_label(text, "caption"))
            self._line(privacy, fact, vertical=True)
        self.privacy_state = _label("", "caption")
        self._line(privacy, self.privacy_state)
        startup = self._group(body, "Startup")
        self.startup = Gtk.Switch()
        self._row(startup, "Start at login", self.startup, "Needs the optional user service; see the README.")
        self.startup.connect("notify::active", lambda *_: None if self._loading else self._save_startup())
        backup = self._group(body, "Your settings", "Save every setting to a file to back them up or move them to "
                             "another computer. Jarvis also keeps a copy before an update or an import changes them; "
                             "a setting a new version can't use is reset on its own, never all of them.")
        self._line(backup, _button("Export…", self._export_settings), _button("Import…", self._import_settings),
                   _button("Open backups", self._open_backups))
        keys = self._group(body, "Keyboard shortcuts", "While this window is open.")
        for shortcut, meaning in (("Ctrl+M", "Mute or unmute the microphone"), ("Ctrl+Space", "Listen now, or cancel"),
                                  ("Ctrl+D", "Start or stop dictation"), ("Ctrl+K", "Commands"),
                                  ("Ctrl+W", "Close this window")):
            self._line(keys, _label(shortcut, "keycap", wrap=False), self._grow(_label(meaning)))
        leave = self._group(body, "Quit")
        reason = self._grow(_label("Stops voice control and instant replay, and closes this window. A recording in "
                                   "progress is saved first.", "caption"))
        reason.set_valign(Gtk.Align.CENTER)
        self._line(leave, reason, _button("Quit Jarvis…", self._confirm_quit, "destructive"))
        body.append(_label("Spoken shell commands are never run: actions are limited to the command list and "
                           "checked desktop integrations.", "caption"))

    @staticmethod
    def _draw_swatches(_area: Any, cr: Any, width: int, height: int, colors: list) -> None:
        for index, color in enumerate(colors):
            r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
            cr.set_source_rgb(r, g, b)
            cr.rectangle(width * index / len(colors), 0, width / len(colors) + 1, height)
            cr.fill()

    def _choose_palette(self, name: str, button: Any) -> None:
        if self._loading or not button.get_active():
            return
        load_theme(name)
        try:
            palettes.choose(name)
        except OSError as error:
            self._notice(f"The colors changed, but couldn't be kept for next time: {error}", True)
            return
        self._notice(f"Colors: {palettes.PRESETS[name]['name']}.")

    # Applying changes ----------------------------------------------------

    def _wire_autosave(self) -> None:
        """Every setting saves on its own. Switches and lists save just after a
        change; numbers once you pause; text when you press Enter or leave the field."""
        groups = {
            "voice": (self.activation, self.wake_phrase, self.wake_aliases, self.language, self.dictation_language,
                      self.stop_phrases, self.dictation_idle, self.keyboard_fallback, self.device, self.accurate,
                      self.speech_model, self.noise_suppression, self.adaptive_gate, self.closest_match,
                      self.sensitivity, self.timeout, self.confidence, self.vad_rms),
            "replay": (self.replay_seconds, self.replay_storage, self.replay_max_mb, self.replay_buffer_dir,
                       self.replay_game, self.replay_mic, self.replay_tracks, self.replay_apps, self.replay_sound,
                       self.replay_autostart),
            "recorder": (self.monitor, self.color_mode, self.output_dir, self.system_audio, self.record_mic,
                         self.combined_audio, self.record_device, self.capture_configured, self.sound_feedback,
                         self.game_folders, self.quality),
            "video": (self.replay_same, self.codec, self.resolution, self.fps, self.rate_control,
                      self.quality_preset, self.bitrate_auto, self.bitrate),
            "close_words": (self.close_words,),
        }
        for group, widgets in groups.items():
            for widget in widgets:
                self._autosave_on(widget, group)

    def _autosave_on(self, widget: Any, group: str) -> None:
        def later(delay: int) -> Callable:
            return lambda *_: self._autosave(group, delay)
        if isinstance(widget, Gtk.SpinButton):
            widget.connect("value-changed", later(800))
        elif isinstance(widget, Gtk.Entry):
            widget.connect("activate", later(0))
            focus = Gtk.EventControllerFocus()
            focus.connect("leave", later(0))
            widget.add_controller(focus)
        elif isinstance(widget, Gtk.Scale):
            widget.connect("value-changed", later(800))
        elif isinstance(widget, Gtk.Switch):
            widget.connect("notify::active", later(400))
        elif isinstance(widget, Gtk.DropDown):
            widget.connect("notify::selected", later(400))
        elif isinstance(widget, Gtk.CheckButton):
            widget.connect("toggled", later(400))

    def _autosave(self, group: str, delay: int = 400) -> None:
        if self._loading or self.closed:
            return
        if not self.connected:
            self._notice("Start Jarvis to change settings. Your change wasn't saved.", True)
            return
        self._dirty.add(group)
        if self._autosave_timer:
            GLib.source_remove(self._autosave_timer)
        self._autosave_timer = GLib.timeout_add(max(delay, 50), self._flush_autosave)

    def _pending_patch(self) -> tuple[dict, tuple]:
        groups, self._dirty = self._dirty, set()
        patch: dict = {}
        for group in sorted(groups):
            part = self._patches[group]()
            if part:
                patch = _merge(patch, part)
        return patch, ("close_words",) if "close_words" in patch else ()

    def _flush_autosave(self) -> bool:
        self._autosave_timer = 0
        patch, replace = self._pending_patch()
        if patch and _merge(self.config, patch) != self.config:
            self._save(patch, "Saved.", replace_keys=replace)
        return False

    def _flush_before_close(self) -> None:
        """A change still waiting for its pause is saved before the window goes."""
        if self._autosave_timer:
            GLib.source_remove(self._autosave_timer)
            self._autosave_timer = 0
        if not self.connected:
            return
        self._dirty |= self._unconfirmed_text()   # typed, but not yet confirmed with Enter
        patch, replace = self._pending_patch()
        if not patch or _merge(self.config, patch) == self.config:
            return
        try:
            with self._save_lock:
                current = client.request("config")
                if current.get("ok"):
                    merged = _merge(current.get("config", {}), patch)
                    for key in replace:
                        merged[key] = copy.deepcopy(patch[key])
                    client.save_config_restarting(merged)
        except (OSError, ConnectionError, ValueError):
            pass

    def _unconfirmed_text(self) -> set[str]:
        """Groups whose text fields differ from the saved settings."""
        c = self.config
        def words(entry: Any) -> list[str]:
            return [word.strip() for word in entry.get_text().split(",") if word.strip()]
        try:
            close = [normalize(word) for word in words(self.close_words)]
        except ValueError:
            close = None
        checks = (("voice", self.wake_phrase.get_text().strip().lower(), c.get("activation", {}).get("wake_phrase")),
                  ("voice", words(self.wake_aliases), c.get("activation", {}).get("wake_aliases")),
                  ("voice", words(self.stop_phrases), c.get("dictation", {}).get("stop_phrases")),
                  ("replay", words(self.replay_apps), c.get("replay", {}).get("separate_apps")),
                  ("replay", self.replay_buffer_dir.get_text().strip(), c.get("replay", {}).get("buffer_dir")),
                  ("recorder", self.output_dir.get_text().strip(), c.get("recording", {}).get("output_dir")),
                  ("close_words", close, c.get("close_words")))
        return {group for group, shown, saved in checks if saved is not None and shown is not None and shown != saved}

    def _quietly(self, change: Callable[[], Any]) -> None:
        """Show a value without saving it."""
        loading, self._loading = self._loading, True
        try:
            change()
        finally:
            self._loading = loading

    def _voice_patch(self) -> dict:
        selected = self.device.get_selected()
        device = self._devices[selected].get("id", "") if selected < len(self._devices) else ""
        return {"recognition": {"closest_match": self.closest_match.get_active(),
                                "accurate": self.accurate.get_active(),
                                "speech_model": "light" if self.speech_model.get_selected() == 1 else "small",
                                "noise_suppression": self.noise_suppression.get_active(),
                                "language": "ar_en" if self.language.get_selected() == 1 else "en"},
                "dictation": {"language": ("auto", "ar", "en")[self.dictation_language.get_selected()],
                              "stop_phrases": [p.strip() for p in self.stop_phrases.get_text().split(",") if p.strip()],
                              "idle_seconds": self.dictation_idle.get_value_as_int(),
                              "keyboard_fallback": self.keyboard_fallback.get_active()},
                "audio": {"device": device, "vad_rms": self.vad_rms.get_value(),
                          "adaptive_gate": self.adaptive_gate.get_active()},
                "activation": {"mode": "wake_word" if self.activation.get_selected() == 1 else "push_to_talk",
                               "wake_phrase": self.wake_phrase.get_text().strip().lower(),
                               "wake_aliases": [name.strip() for name in self.wake_aliases.get_text().split(",") if name.strip()],
                               "sensitivity": self.sensitivity.get_value(),
                               "command_timeout": self.timeout.get_value_as_int(),
                               "confidence": self.confidence.get_value()}}

    def _replay_patch(self) -> dict:
        return {"replay": {"seconds": self.replay_seconds.get_value_as_int(), "autostart": self.replay_autostart.get_active(),
                           "game_audio": self.replay_game.get_active(), "microphone": self.replay_mic.get_active(),
                           "sound": self.replay_sound.get_active(),
                           "storage": "disk" if self.replay_storage.get_selected() == 1 else "ram",
                           "max_mb": self.replay_max_mb.get_value_as_int(),
                           "buffer_dir": self.replay_buffer_dir.get_text().strip(),
                           "audio_tracks": "separate" if self.replay_tracks.get_selected() == 1 else "mixed",
                           "separate_apps": [a.strip() for a in self.replay_apps.get_text().split(",") if a.strip()]}}

    def _recorder_patch(self) -> dict | None:
        selected = self.monitor.get_selected()
        monitor = self._monitors[selected]["name"] if selected < len(self._monitors) else ""
        source_index = self.record_device.get_selected()
        source = self._shown_recording_devices[source_index]["id"] if source_index < len(self._shown_recording_devices) else ""
        if (self.system_audio.get_active() or self.record_mic.get_active()) and not source:
            self._notice("Choose a recording sound source, or turn recording sound off. Not saved yet.", True)
            return None
        return {"recording": {"output_dir": self.output_dir.get_text().strip(), "monitor": monitor,
                              "system_audio": self.system_audio.get_active(), "microphone": self.record_mic.get_active(),
                              "configured": self.capture_configured.get_active(),
                              "sound_feedback": self.sound_feedback.get_active(),
                              "game_folders": self.game_folders.get_active(),
                              "combined_audio_source": self.combined_audio.get_active(),
                              "color_mode": ["auto", "hdr", "sdr"][self.color_mode.get_selected()],
                              "audio_device": source, "quality": self.quality.get_value_as_int()}}

    def _video_patch(self) -> dict | None:
        self._profiles[self._profile] = self._read_video()
        same = self.replay_same.get_active()
        video = self._profiles.get("recording") or self._read_video()
        replay_video = video if same else self._profiles.get("replay", video)
        if replay_video["codec"] not in GPU_CODECS:
            replay_video = dict(replay_video, codec=self._preset_codec("best"))
        for codec in {video["codec"], replay_video["codec"]}:
            if codec in GPU_CODECS and self._formats.get("formats") and not self._formats["formats"].get(codec):
                selected = self.monitor.get_selected()
                monitor = self._monitors[selected]["name"] if selected < len(self._monitors) else "this display"
                self._notice(f"{self.NAMES[codec]} didn't pass the encoder test on {self._formats.get('gpu')} (the GPU "
                             f"of {monitor}). Choose another format; this one wasn't saved.", True)
                return None
        return {"recording": dict(video), "replay": {"own_video": not same, **replay_video}}

    def _close_words_patch(self) -> dict | None:
        words = [normalize(word) for word in self.close_words.get_text().split(",") if word.strip()]
        if not words:
            self._notice("Enter at least one close word, for example close.", True)
            return None
        return {"close_words": words}

    def _install_actions(self) -> None:
        application = self.get_application()
        for name, callback, accelerators in [
            ("mute", self._toggle_mute, ["<Control>m"]),
            ("listen", self._listen, ["<Control>space"]),
            ("dictate", self._toggle_dictation, ["<Control>d"]),
            ("commands", self._show_all_commands, ["<Control>k"]),
            ("close", self.close, ["<Control>w"]),
            ("export", self._export_settings, []),
            ("import", self._import_settings, []),
            ("backups", self._open_backups, []),
            ("quit", self._confirm_quit, []),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _parameter, cb=callback: cb())
            self.add_action(action)
            if accelerators:
                application.set_accels_for_action(f"win.{name}", accelerators)

    # Messages ------------------------------------------------------------

    def _notice(self, text: str, error: bool = False) -> bool:
        if self.closed or not text:
            return False
        self.notice.set_text(text)
        if error:
            self.toast_bubble.add_css_class("error")
        else:
            self.toast_bubble.remove_css_class("error")
        self.toast.set_reveal_child(True)
        if self._toast_timer:
            GLib.source_remove(self._toast_timer)
            self._toast_timer = 0
        if not error:   # errors stay until dismissed or replaced
            self._toast_timer = GLib.timeout_add(min(9000, 2500 + 45 * len(text)), self._hide_toast)
        return False

    def _hide_toast(self) -> bool:
        if self._toast_timer:
            GLib.source_remove(self._toast_timer)
        self._toast_timer = 0
        self.toast.set_reveal_child(False)
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
        threading.Thread(target=run, name="jarvis-ui-request", daemon=True).start()

    # Service state -------------------------------------------------------

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
        if self.toast_bubble.has_css_class("error"):
            self._hide_toast()   # the connection error no longer applies
        response = result.get("config", {})
        if response.get("ok"):
            self.config = response.get("config", {})
            self._load_settings()
        self._quietly(lambda: self._apply_devices(result.get("devices", {}), result.get("monitors", {}),
                                                  result.get("recording_devices", {})))
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
        self._start_watch()

    def _disconnected(self, error: str) -> None:
        self.connected = False
        self.connection_card.set_visible(True)
        self.connection_detail.set_text(error)
        self.connection_detail.set_visible(bool(error))
        self.voice_badge.set_text("Service offline")
        self._show_state("offline")
        self.voice_message.set_text("Start Jarvis, or try again, to see what it's doing.")
        self.mute_button.set_sensitive(False)
        self.listen_button.set_sensitive(False)
        self.record_start.set_sensitive(False)
        self.record_stop.set_sensitive(False)
        self.test_button.set_sensitive(False)
        self._show_test(False)
        self.record_badge.set_text("Status unavailable")
        self.recorder_status.set_text("Status unavailable")
        self.record_message.set_text("Start Jarvis to see whether it is recording.")
        if self.stack.get_visible_child_name() != "dashboard":   # Home already says so
            self._notice("Can't reach Jarvis. Start it from Home.", True)

    def _start_watch(self) -> None:
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._watch_stop = threading.Event()
        def watch() -> None:
            try:
                client.watch(lambda event: GLib.idle_add(self._set_status, event), self._watch_stop)
                if not self._watch_stop.is_set():
                    GLib.idle_add(self._disconnected, "The connection to Jarvis closed.")
            except Exception as error:
                if not self._watch_stop.is_set():
                    GLib.idle_add(self._disconnected, f"The connection to Jarvis closed: {error}")
        self._watch_thread = threading.Thread(target=watch, name="jarvis-ui-events", daemon=True)
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
        wake = str(self.config.get("activation", {}).get("wake_phrase", "jarvis")).capitalize()
        titles = {"paused": "Microphone muted", "idle": f"Waiting for “{wake}”", "listening": "Listening…",
                  "processing": "Working on it", "dictating": "Dictating", "error": "Voice needs attention"}
        title = titles.get(state, state.capitalize())
        self.voice_badge.set_text(title)
        self._show_state(state)
        message = str(status.get("message", "")) or "Press Listen now to give one command."
        self.voice_message.set_text(message)
        self.voice_message.set_visible(message.casefold() != title.casefold())   # no echo of the title
        self._set_mute(muted)
        self.mute_button.set_sensitive(self.connected)
        onboarded = bool(self.config.get("privacy", {}).get("onboarded"))
        self.onboard_banner.set_visible(not onboarded)
        can_listen = self.connected and onboarded and bool(status.get("voice_available", True))
        self.listen_button.set_sensitive(can_listen and state != "processing")
        self.listen_button.set_label("Cancel" if state == "listening" else "Listen now")
        self.test_button.set_sensitive(self.connected and onboarded)
        if "mic_test" in status and bool(status["mic_test"]) != self._test_active:
            self._show_test(bool(status["mic_test"]))   # the service ends a test after 15 seconds
        engine = str(status.get("engine") or "")
        if engine == "vosk":
            self.engine_label.set_text("Recognizer: Vosk only (the accurate model is off or not installed)")
        elif engine:
            parts = {"vosk": "Vosk", "pocketsphinx": "PocketSphinx (basic)", "whisper-small": "Whisper small",
                     "whisper-tiny": "Whisper tiny", "parakeet": "Parakeet", "moonshine-ar": "Moonshine Arabic"}
            self.engine_label.set_text("Recognizer: " + " + ".join(parts.get(part, part) for part in engine.split("+")))
        if any(model in engine for model in ("whisper", "parakeet")):
            self.install_model_button.set_visible(False)
        if self._test_active:
            value = min(1.0, max(0.0, float(status.get("level", 0))))
            self.level.set_value(value)
            gate = status.get("gate")
            gate_text = f" · speech threshold {float(gate):.1%}" if gate else ""
            self.level_text.set_text(f"Input test active · level {value:.0%}{gate_text}")
        recording = status.get("recording", {})
        rec_state = recording.get("state", "idle")
        rec_title = {"recording": "Recording", "stopping": "Finishing your video…", "error": "Recording needs attention", "idle": "Not recording"}.get(rec_state, rec_state.capitalize())
        self.record_badge.set_text(rec_title)
        self.recorder_status.set_text(rec_title)
        for title in (self.record_badge, self.recorder_status):
            title.remove_css_class("active")
            title.remove_css_class("error")
            if rec_state in ("recording", "error"):
                title.add_css_class("active" if rec_state == "recording" else "error")
        message = str(recording.get("message") or "")
        filename = str(recording.get("file") or "")
        detail = "\n".join(value for value in (message, filename) if value)
        self.record_message.set_text(detail or "Ready to record.")
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
        self.overview_replay.set_text({"on": f"Replay on · keeps the last {_duration(self.config.get('replay', {}).get('seconds', 120))}",
                                       "stopping": "Stopping replay…", "error": "Replay needs attention"}.get(replay_state, "Replay off"))
        self.overview_save.set_sensitive(self.connected and replay_state == "on")
        self.overview_record.set_label("Stop recording" if rec_state == "recording" else "Start recording")
        self.overview_record.set_sensitive(self.connected and rec_state in {"idle", "error", "recording"})
        self.overview_audio.set_text(_audio_summary(self.config, rec_state == "recording", replay_state == "on"))
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
        summary = f"{len(excluded)} phrases aren't in the speech dictionary. Give those apps a voice name made of common English words."
        unspeakable = [str(app.get("name", "")) for app in self._apps
                       if not app.get("name_speakable") and not app.get("voice_aliases")]
        if unspeakable:
            summary += f" For example, give “{unspeakable[0]}” a name that describes it in ordinary words."
        for notice in (self.app_grammar_notice, self.voice_grammar_notice):
            notice.set_text(summary if excluded else "")
            notice.set_visible(bool(excluded))
        self._render_apps()

    def _load_settings(self) -> None:
        loading, self._loading = self._loading, True   # shown, not saved
        try:
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
            self.dictation_idle.set_value(float(dictation.get("idle_seconds", 3)))
            self.keyboard_fallback.set_active(bool(dictation.get("keyboard_fallback", True)))
            self._show_arabic_model()
            self.noise_suppression.set_active(bool(recognition.get("noise_suppression", False)))
            self.adaptive_gate.set_active(bool(self.config.get("audio", {}).get("adaptive_gate", True)))
            self.output_dir.set_text(str(recorder.get("output_dir", "~/Videos/Jarvis")))
            modes = ["auto", "hdr", "sdr"]
            self.color_mode.set_selected(modes.index(recorder.get("color_mode", "auto")))
            self.combined_audio.set_active(bool(recorder.get("combined_audio_source", False)))
            self.system_audio.set_active(bool(recorder.get("system_audio", False)))
            self.record_mic.set_active(bool(recorder.get("microphone", False)))
            self.capture_configured.set_active(bool(recorder.get("configured", False)))
            self.game_folders.set_active(bool(recorder.get("game_folders", True)))
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
            self._populate_record_devices(keep_shown=False)
            self._showing = True  # load every control, then find the matching preset once
            self.fps.set_value(float(recorder.get("fps", 30)))
            self.quality.set_value(float(recorder.get("quality", 23)))
            self.rate_control.set_selected(1 if recorder.get("rate_control") == "bitrate" else 0)
            self.bitrate.set_value(float(recorder.get("bitrate_mbps", 8)))
            self.bitrate_auto.set_active(recorder.get("bitrate_auto", True))
            self.quality_preset.set_selected(QUALITY_PRESETS.index(recorder.get("quality_preset", "very_high")))
            self.resolution.set_selected(self.RESOLUTIONS.index(recorder.get("resolution", "native")))
            self._showing = False
            self._show_codec(recorder.get("codec", "libx264"))
            replay = self.config.get("replay", {})
            self._profile = "recording"
            self._profiles = {"replay": {key: replay[key] for key in REPLAY_VIDEO}
                              if replay.get("own_video") else self._read_video()}
            self._showing = True
            self.video_profile.set_selected(0)
            self.replay_same.set_active(not replay.get("own_video", False))
            self._showing = False
            self.video_profile.get_parent().set_visible(not self.replay_same.get_active())
            self._encoding_changed()
            self.startup.set_active(bool(self.config.get("startup", False)))
            self._show_privacy()
            from .models import describe
            models = describe(self.config)
            self.install_model_button.set_visible(not (models["model_ready"] and models["accurate_model_ready"]))
        finally:
            self._loading = loading

    def _show_privacy(self) -> None:
        onboarded = bool(self.config.get("privacy", {}).get("onboarded", False))
        self.consent.set_active(onboarded)
        self.onboard_button.set_label("Microphone allowed" if onboarded else "Allow and turn on the wake word")
        self.onboard_button.set_sensitive(not onboarded)
        self.privacy_state.set_text("You allowed the microphone. Mute it at any time; it stays closed while muted."
                                    if onboarded else "You haven't allowed the microphone yet. Home shows how.")

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
        self.monitor.set_model(Gtk.StringList.new(labels or ["No displays found; try again in Hyprland"]))
        self.monitor.set_selected(next((i for i, item in enumerate(self._monitors) if item.get("name") == selected_monitor), 0))
        self._probe_formats()

    def _record_audio_changed(self, control: Any) -> None:
        if control.get_active() and not self.combined_audio.get_active():
            other = self.record_mic if control is self.system_audio else self.system_audio
            other.set_active(False)
        self._populate_record_devices()
        if (control.get_active() and not self._loading and self.record_device.get_selected() == 0
                and len(self._shown_recording_devices) > 1):
            self.record_device.set_selected(1)   # sound on needs a source: start with the first that fits

    def _combined_audio_changed(self) -> None:
        if not self.combined_audio.get_active() and self.system_audio.get_active() and self.record_mic.get_active():
            self.record_mic.set_active(False)
        self._populate_record_devices()

    def _populate_record_devices(self, keep_shown: bool = True) -> None:
        if not hasattr(self, "record_device"):
            return
        index = self.record_device.get_selected()
        shown = (self._shown_recording_devices[index]["id"] if keep_shown and index < len(self._shown_recording_devices)
                 else "")
        is_monitor = self.system_audio.get_active()
        self._shown_recording_devices = [{"id": "", "name": "Choose a sound source"}]
        self._shown_recording_devices.extend(source for source in self._recording_devices if self.combined_audio.get_active() or bool(source.get("monitor")) == is_monitor)
        selected = shown or self.config.get("recording", {}).get("audio_device", "")
        if selected and not any(source.get("id") == selected for source in self._shown_recording_devices):
            if shown:
                selected = self.config.get("recording", {}).get("audio_device", "")
            if selected and not any(source.get("id") == selected for source in self._shown_recording_devices):
                self._shown_recording_devices.append({"id": selected, "name": selected + " (saved source)"})
        self.record_device.set_model(Gtk.StringList.new([str(source.get("name", source.get("id", "Unknown source"))) for source in self._shown_recording_devices]))
        self.record_device.set_selected(next((i for i, source in enumerate(self._shown_recording_devices) if source.get("id") == selected), 0))

    def _refresh_devices(self) -> None:
        def apply(value: Any) -> None:
            if isinstance(value, tuple):
                self._quietly(lambda: self._apply_devices(*value))
                self._notice("Microphones and displays updated.")
            else:
                self._notice(str(value.get("error")), True)
        self._work(lambda: (client.request("devices"), client.request("monitors"), client.request("recording_devices")), apply)

    def _clear(self, container: Any) -> None:
        while child := container.get_first_child():
            container.remove(child)

    # Commands and apps ---------------------------------------------------

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
        self.command_count.set_text(f"{start + 1 if matches else 0}–{min(start + 30, len(matches))} of {len(matches)}")
        self.command_previous.set_sensitive(self._command_page > 0)
        self.command_next.set_sensitive(start + 30 < len(matches))
        if not matches:
            self.command_list.append(_label("No matching commands." if self._command_data else "Start Jarvis to load commands for every installed app.", "caption"))

    def _render_apps(self) -> None:
        self._clear(self.app_list)
        needle = self.app_search.get_text().casefold().strip()
        matching = [app for app in self._apps if needle in f"{app.get('name', '')} {app.get('id', '')}".casefold()]
        # The same app installed twice (native + Flatpak) shows once: the entry
        # voice commands open. A voice name can still pick the other copy.
        visible = [app for app in matching if not app.get("duplicate_of")]
        hidden = len(matching) - len(visible)
        summary = f"{len(visible)} apps" + (f" · {hidden} duplicate entr{'y' if hidden == 1 else 'ies'} hidden "
                                            "(such as a Flatpak copy; Jarvis opens the native one)" if hidden else "")
        self.app_count.set_text(summary if self._apps else "Start Jarvis to find installed apps.")
        # A widget for every system desktop entry is unnecessary until searched.
        for app in visible[:30]:
            self.app_list.append(self._app_row(app))
        if len(visible) > 30:
            more = _label("Showing the first 30. Search to find the rest.", "caption")
            more.add_css_class("row")
            self.app_list.append(more)
        self.app_list.set_visible(bool(visible))
        # Rebuild the voice-name app picker only when the app list changes, so
        # searching the list above does not reset a selection the user made.
        ids = tuple(app["id"] for app in self._apps)
        if ids != self._alias_model_ids:
            self._alias_model_ids = ids
            self.alias_app.set_model(Gtk.StringList.new([f"{app.get('name', app['id'])} ({app['id']})" for app in self._apps] or ["No apps found"]))

    def _app_row(self, app: dict) -> Any:
        """An app with what to say, and "Add voice name": click, type a name, press Enter."""
        app_id = str(app.get("id", ""))
        name = str(app.get("name", app_id or "Application"))
        row = _box(8)
        row.add_css_class("row")
        top = _box(12, True)
        info = self._grow(_box(2))
        title = _label(name, "row-title")
        title.set_tooltip_text(app_id)
        info.append(title)
        phrases = app.get("voice_commands", [])
        if phrases:
            info.append(_label("Say " + " or ".join(f"“{phrase}”" for phrase in phrases), "caption"))
        if app.get("name_speakable") is False or app.get("speakable") is False:
            aliases = app.get("voice_aliases", [])
            info.append(_label(f"Its own name isn't in the speech dictionary; say “open {aliases[0]}”." if aliases
                               else "Its name isn't in the speech dictionary. Give it a voice name of common words.",
                               "caption warning"))
        names = sorted(alias for alias, target in self.config.get("aliases", {}).items() if target == app_id)
        if names:
            info.append(_label("Your names: " + ", ".join(f"“{n}”" for n in names), "caption"))
        top.append(info)
        add = Gtk.Button(label="Add voice name")
        add.add_css_class("quiet")
        add.set_valign(Gtk.Align.CENTER)
        add.update_property([Gtk.AccessibleProperty.LABEL], [f"Add a voice name for {name}"])
        top.append(add)
        row.append(top)
        fields = _box(8, True)
        entry = self._entry("Name to say, e.g. " + name.lower())
        entry.update_property([Gtk.AccessibleProperty.LABEL], [f"Voice name for {name}"])
        prefix = Gtk.Entry()
        prefix.set_width_chars(10)
        prefix.set_placeholder_text("Launch word")
        prefix.set_text("open")
        prefix.update_property([Gtk.AccessibleProperty.LABEL], ["Launch word; clear it to say only the name"])
        save = _button("Save", lambda: self._save_voice_name(app_id, entry, prefix), "primary")
        entry.connect("activate", lambda _entry: self._save_voice_name(app_id, entry, prefix))
        fields.append(prefix)
        fields.append(entry)
        fields.append(save)
        fields.set_visible(False)
        row.append(fields)
        def reveal(_button: Any) -> None:
            add.set_visible(False)
            fields.set_visible(True)
            entry.grab_focus()
        add.connect("clicked", reveal)
        return row

    def _render_added_apps(self) -> None:
        from . import app_catalog
        self._clear(self.added_apps)
        self._clear(self.appimage_hints)
        for item in app_catalog.added():
            row = _box(8, True)
            row.add_css_class("row")
            row.append(self._grow(_label(f"{item['name']} · added by Jarvis", "caption")))
            row.append(_button("Remove", lambda identity=item["id"]: self._remove_added_app(identity)))
            self.added_apps.append(row)
        known = " ".join(str(app.get("exec", "")) for app in self._apps)
        for path in app_catalog.program_candidates(known):
            row = _box(8, True)
            row.add_css_class("row")
            row.append(self._grow(_label(f"Found {path.name} in {path.parent}", "caption")))
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
        self._notice("Removed the program Jarvis added. The program file itself was not deleted.")
        self.refresh()

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
        names = {app.get("id"): app.get("name") for app in self._apps}
        for name, app_id in sorted(self.config.get("aliases", {}).items()):
            row = _box(8, True)
            row.add_css_class("row")
            prefix = Gtk.Entry()
            prefix.set_placeholder_text("Launch word")
            prefix.set_text(self.config.get("app_prefixes", {}).get(name, "open"))
            prefix.set_width_chars(10)
            prefix.set_valign(Gtk.Align.CENTER)
            prefix.update_property([Gtk.AccessibleProperty.LABEL], [f"Launch word for {name}; clear it to say only the name"])
            def save_word(*_args: Any, phrase: str = name, control: Any = prefix) -> None:
                word = normalize(control.get_text())
                if word != self.config.get("app_prefixes", {}).get(phrase, "open") and self.connected:
                    self._save({"app_prefixes": {phrase: word}}, "Launch word saved.")
            prefix.connect("activate", save_word)
            focus = Gtk.EventControllerFocus()
            focus.connect("leave", save_word)
            prefix.add_controller(focus)
            row.append(prefix)
            label = self._grow(_label(f"“{name}” opens {names.get(app_id) or app_id}"))
            label.set_valign(Gtk.Align.CENTER)
            row.append(label)
            remove = _button("Remove", lambda phrase=name: self._remove_mapping("aliases", phrase))
            remove.set_valign(Gtk.Align.CENTER)
            remove.update_property([Gtk.AccessibleProperty.LABEL], [f"Remove the voice name {name}"])
            row.append(remove)
            self.alias_list.append(row)
        if not self.config.get("aliases"):
            empty = _label("No voice names yet. Use Add voice name on an app above, or the form below.", "caption")
            empty.add_css_class("row")
            self.alias_list.append(empty)

    def _render_phrases(self) -> None:
        self._clear(self.phrase_list)
        for phrase, canonical in sorted(self.config.get("commands", {}).items()):
            row = _box(8, True)
            row.add_css_class("row")
            text = self._grow(_label(f"“{phrase}” runs “{canonical}”"))
            text.set_valign(Gtk.Align.CENTER)
            row.append(text)
            remove = _button("Remove", lambda item=phrase: self._remove_mapping("commands", item))
            remove.update_property([Gtk.AccessibleProperty.LABEL], [f"Remove the shortcut {phrase}"])
            row.append(remove)
            self.phrase_list.append(row)
        if not self.config.get("commands"):
            empty = _label("No shortcuts yet.", "caption")
            empty.add_css_class("row")
            self.phrase_list.append(empty)

    # Saving --------------------------------------------------------------

    def _save(self, patch: dict, success: str = "Saved.", replace_keys: tuple = ()) -> None:
        if not self.connected:
            self._notice("Start Jarvis to change settings.", True)
            return
        def save() -> dict:
            with self._save_lock:
                current = client.request("config")
                if not current.get("ok"):
                    return current
                merged = _merge(current.get("config", {}), patch)
                for key in replace_keys:
                    merged[key] = copy.deepcopy(patch[key])
                result = client.save_config_restarting(merged)
                if result.get("ok"):
                    result["config"] = merged
                return result
        def saved(result: dict) -> None:
            if not result.get("ok"):
                self._notice(str(result.get("error", "Settings couldn't be saved.")), True)
                return
            note = client.restart_note(result)
            self.config = result["config"]
            self._quietly(self._show_language)
            self._render_aliases()
            self._render_phrases()
            self._render_apps()
            if patch.get("privacy", {}).get("onboarded"):
                self._quietly(self._show_privacy)
                self._quietly(lambda: self.activation.set_selected(1))
            self._set_status({"ok": True, "status": self.status}, validate_grammar=False)
            self._notice(success + note, bool(result.get("restart_errors")))
        self._work(save, saved)

    def _show_language(self) -> None:
        arabic = self.config.get("recognition", {}).get("language") == "ar_en"
        self.language.set_selected(1 if arabic else 0)
        self.overview_arabic.set_active(arabic)

    def _toggle_arabic(self) -> None:
        """The Home switch: English only, or Arabic and English (also: jarvis language)."""
        from .models import arabic_model_ready
        if not self.overview_arabic.get_active():
            self._save({"recognition": {"language": "en"}}, "Arabic is off. Jarvis understands English only.")
        elif not arabic_model_ready():
            self._quietly(lambda: self.overview_arabic.set_active(False))
            self._notice("The Arabic model isn't installed yet. Download it in Voice → Language.", True)
        else:
            self._save({"recognition": {"language": "ar_en"}},
                       "Arabic is on. Say commands in Arabic or English; dictation types Arabic.")

    def _show_arabic_model(self) -> None:
        from .models import arabic_model_dir, arabic_model_ready
        ready = arabic_model_ready()
        self.arabic_status.set_text(f"The Arabic model is installed at {arabic_model_dir()}." if ready else
                                    "The Arabic model isn't installed. Arabic and English needs it.")
        self.arabic_install.set_visible(not ready)
        self.arabic_file.set_visible(not ready)

    def _download_arabic(self) -> None:
        self.arabic_install.set_sensitive(False)
        self._notice("Downloading the Arabic model (about 118 MB) and checking its checksum…")
        def install() -> dict:
            from .models import install_arabic
            return {"ok": True, "message": f"Arabic model installed at {install_arabic()}. "
                                           "Choose Arabic and English to use it."}
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
                                               "Choose Arabic and English to use it."}
            def done(result: dict) -> None:
                self._show_arabic_model()
                self._notice(str(result.get("message") or result.get("error")), not result.get("ok"))
            self._work(install, done)
        dialog.open(self, None, chosen)

    def _dictation_buttons(self) -> list:
        return [self.dictation_button, self.overview_dictate]

    def _dictation_labels(self) -> list:
        return [self.dictation_state, self.overview_dictation]

    def _held_buttons(self) -> list:
        return [self.dictation_copy, self.overview_copy]

    def _show_all_commands(self) -> None:
        self.show_page("commands")
        self.command_search.grab_focus()

    def _toggle_dictation(self) -> None:
        if not self.config.get("privacy", {}).get("onboarded"):
            self.show_page("dashboard")
            self._notice("Allow the microphone on Home first.", True)
            return
        self._action("dictation", action="toggle")

    def _copy_held(self) -> None:
        held = str(self.status.get("dictation", {}).get("held", ""))
        if held:
            self.get_clipboard().set(held)
            self._action("dictation", action="copied")
            self._notice("Copied the text that wasn't typed.")

    def _install_model(self) -> None:
        self.install_model_button.set_sensitive(False)
        self._notice("Downloading the voice models (about 790 MB) and checking their checksums. This can take a few minutes…")
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
                                            "No app is playing sound right now (or gpu-screen-recorder isn't "
                                            "installed). Start the podcast or video, then try again.")
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

    # (name, format, resolution, fps, size control, quality, Mbps or "auto").
    # Formats: "best" is AV1 when the GPU encodes it (else HEVC, H.264);
    # "high" AV1 10-bit. "auto" bitrate follows resolution and frame rate.
    PRESETS = [("Recommended", "best", "native", 60, "bitrate", "very_high", "auto"),
               ("High quality", "high", "native", 60, "quality", "ultra", "auto"),
               ("High frame rate · 120 fps", "best", "native", 120, "bitrate", "very_high", "auto"),
               ("Small files · 1080p", "best", "1080", 30, "bitrate", "very_high", "auto"),
               ("Compatible · H.264", "h264", "native", 60, "bitrate", "very_high", "auto")]
    RECOMMENDED, HIGH, HIGH_FPS, SMALL, COMPATIBLE, CUSTOM = range(6)
    RESOLUTIONS = ["native", "2160", "1440", "1080", "720"]
    RESOLUTION_LABELS = ["Display's own", "4K · 3840×2160", "2K · 2560×1440", "1080p · 1920×1080", "720p · 1280×720"]
    QUALITY_NAMES = ["Compact", "Balanced", "Sharp", "Maximum"]

    def _preset_codec(self, kind: str) -> str:
        from .encoder_probe import recommended
        formats = self._formats.get("formats", {})
        if kind == "best":
            return recommended(formats) or "av1"
        if kind == "high":
            return next((c for c in ("av1_10bit", "hevc_10bit") if formats.get(c)), "av1_10bit")
        return kind

    def _settings_key(self, codec, resolution, fps, rate, quality, bitrate) -> tuple:
        """What a preset fixes: quality only counts with consistent quality, bitrate only when fixed."""
        return (codec, resolution, fps, rate, quality if rate == "quality" else None, bitrate if rate == "bitrate" else None)

    def _matching_preset(self) -> int:
        rate = "bitrate" if self.rate_control.get_selected() == 1 else "quality"
        bitrate = "auto" if self.bitrate_auto.get_active() else self.bitrate.get_value_as_int()
        current = self._settings_key(self._chosen_codec(), self.RESOLUTIONS[self.resolution.get_selected()],
                                     int(round(self.fps.get_value())), rate,
                                     QUALITY_PRESETS[self.quality_preset.get_selected()], bitrate)
        for index, (_, kind, resolution, fps, rate, quality, bitrate) in enumerate(self.PRESETS):
            if current == self._settings_key(self._preset_codec(kind), resolution, fps, rate, quality, bitrate):
                return index
        return self.CUSTOM

    def _sync_preset(self) -> None:
        self._showing = True  # reflect the controls without applying a preset
        self.capture_preset.set_selected(self._matching_preset())
        self._showing = False

    def _apply_preset(self, index: int) -> None:
        _, kind, resolution, fps, rate, quality, bitrate = self.PRESETS[index]
        codec = self._preset_codec(kind)
        self._showing = True
        if codec in self._codec_options:
            self.codec.set_selected(self._codec_options.index(codec))
        self.resolution.set_selected(self.RESOLUTIONS.index(resolution))
        self.fps.set_value(fps)
        self.rate_control.set_selected(1 if rate == "bitrate" else 0)
        self.quality_preset.set_selected(QUALITY_PRESETS.index(quality))
        self.bitrate_auto.set_active(bitrate == "auto")
        if bitrate != "auto":
            self.bitrate.set_value(bitrate)
        self._showing = False
        self._encoding_changed()

    def _preset_changed(self) -> None:
        if self._showing:
            return
        index = self.capture_preset.get_selected()
        if index == self.CUSTOM:
            self.capture_advanced.set_expanded(True)
        else:
            self._apply_preset(index)

    def _show_resolutions(self) -> None:
        """Name the selected display's own size (2K, 4K…) in the resolution list."""
        selected = self.monitor.get_selected()
        item = self._monitors[selected] if selected < len(self._monitors) else {}
        width, height = item.get("width"), item.get("height")
        own = "Display's own"
        if isinstance(width, int) and isinstance(height, int):
            names = {2160: "4K", 1440: "2K", 1080: "1080p", 720: "720p"}
            own += f" · {names[height]} ({width}×{height})" if height in names else f" · {width}×{height}"
        keep = self.resolution.get_selected()
        self._showing = True
        self.resolution.set_model(Gtk.StringList.new([own] + self.RESOLUTION_LABELS[1:]))
        self.resolution.set_selected(keep if keep < len(self.RESOLUTIONS) else 0)
        self._showing = False

    def _show_codec(self, codec: str) -> None:
        """Put a saved codec into the picker. Legacy encoders stay selectable only
        for configurations that already use them."""
        self._saved_codec = codec
        self._codec_options = list(self.LABELS) + ([codec] if codec in self.LEGACY else [])
        self._showing = True
        self.codec.set_model(Gtk.StringList.new([self.LABELS.get(c) or self.LEGACY[c] for c in self._codec_options]))
        self.codec.set_selected(self._codec_options.index(codec) if codec in self._codec_options else 0)
        self._showing = False
        self.legacy_banner.set_visible(codec in self.LEGACY)
        self.legacy_banner.get_parent().set_visible(codec in self.LEGACY)
        self.legacy_text.set_text(f"This recorder uses {self.LEGACY.get(codec, '')}. It keeps working, but the GPU "
                                  "formats record games with a small fraction of the CPU.")
        if codec in self.LEGACY and not self.config.get("recording", {}).get("configured"):
            self._apply_preset(self.RECOMMENDED)  # a new setup starts on AV1 where the GPU has it
        else:
            self._sync_preset()

    def _read_video(self) -> dict:
        """The video settings the controls show, keyed like the config."""
        return {"codec": self._chosen_codec(), "resolution": self.RESOLUTIONS[self.resolution.get_selected()],
                "fps": int(round(self.fps.get_value())),
                "rate_control": "bitrate" if self.rate_control.get_selected() == 1 else "quality",
                "quality_preset": QUALITY_PRESETS[self.quality_preset.get_selected()],
                "bitrate_mbps": self.bitrate.get_value_as_int(), "bitrate_auto": self.bitrate_auto.get_active()}

    def _write_video(self, video: dict) -> None:
        codec = video["codec"]
        if self._profile == "replay" and codec not in GPU_CODECS:
            codec = self._preset_codec("best")  # replay always records on the GPU
        self._showing = True
        self.resolution.set_selected(self.RESOLUTIONS.index(video["resolution"]))
        self.fps.set_value(video["fps"])
        self.rate_control.set_selected(1 if video["rate_control"] == "bitrate" else 0)
        self.quality_preset.set_selected(QUALITY_PRESETS.index(video["quality_preset"]))
        self.bitrate.set_value(video["bitrate_mbps"])
        self.bitrate_auto.set_active(video.get("bitrate_auto", True))
        self._showing = False
        self._show_codec(codec)

    def _profile_changed(self) -> None:
        """Switch which settings (recording or replay) the controls edit."""
        new = "replay" if self.video_profile.get_selected() == 1 else "recording"
        if self._showing or new == self._profile:
            return
        def switch() -> None:
            self._profiles[self._profile] = self._read_video()
            self._profile = new
            self._write_video(self._profiles[new])
            self._encoding_changed()
        self._quietly(switch)   # showing other settings changes nothing

    def _same_changed(self) -> None:
        if self._showing:
            return
        same = self.replay_same.get_active()
        self.video_profile.get_parent().set_visible(not same)
        if same:
            self._quietly(lambda: self.video_profile.set_selected(0))  # back to editing the shared settings
        elif self._profile == "recording":
            self._profiles["replay"] = self._read_video()  # replay starts as a copy
        self._encoding_changed()

    def _chosen_codec(self) -> str:
        index = self.codec.get_selected()
        return self._codec_options[index] if index < len(self._codec_options) else "av1"

    def _monitor_changed(self) -> None:
        self._probe_by_user = not self._loading
        self._probe_formats()

    def _probe_formats(self, force: bool = False) -> None:
        selected = self.monitor.get_selected()
        monitor = self._monitors[selected]["name"] if selected < len(self._monitors) else ""
        self._show_resolutions()
        by_user, self._probe_by_user = self._probe_by_user, False
        def probe() -> dict:
            from . import encoder_probe
            if force:
                encoder_probe.probe_node.cache_clear()
            return encoder_probe.formats_for_monitor(monitor)
        def show(result: dict) -> None:
            if isinstance(result, dict) and "formats" in result:
                preset = self.capture_preset.get_selected()
                self._formats = result
                self._show_codec(self._chosen_codec() if self._codec_options else getattr(self, "_saved_codec", "libx264"))
                if preset != self.CUSTOM and self._matching_preset() != preset:
                    self._apply_preset(preset)  # e.g. Recommended is HEVC on a GPU without AV1
            self._encoding_changed()
        def done(result: dict) -> None:
            before = self._read_video()
            self._quietly(lambda: show(result))
            if by_user and self._read_video() != before:
                self._autosave("video")   # the new display's GPU needs another format
        self._work(probe, done)

    def _encoding_changed(self) -> None:
        if not self._showing:
            self._sync_preset()
        formats = self._formats.get("formats", {})
        codec = self._chosen_codec()
        names = self.NAMES
        if formats:
            works = ", ".join(names[c] for c in ("av1", "hevc", "h264") if formats.get(c)) or "none"
            ten = ", ".join(n for c, n in (("av1_10bit", "AV1"), ("hevc_10bit", "HEVC")) if formats.get(c)) or "none"
            self.format_tested.set_text(f"Tested on {self._formats.get('gpu')}: {works} · 10-bit (SDR and HDR): {ten}.")
        budget = self.rate_control.get_selected() == 1
        gpu = codec in GPU_CODECS
        size = self.resolution.get_model().get_string(self.resolution.get_selected()) or "Display's own"
        fps = int(round(self.fps.get_value()))
        if self.bitrate_auto.get_active():
            selected = self.monitor.get_selected()
            item = self._monitors[selected] if selected < len(self._monitors) else {}
            width, height = item.get("width"), item.get("height")
            automatic = auto_bitrate({"codec": codec, "fps": fps,
                                      "resolution": self.RESOLUTIONS[self.resolution.get_selected()]},
                                     width if isinstance(width, int) else None, height if isinstance(height, int) else None)
            if self.bitrate.get_value_as_int() != automatic:
                showing, self._showing = self._showing, True
                self.bitrate.set_value(automatic)
                self._showing = showing
        amount = (f"fixed {self.bitrate.get_value_as_int()} Mbps" + (" (matches resolution and frame rate)"
                  if self.bitrate_auto.get_active() else "") if budget
                  else f"{self.QUALITY_NAMES[self.quality_preset.get_selected()]} quality")
        if codec in self.LEGACY:
            detail = f"{self.LEGACY[codec]} · {fps} fps."
        elif formats and not formats.get(codec):
            detail = f"{names.get(codec, codec)} didn't pass the encoder test on this display's GPU; choose another preset or format."
        elif shutil.which("ffmpeg") is None and not formats:
            detail = "FFmpeg is missing, so formats can't be tested. Install ffmpeg."
        else:
            who = ("Recording and replay record" if self.replay_same.get_active()
                   else "Replay records" if self._profile == "replay" else "Recording records")
            detail = f"{who} {names.get(codec, codec)} · {size.split(' (')[0]} · {fps} fps · {amount}."
            if self.capture_preset.get_selected() == self.HIGH:
                detail += " Files are larger, most of all in fast motion, and a replay keeps fewer seconds in its memory budget."
            elif fps > 60:
                detail += " The game must reach this frame rate, and fast motion makes larger files."
        self.format_detail.set_text(detail)
        self.bitrate_auto.set_sensitive(budget)
        self.bitrate.set_sensitive(budget and not self.bitrate_auto.get_active())
        self.quality_preset.set_sensitive(not budget and gpu)
        self.resolution.set_sensitive(gpu)
        self.quality.set_sensitive(not budget and not gpu)
        if budget:
            cfg = {"bitrate_mbps": self.bitrate.get_value_as_int()}
            self.storage_hint.set_text(f"About {storage_estimate(cfg):.0f} MB a minute of video, "
                f"{storage_estimate(cfg, 3600) / 1000:.2f} GB an hour. "
                f"Replay video buffer: about {storage_estimate(cfg, self.replay_seconds.get_value_as_int()):.0f} MB. "
                "Audio and container overhead are extra; these are estimates, not limits.")
        else:
            self.storage_hint.set_text("File size and replay memory vary with the scene. Choose a fixed bitrate for a predictable size.")

    # Actions -------------------------------------------------------------

    def _export_settings(self) -> None:
        dialog = Gtk.FileDialog(title="Export Jarvis settings", initial_name=f"jarvis-settings-{time.strftime('%Y-%m-%d')}.json")
        def chosen(source, result) -> None:
            try:
                path = source.save_finish(result).get_path()
            except GLib.Error:
                return
            from .settings_file import export_settings
            self._work(lambda: {"ok": True, "message": f"Settings saved to {export_settings(path)}."},
                       lambda r: self._notice(str(r.get("message") or r.get("error")), not r.get("ok")))
        dialog.save(self, None, chosen)

    def _import_settings(self) -> None:
        dialog = Gtk.FileDialog(title="Import Jarvis settings")
        def chosen(source, result) -> None:
            try:
                path = source.open_finish(result).get_path()
            except GLib.Error:
                return
            from .settings_file import import_settings
            self._notice("Importing settings…")
            def imported(result: dict) -> None:
                self._notice(str(result.get("message") or result.get("error")), not result.get("ok"))
                if result.get("ok"):
                    self.refresh()
            self._work(lambda: {"ok": True, "message": import_settings(path)}, imported)
        dialog.open(self, None, chosen)

    def _open_backups(self) -> None:
        from .config import backups_dir
        folder = backups_dir()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)

    def _overview_record(self) -> None:
        state = self.status.get("recording", {}).get("state", "idle")
        self._action("record_stop") if state == "recording" else self._start_recording()

    def _start_recording(self) -> None:
        """Start recording. Pressing Start in this window is the setup test, so
        the first one also marks recorder setup complete."""
        if self.config.get("recording", {}).get("configured"):
            self._action("record_start")
            return
        if not self.connected:
            self._notice("Start Jarvis first.", True)
            return
        def start() -> dict:
            current = client.request("config")
            if not current.get("ok"):
                return current
            config = current["config"]
            config["recording"]["configured"] = True
            saved = client.request("save_config", config=config)
            if not saved.get("ok"):
                return saved
            result = client.request("record_start")
            result["config"] = config
            return result
        def done(result: dict) -> None:
            if "config" in result:
                self.config = result["config"]
                self._quietly(lambda: self.capture_configured.set_active(True))
            self._notice(str(result.get("error") or result.get("message") or "Recording started."), not result.get("ok"))
            self.refresh()
        self._work(start, done)

    def _save_startup(self) -> None:
        self._action("set_startup", enabled=self.startup.get_active())

    def _onboard(self) -> None:
        if not self.consent.get_active():
            self._notice("Tick “I understand when Jarvis opens the microphone” first.", True)
            return
        self._save({"privacy": {"onboarded": True}, "activation": {"mode": "wake_word"}},
                   "Microphone allowed. The wake word is on; the status at the top shows when it's ready.")

    def _add_alias(self) -> None:
        phrase = " ".join(self.alias_name.get_text().lower().split())
        index = self.alias_app.get_selected()
        if not phrase or index >= len(self._apps):
            self._notice("Type a name and choose the app it opens.", True)
            return
        self._save({"aliases": {phrase: self._apps[index]["id"]}, "app_prefixes": {phrase: normalize(self.alias_prefix.get_text())}},
                   "Voice name saved.")
        self.alias_name.set_text("")

    def _add_phrase(self) -> None:
        phrase = " ".join(self.new_phrase.get_text().lower().split())
        canonical = " ".join(self.canonical_phrase.get_text().lower().split())
        if not phrase or not canonical:
            self._notice("Type both your phrase and the built-in command it runs.", True)
            return
        self._save({"commands": {phrase: canonical}}, "Shortcut saved.")
        self.new_phrase.set_text("")
        self.canonical_phrase.set_text("")

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
                self._notice(str(result.get("error", "Couldn't remove it.")), True)
                return
            self.config = result["config"]
            self._quietly(self._show_language)
            self._render_aliases()
            self._render_phrases()
            self._render_apps()
            self._notice("Removed.")
        self._work(remove, removed)

    def _action(self, op: str, **kwargs: Any) -> None:
        if not self.connected:
            self._notice("Start Jarvis first.", True)
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
                self._notice(str(result.get("error", "That didn't work.")), True)
                return
            if "status" in result:
                self._set_status(result)
            if result.get("message"):
                self._notice(str(result["message"]))
        self._work(action, done)

    def _confirm_quit(self) -> None:
        if not hasattr(Gtk, "AlertDialog"):   # GTK older than 4.10
            self._quit_jarvis()
            return
        dialog = Gtk.AlertDialog()
        dialog.set_message("Quit Jarvis?")
        dialog.set_detail("Voice control and instant replay stop, and this window closes. A recording in progress "
                          "is saved first.")
        dialog.set_buttons(["Cancel", "Quit Jarvis"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        def chosen(source, result) -> None:
            try:
                if source.choose_finish(result) == 1:
                    self._quit_jarvis()
            except GLib.Error:
                pass
        dialog.choose(self, None, chosen)

    def _quit_jarvis(self) -> None:
        """Close the service (a recording is saved first), then this window."""
        def quit_service() -> dict:
            try:
                return client.request("quit", timeout=60)
            except (OSError, ConnectionError):
                return {"ok": True}   # already closed
        self._notice("Closing Jarvis…")
        self._work(quit_service, lambda _result: self.close())

    def _set_mute(self, muted: bool) -> None:
        self.mute_text.set_text("Unmute" if muted else "Mute")
        self.mute_icon.set_from_icon_name("microphone-disabled-symbolic" if muted else "audio-input-microphone-symbolic")
        self.mute_button.set_tooltip_text(("Open the voice microphone" if muted else "Close the voice microphone now")
                                          + " (Ctrl+M)")
        self.mute_button.update_property([Gtk.AccessibleProperty.LABEL],
                                         ["Unmute microphone" if muted else "Mute microphone"])
        if muted:
            self.mute_button.add_css_class("muted")
        else:
            self.mute_button.remove_css_class("muted")

    def _toggle_mute(self) -> None:
        if self.status.get("muted", True) and not self.config.get("privacy", {}).get("onboarded"):
            self.show_page("dashboard")
            self._notice("Allow the microphone on Home first.", True)
            return
        self._action("mute", muted=not bool(self.status.get("muted", True)))

    def _listen(self) -> None:
        if not self.config.get("privacy", {}).get("onboarded"):
            self.show_page("dashboard")
            self._notice("Allow the microphone on Home first.", True)
            return
        self._action("cancel" if self.status.get("state") == "listening" else "listen")

    def _toggle_test(self) -> None:
        if not self.config.get("privacy", {}).get("onboarded"):
            self.show_page("dashboard")
            self._notice("Allow the microphone on Home before testing it.", True)
            return
        enabled = not self._test_active
        def done(result: dict) -> None:
            if not result.get("ok"):
                self._notice(str(result.get("error", "The microphone test failed.")), True)
                return
            self._show_test(enabled)
        self._work(lambda: client.request("mic_test", enabled=enabled), done)

    def _show_state(self, state: str) -> None:
        pills = {"offline": "Offline", "paused": "Muted", "idle": "Waiting for wake word",
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
        self._notice("Starting Jarvis. The microphone stays closed until you allow it.")
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
            where = ("journalctl --user -u jarvis.service -n 50" if started_by == "systemd"
                     else f"the log at {log}")
            return {"ok": False, "error": f"Jarvis didn't become ready. Check {where}, then choose Try again."}
        def done(result: dict) -> None:
            self.start_button.set_sensitive(True)
            if result.get("ok"):
                self.refresh()
            else:
                self._notice(str(result.get("error", "Jarvis couldn't start.")), True)
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
        self._flush_before_close()
        self.closed = True
        self._watch_stop.set()
        return False


_THEME: Any = None


def load_theme(name: str | None = None) -> None:
    """Load style.css with the chosen Wada preset's colors (or switch to another preset)."""
    global _THEME
    display = Gdk.Display.get_default()
    if display is None:
        return
    style = Path(__file__).with_name("style.css").read_text(encoding="utf-8")
    css = palettes.gtk_colors(name) + re.sub(r"(?m)^@define-color .*\n", "", style)
    if _THEME is None:
        _THEME = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, _THEME, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    if hasattr(_THEME, "load_from_string"):
        _THEME.load_from_string(css)
    else:
        _THEME.load_from_data(css, -1)
    preset = palettes.PRESETS[name if name in palettes.PRESETS else palettes.chosen()]
    settings = Gtk.Settings.get_default()
    if settings is None:
        return
    # GTK's own parts (selection, scrollbars, dialogs) follow light or dark.
    if settings.find_property("gtk-interface-color-scheme") is not None:   # GTK 4.20+
        settings.set_property("gtk-interface-color-scheme", Gtk.InterfaceColorScheme.DARK if preset["dark"]
                              else Gtk.InterfaceColorScheme.LIGHT)
    else:
        settings.set_property("gtk-application-prefer-dark-theme", preset["dark"])


def service_log_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return state / "jarvis" / "daemon.log"


def start_service_process() -> tuple[str, Path | None]:
    """Prefer the installed user service so systemd owns, logs and cleans up the
    daemon. Without it, start a detached daemon whose output goes to a private
    log file (not /dev/null) and reap it so it never lingers as a zombie."""
    systemctl = shutil.which("systemctl")
    if systemctl:
        unit = subprocess.run([systemctl, "--user", "cat", "jarvis.service"],
                              capture_output=True, text=True, timeout=5, check=False)
        if unit.returncode == 0:
            subprocess.run([systemctl, "--user", "start", "jarvis.service"],
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
        process = subprocess.Popen([sys.executable, "-m", "jarvis", "daemon"],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=True)
    threading.Thread(target=process.wait, name="jarvis-daemon-reaper", daemon=True).start()
    return "process", log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis native GTK4 settings")
    parser.add_argument("--page", choices=sorted(PAGES | set(PAGE_ALIASES)), default="dashboard")
    args = parser.parse_args(argv)
    if Gtk is None:
        print("Jarvis settings require GTK4 and PyGObject (python-gobject on Arch, python3-gi and gir1.2-gtk-4.0 on Debian/Ubuntu).\n"
              f"Details: {GTK_ERROR}\nThe background CLI can run without the settings window.", file=sys.stderr)
        return 1
    if not Gtk.init_check():
        print("Cannot open a graphical display. Run ‘jarvis ui’ inside your Wayland desktop session. The service does not need a display window.", file=sys.stderr)
        return 1
    application = Gtk.Application(application_id="org.jarvis.Controller", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
    def activate(app: Any) -> None:
        window = app.get_active_window()
        if window is None:
            load_theme()
            window = JarvisWindow(app, args.page)
        window.present()
    def command_line(app: Any, command: Any) -> int:
        requested = parser.parse_args(command.get_arguments()[1:])
        activate(app)
        window = app.get_active_window()
        if window is not None:
            window.show_page(requested.page)
        return 0
    application.connect("activate", activate)
    application.connect("command-line", command_line)
    return application.run(["jarvis-ui", "--page", args.page])


if __name__ == "__main__":
    raise SystemExit(main())
