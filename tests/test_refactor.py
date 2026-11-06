"""Policy equivalence and lifecycle regressions; no live desktop or microphone."""
import io
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from jarvis import client, config
from jarvis.apps import App
from jarvis.hyprland import Hyprland
from jarvis.recorder import RecorderError
from jarvis.replay import Replay


class SettingsOwnershipTests(unittest.TestCase):
    def test_nested_patches_preserve_siblings_and_do_not_mutate_defaults(self):
        base = {"nested": {"old": [1], "changed": {"a": 1}}}
        update = {"nested": {"new": [2], "changed": {"b": [3]}}, "extra": {"list": []}}
        merged = config.merged(base, update)
        self.assertEqual(merged, {"nested": {"old": [1], "changed": {"a": 1, "b": [3]},
                                            "new": [2]}, "extra": {"list": []}})
        merged["nested"]["old"].append(4)
        self.assertEqual(base, {"nested": {"old": [1], "changed": {"a": 1}}})
        self.assertEqual(update, {"nested": {"new": [2], "changed": {"b": [3]}}, "extra": {"list": []}})


class WindowMatchingTests(unittest.TestCase):
    def test_current_and_initial_classes_preserve_focus_order_and_unicode_folding(self):
        app = App("strasse.desktop", "Straße", "/fixture/strasse.desktop", "strasse")
        windows = [{"class": "other", "initialClass": "STRASSE", "focusHistoryID": 2},
                   {"class": "Straße", "focusHistoryID": 1},
                   {"class": "unrelated", "focusHistoryID": 0}]
        desktop = Hyprland()
        with patch.object(desktop, "query", return_value=windows):
            self.assertEqual(desktop.running(app), [windows[1], windows[0]])


class ResponseValidationTests(unittest.TestCase):
    def test_non_object_service_responses_are_connection_errors(self):
        for value in ([], None, "status", 1):
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.makefile.return_value.__enter__.return_value.readline.return_value = json.dumps(value).encode() + b"\n"
            with self.subTest(value=value), patch.object(client.socket, "socket", return_value=connection):
                with self.assertRaises(ConnectionError):
                    client.request("status")

    def test_non_object_replay_responses_are_actionable_errors(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.makefile.return_value.__enter__.return_value.readline.return_value = b"[]\n"
        with patch("jarvis.replay.socket.socket", return_value=connection):
            with self.assertRaisesRegex(RecorderError, "invalid response"):
                Replay(config.validate({}))._request("save-replay")

    def test_replay_stop_handles_a_child_that_exits_before_signalling(self):
        replay = Replay(config.validate({}))
        process = MagicMock(stderr=io.BytesIO(), **{"wait.return_value": 0})
        replay._process = process
        replay._status["state"] = "on"
        replay._done.clear()
        watcher = threading.Thread(target=replay._watch, args=(process,))
        def exit_before_signal(_signal):
            watcher.start()
            raise ProcessLookupError
        process.send_signal.side_effect = exit_before_signal
        try:
            self.assertEqual(replay.stop()["state"], "off")
        finally:
            watcher.join(timeout=2)
        self.assertFalse(watcher.is_alive())
        self.assertTrue(process.stderr.closed)


if __name__ == "__main__":
    unittest.main()
