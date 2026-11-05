"""Close-program commands use exact app identities and graceful window requests."""
import dataclasses
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from murmur import config
from murmur.actions import Dispatcher
from murmur.commands import CommandError, GrammarMatcher, Intent, Parser, REFERENCE, closest_match, match_candidates
from murmur.hyprland import Hyprland


class CloseCommandTests(unittest.TestCase):
    def test_every_openable_name_and_configured_alias_can_be_closed(self):
        cfg = config.validate({"aliases": {"web browser": "firefox.desktop"},
                               "app_prefixes": {"web browser": "watch"}})
        parser = Parser.from_config(cfg)
        names = ["firefox", "web browser", "visual studio code", "steam"]
        grammar = parser.grammar(names)
        matcher = GrammarMatcher(grammar["phrases"], grammar["apps"], grammar["verbs"])
        candidates = match_candidates(dict(grammar, app_ids={"firefox": "firefox.desktop", "web browser": "firefox.desktop"}))
        for name in names:
            for phrase in (f"close {name}", f"please close {name}", f"close the {name} please"):
                self.assertEqual(parser.parse(phrase), Intent("close_app", name))
                self.assertEqual(closest_match(phrase, candidates), (f"close {name}", 1.0))
            self.assertTrue(matcher.accepts(f"close {name}"))
        self.assertEqual(candidates["close web browser"], candidates["close firefox"])
        self.assertTrue(any("close Firefox" in row["examples"] for row in REFERENCE))

    def test_close_is_not_guessed_from_an_open_command_or_incomplete_name(self):
        candidates = match_candidates(Parser().grammar(["visual studio code", "firefox"]))
        for text in ("visual studio code", "cloze visual studio code", "close visual studio cod", "close fire fox"):
            result = closest_match(text, candidates)
            self.assertTrue(result is None or not result[0].startswith("close "), (text, result))
            if text.startswith("close "):
                self.assertIsNone(result)
        self.assertEqual(closest_match("open visual studio code", candidates), ("open visual studio code", 1.0))

    def test_configured_close_words_replace_close(self):
        cfg = config.validate({"close_words": ["Quit", "shut down"], "aliases": {"web browser": "firefox.desktop"},
                               "app_prefixes": {"web browser": ""}})
        self.assertEqual(cfg["close_words"], ["Quit", "shut down"])
        parser = Parser.from_config(cfg)
        for phrase in ("quit firefox", "please shut down the firefox", "quit web browser"):
            self.assertEqual(parser.parse(phrase).action, "close_app", phrase)
        with self.assertRaises(CommandError):
            parser.parse("close firefox")
        grammar = parser.grammar(["firefox", "web browser"])
        self.assertEqual(grammar["close_verbs"], ["quit", "shut down"])
        self.assertIn("quit", grammar["verbs"])
        self.assertNotIn("close", grammar["verbs"])
        candidates = match_candidates(dict(grammar, app_ids={"firefox": "firefox.desktop", "web browser": "firefox.desktop"}))
        self.assertEqual(closest_match("quit firefox", candidates, close_verbs=grammar["close_verbs"]), ("quit firefox", 1.0))
        # A misheard close word phrase must not fall back to opening "web browser".
        self.assertIsNone(closest_match("quit web browsr", candidates, close_verbs=grammar["close_verbs"]))
        self.assertTrue(GrammarMatcher(grammar["phrases"], grammar["apps"], grammar["verbs"]).accepts("quit firefox"))

    def test_close_words_are_validated(self):
        self.assertEqual(config.validate({})["close_words"], ["close"])
        for bad in ([], ["open"], ["please"], ["quit", "QUIT"], ["a b c d"], [""], ["x" * 41], "close", [1], ["close"] * 9):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate({"close_words": bad})

    def test_dispatch_closes_only_resolved_windows_without_launching_or_focusing(self):
        apps, desktop = MagicMock(), MagicMock()
        apps.resolve.return_value.name = "Firefox"
        desktop.running.return_value = [{"address": "0x1"}, {"address": "0x2"}, {"address": "0x1"}]
        dispatch = Dispatcher(apps, desktop, MagicMock(), MagicMock(), MagicMock())
        self.assertEqual(dispatch.execute(Intent("close_app", "browser")), "Close requested for Firefox")
        apps.resolve.assert_called_once_with("browser")
        desktop.dispatch.assert_has_calls([call("close_address", "0x1"), call("close_address", "0x2")])
        self.assertEqual(desktop.dispatch.call_count, 2)
        apps.launch.assert_not_called()
        desktop.running.return_value = []
        desktop.dispatch.reset_mock()
        with self.assertRaisesRegex(CommandError, "not running"):
            dispatch.execute(Intent("close_app", "browser"))
        desktop.dispatch.assert_not_called()
        desktop.running.side_effect = CommandError("desktop unavailable")
        with self.assertRaisesRegex(CommandError, "unavailable"):
            dispatch.execute(Intent("close_app", "browser"))
        apps.launch.assert_not_called()

    def test_hyprland_targets_a_validated_window_in_both_syntaxes(self):
        self.assertEqual(Hyprland._commands("close_address", "0xabc"), {
            "legacy": ["closewindow", "address:0xabc"],
            "lua": ['hl.dsp.window.close({ window = "address:0xabc" })']})
        with patch("murmur.hyprland.subprocess.run") as run:
            for target in (None, "", "activewindow", "0x", "0x1; quit", "0x1\"", 1, True):
                with self.assertRaises(CommandError):
                    Hyprland().dispatch("close_address", target)
            run.assert_not_called()

    def test_windows_posts_wm_close_to_the_matched_window_without_foreground_changes(self):
        from murmur.windows.desktop import Desktop
        gui = types.SimpleNamespace(IsWindow=MagicMock(return_value=True), PostMessage=MagicMock(),
                                    GetForegroundWindow=MagicMock())
        constants = types.SimpleNamespace(WM_CLOSE=16)
        with patch.dict(sys.modules, win32gui=gui, win32con=constants):
            desktop = Desktop()
            desktop.dispatch("close_address", 123)
            gui.PostMessage.assert_called_once_with(123, 16, 0, 0)
            gui.GetForegroundWindow.assert_not_called()
            gui.PostMessage.reset_mock()
            gui.IsWindow.return_value = False
            for target in (123, None, "123", 0, -1, True):
                with self.assertRaises(CommandError):
                    desktop.dispatch("close_address", target)
            gui.PostMessage.assert_not_called()


ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "native/controller/target/release/jarvis-controller"

