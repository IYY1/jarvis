"""Dictation indicator: a small bar at the top of the screen with a Stop button.

It is a separate process that the service starts with dictation and that
exits when dictation ends. On Wayland it uses gtk4-layer-shell, so it floats
above windows and never takes keyboard focus from the field being typed into.
Without layer-shell (or GTK) it shows a desktop notification instead; the
stop phrase and `jarvis dictate stop` still work.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading

from . import client

LANGUAGES = {"ar": "Arabic", "en": "English"}


def _notify(text: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Jarvis", "-t", "4000", "Dictation", text], check=False)


def main() -> int:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import GLib, Gtk, Gtk4LayerShell as Layer
    except (ImportError, ValueError):
        _notify("Typing what you say. Say “stop dictation” or press your dictation shortcut to stop.")
        return 0
    if not Gtk.init_check() or not Layer.is_supported():
        _notify("Typing what you say. Say “stop dictation” or press your dictation shortcut to stop.")
        return 0

    app = Gtk.Application(application_id="org.jarvis.Dictation")
    stop = threading.Event()

    def build(application):
        window = Gtk.ApplicationWindow(application=application, title="Jarvis dictation")
        Layer.init_for_window(window)
        Layer.set_layer(window, Layer.Layer.OVERLAY)
        Layer.set_anchor(window, Layer.Edge.TOP, True)
        Layer.set_margin(window, Layer.Edge.TOP, 12)
        Layer.set_keyboard_mode(window, Layer.KeyboardMode.NONE)   # never take focus
        Layer.set_namespace(window, "jarvis-dictation")
        css = Gtk.CssProvider()
        css.load_from_string(
            ".dictation { background: #1f1a2b; color: #f3effa; border: 1px solid #e9c46a; border-radius: 999px; padding: 6px 8px 6px 16px; }"
            ".dictation .dot { color: #e9c46a; font-weight: 800; }"
            ".dictation .detail { color: #c3bbd5; }"
            ".dictation button { background: #2b2440; color: #f3effa; border: 1px solid #594d77; border-radius: 999px; padding: 4px 12px; }"
            ".dictation button.stop { background: #e9c46a; color: #2a1f05; border-color: #e9c46a; }")
        Gtk.StyleContext.add_provider_for_display(window.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("dictation")
        dot = Gtk.Label(label="●")
        dot.add_css_class("dot")
        title = Gtk.Label(label="Dictating")
        detail = Gtk.Label(label="Starting…")
        detail.add_css_class("detail")
        detail.set_max_width_chars(48)
        detail.set_ellipsize(3)   # Pango.EllipsizeMode.END
        stop_button = Gtk.Button(label="Stop")
        stop_button.add_css_class("stop")
        stop_button.set_tooltip_text("Stop dictation; what you already said is still typed")
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.set_tooltip_text("Stop and discard speech that has not been typed yet")
        for widget in (dot, title, detail, stop_button, cancel_button):
            row.append(widget)
        window.set_child(row)

        def act(action):
            threading.Thread(target=lambda: _request(action), daemon=True).start()

        stop_button.connect("clicked", lambda _b: act("stop"))
        cancel_button.connect("clicked", lambda _b: act("cancel"))

        def show(status: dict) -> bool:
            dictation = status.get("dictation") or {}
            if not dictation.get("active"):
                window.close()
                return False
            language = LANGUAGES.get(dictation.get("language", ""), "")
            typed = dictation.get("inserted", 0)
            parts = [p for p in (language, f"{typed} characters typed" if typed else "speak now",
                                 dictation.get("message", "") if dictation.get("held") else "") if p]
            detail.set_label(" · ".join(parts))
            return False

        def watch() -> None:
            try:
                client.watch(lambda event: GLib.idle_add(show, event.get("status", {})), stop)
            except OSError:
                pass
            GLib.idle_add(window.close)

        window.connect("close-request", lambda _w: (stop.set(), False)[1])
        window.present()
        threading.Thread(target=watch, daemon=True).start()

    app.connect("activate", build)
    return app.run([sys.argv[0]])


def _request(action: str) -> None:
    try:
        client.request("dictation", action=action)
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
