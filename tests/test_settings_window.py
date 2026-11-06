"""The settings window's color presets and apply-as-you-change saving."""
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from jarvis import palettes
from jarvis.client import recording_capture_changed

ROOT = Path(__file__).resolve().parents[1]


class PaletteTests(unittest.TestCase):
    def test_every_preset_is_readable(self):
        # WCAG AA: 4.5:1 for text, 3:1 for focus rings and component edges; body text aims for 7:1.
        pairs = [("text", "bg", 7), ("text", "card", 7), ("text", "surface", 7), ("text", "inset", 7),
                 ("text-muted", "bg", 4.5), ("text-muted", "card", 4.5), ("text", "control", 4.5),
                 ("text", "selected", 4.5), ("on-accent", "accent", 4.5), ("on-live", "live", 4.5),
                 ("live-text", "bg", 4.5), ("live-text", "card", 4.5), ("accent-text", "bg", 4.5),
                 ("accent-text", "card", 4.5), ("error-text", "error-bg", 4.5), ("text", "live-soft", 4.5),
                 ("focus", "bg", 3), ("accent", "bg", 3)]
        for name, preset in palettes.PRESETS.items():
            for foreground, background, needed in pairs:
                with self.subTest(preset=name, pair=(foreground, background)):
                    ratio = palettes.contrast(preset["roles"][foreground], preset["roles"][background])
                    self.assertGreaterEqual(ratio, needed)

    def test_presets_are_combinations_from_the_book(self):
        self.assertEqual(len(palettes.PRESETS), 3)
        self.assertEqual({p["number"] for p in palettes.PRESETS.values()}, {344, 166, 190})
        self.assertEqual({p["dark"] for p in palettes.PRESETS.values()}, {True, False})   # a light one too
        self.assertEqual(palettes.PRESETS["344"]["swatches"]["Aconite Violet"], "#a36aa5")

    def test_every_color_the_stylesheet_uses_is_defined(self):
        style = (ROOT / "src/jarvis/style.css").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?m)^@define-color", style))   # colors come only from the chosen preset
        used = set(re.findall(r"@([a-z][a-z-]*)", re.sub(r"(?s)/\*.*?\*/", "", style)))
        for name, preset in palettes.PRESETS.items():
            with self.subTest(preset=name):
                self.assertLessEqual(used, set(preset["roles"]))
                self.assertIn("@define-color text-muted ", palettes.gtk_colors(name))

    def test_choice_is_kept_beside_the_settings(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {"JARVIS_CONFIG": str(Path(folder) / "config.json")}):
            self.assertEqual(palettes.chosen(), palettes.DEFAULT)
            palettes.choose("190")
            self.assertEqual(palettes.chosen(), "190")
            self.assertEqual(palettes.preference_path(), Path(folder) / "appearance.json")
            palettes.preference_path().write_text('{"palette": "999"}')
            self.assertEqual(palettes.chosen(), palettes.DEFAULT)   # unknown: the default, not an error
            with self.assertRaises(ValueError):
                palettes.choose("999")


class CaptureRestartTests(unittest.TestCase):
    def test_only_capture_settings_restart_a_running_capture(self):
        base = {"fps": 60, "sound_feedback": False, "configured": False, "monitor": "DP-3"}
        self.assertFalse(recording_capture_changed(base, dict(base, sound_feedback=True, configured=True)))
        self.assertTrue(recording_capture_changed(base, dict(base, fps=120)))
        self.assertTrue(recording_capture_changed(base, dict(base, monitor="DP-5")))


class ApplyAsYouChangeTests(unittest.TestCase):
    def setUp(self):
        try:
            from jarvis.ui import Gtk, JarvisWindow
        except (ImportError, ValueError):
            self.skipTest("GTK is not available")
        if Gtk is None:
            self.skipTest("GTK is not available")
        self.Window = JarvisWindow

    def window(self, **fields):
        window = MagicMock(**{"closed": False, "connected": True, "_loading": False, "_autosave_timer": 0, **fields})
        window._dirty = set()
        window._pending_patch = lambda: self.Window._pending_patch(window)
        return window

    def test_shown_values_are_not_saved(self):
        window = self.window(_loading=True)
        with patch("jarvis.ui.GLib") as glib:
            self.Window._autosave(window, "voice")
        glib.timeout_add.assert_not_called()
        self.assertEqual(window._dirty, set())

    def test_a_burst_of_changes_makes_one_save_after_the_pause(self):
        window = self.window()
        with patch("jarvis.ui.GLib") as glib:
            glib.timeout_add.side_effect = [11, 12]
            self.Window._autosave(window, "voice", 800)
            window._autosave_timer = 11
            self.Window._autosave(window, "replay", 400)
        glib.source_remove.assert_called_once_with(11)
        self.assertEqual(window._dirty, {"voice", "replay"})

    def test_offline_changes_say_they_were_not_saved(self):
        window = self.window(connected=False)
        with patch("jarvis.ui.GLib") as glib:
            self.Window._autosave(window, "voice")
        glib.timeout_add.assert_not_called()
        self.assertTrue(window._notice.call_args.args[1])   # shown as an error

    def test_unchanged_settings_are_not_saved_again(self):
        window = self.window(config={"close_words": ["close"]})
        window._patches = {"close_words": lambda: {"close_words": ["close"]}}
        window._dirty = {"close_words"}
        self.Window._flush_autosave(window)
        window._save.assert_not_called()
        window._patches = {"close_words": lambda: {"close_words": ["close", "quit"]}}
        window._dirty = {"close_words"}
        self.Window._flush_autosave(window)
        window._save.assert_called_once_with({"close_words": ["close", "quit"]}, "Saved.", replace_keys=("close_words",))
        self.assertEqual(window._dirty, set())

    def test_an_invalid_group_is_held_back_and_the_rest_saved(self):
        window = self.window(config={"replay": {"seconds": 120}})
        window._patches = {"recorder": lambda: None, "replay": lambda: {"replay": {"seconds": 300}}}
        window._dirty = {"recorder", "replay"}
        self.Window._flush_autosave(window)
        window._save.assert_called_once_with({"replay": {"seconds": 300}}, "Saved.", replace_keys=())

    def test_text_typed_but_not_confirmed_is_found(self):
        text = lambda value: MagicMock(get_text=MagicMock(return_value=value))
        window = self.window(config={"activation": {"wake_phrase": "jarvis", "wake_aliases": ["alfred"]},
                                     "dictation": {"stop_phrases": ["stop dictation"]},
                                     "replay": {"separate_apps": [], "buffer_dir": ""},
                                     "recording": {"output_dir": "~/Videos/Jarvis"}, "close_words": ["close"]},
                             wake_phrase=text("Jarvis "), wake_aliases=text("alfred"), stop_phrases=text("stop dictation"),
                             replay_apps=text(""), replay_buffer_dir=text(""), output_dir=text("~/Videos/Clips"),
                             close_words=text("close"))
        self.assertEqual(self.Window._unconfirmed_text(window), {"recorder"})

    def test_durations_read_naturally(self):
        from jarvis.ui import _duration
        self.assertEqual([_duration(s) for s in (45, 60, 230, 1800)], ["45 s", "1 min", "3 min 50 s", "30 min"])


if __name__ == "__main__":
    unittest.main()