@unittest.skipUnless(BINARY.is_file(), "build the Rust controller")
class NativeCloseCommandTests(unittest.TestCase):
    def test_native_policy_and_window_address_validation_match_python(self):
        from tests.test_rust_controller import policies
        phrases = ["close Firefox", "please close the firefox", "close visual studio code please"]
        actual = policies([{"op": "parse", "text": text} for text in phrases])
        for text, result in zip(phrases, actual, strict=True):
            self.assertEqual(result["result"]["intent"], dataclasses.asdict(Parser().parse(text)))
        cfg = {"close_words": ["quit", "kill"]}
        phrases = ["quit firefox", "kill the steam", "close firefox", "clip last 25 seconds", "save the last two minutes"]
        actual = policies([{"op": "parse", "text": text, "config": cfg} for text in phrases])
        parser = Parser.from_config(config.validate(cfg))
        for text, result in zip(phrases, actual, strict=True):
            try:
                expected = dataclasses.asdict(parser.parse(text))
            except ValueError:
                self.assertFalse(result["ok"], text)
            else:
                self.assertEqual(result["result"]["intent"], expected, text)
        grammar = policies([{"op": "grammar", "apps": ["firefox"], "config": cfg}])[0]["result"]["grammar"]
        expected = parser.grammar(["firefox"])
        for key in ("verbs", "close_verbs", "phrases"):
            self.assertEqual(sorted(grammar[key]), sorted(expected[key]), key)
        for bad in ([], ["open"], ["please"]):
            self.assertFalse(policies([{"op": "validate", "config": {"close_words": bad}}])[0]["ok"], bad)
        targets = ["0xabc", "0x1; anything", "", None, 1]
        results = policies([{"op": "dispatch", "action": "close_address", "target": target} for target in targets])
        expected = Hyprland._commands("close_address", "0xabc")
        self.assertEqual(results[0]["result"], expected)
        self.assertTrue(all(not result["ok"] for result in results[1:]))

    def test_controller_closes_all_matching_windows_and_reports_missing_apps(self):
        from tests.test_rust_controller import Harness
        windows = [{"address": "0x1", "class": "firefox"}, {"address": "0x2", "initialClass": "firefox"},
                   {"address": "0x3", "class": "code"}, {"address": "0x1", "class": "firefox"}]
        h = Harness(dry_run=False, MURMUR_TEST_CLIENTS=json.dumps(windows))
        self.addCleanup(h.close)
        apps = h.root / "data/applications"
        apps.mkdir(parents=True)
        (apps / "firefox.desktop").write_text('[Desktop Entry]\nType=Application\nName=Firefox\nExec=firefox\nStartupWMClass=firefox\n')
        self.assertTrue(h.rpc("apps")["ok"])
        reply = h.rpc("execute", phrase="close firefox")
        self.assertTrue(reply["ok"], reply)
        dispatches = [event["args"] for event in h.events("hyprctl") if event["args"][0] == "dispatch"]
        self.assertEqual(dispatches, [["dispatch", "closewindow", "address:0x1"], ["dispatch", "closewindow", "address:0x2"]])
        self.assertEqual(h.events("gio"), [])
        self.assertFalse(h.rpc("execute", phrase="close missing app")["ok"])
        self.assertTrue(any("close Firefox" in row["examples"] for row in h.rpc("commands")["commands"]))

if __name__ == "__main__":
    unittest.main()
