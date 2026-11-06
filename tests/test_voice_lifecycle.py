"""Parent-death policy checks with mocked process APIs; no microphone access."""
import unittest
import io
import copy
import gc
import types
import weakref
from unittest.mock import MagicMock, patch

from jarvis.voice import _parent_death
from jarvis.voice import main, run
from jarvis.config import DEFAULTS


class VoiceParentDeathTests(unittest.TestCase):
    def test_source_grammar_is_released_before_capture_starts(self):
        class Grammar(dict):
            pass
        for key in ("grammar", "phrases"):
            with self.subTest(key=key):
                config = copy.deepcopy(DEFAULTS)
                config["privacy"]["onboarded"] = True
                references = []
                def load(_line):
                    grammar = Grammar(phrases=["open firefox"])
                    references.append(weakref.ref(grammar))
                    return {"config": config, "mode": "once", key: grammar}
                def make_engine(_config, grammar):
                    self.assertEqual(grammar, {"phrases": ["open firefox"]})
                    return types.SimpleNamespace(frame_bytes=960, close=lambda: None)
                def capture(*args, **kwargs):
                    gc.collect()
                    self.assertIsNone(references[0]())
                    raise RuntimeError("fixture finished before capture")
                with patch("jarvis.voice.sys.stdin", types.SimpleNamespace(buffer=io.BytesIO(b"{}\n"))), \
                        patch("jarvis.voice.json.loads", new=load), \
                        patch("jarvis.voice.make_engine", new=make_engine), \
                        patch("jarvis.voice.subprocess.Popen", new=capture), \
                        patch("jarvis.voice._parent_death"), patch("jarvis.voice.signal.signal"), \
                        patch("jarvis.voice.selectors.DefaultSelector"), \
                        patch("jarvis.voice.emit") as emit:
                    main()
                self.assertTrue(any("fixture finished" in call.kwargs.get("message", "")
                                    for call in emit.call_args_list))

    def test_stalled_microphone_reports_error_and_closes_capture(self):
        config = copy.deepcopy(DEFAULTS)
        config["privacy"]["onboarded"] = True
        capture = MagicMock()
        capture.stderr = io.BytesIO()
        capture.stdout = io.BytesIO()
        capture.wait.return_value = 0
        selector = MagicMock()
        selector.select.return_value = []
        tick = iter([0, 0, 0, 4])
        with patch("jarvis.voice._parent_death"), patch("jarvis.voice.signal.signal"), \
                patch("jarvis.voice.subprocess.Popen", return_value=capture), \
                patch("jarvis.voice.selectors.DefaultSelector", return_value=selector), \
                patch("jarvis.voice.time.monotonic", side_effect=lambda: next(tick)), \
                patch("jarvis.voice.emit") as emit:
            run(config, "test", [])
        self.assertTrue(any(c.kwargs.get("event") == "error" and "No microphone frames" in c.kwargs.get("message", "") for c in emit.call_args_list))
        capture.terminate.assert_called_once()

    def test_reparenting_to_a_subreaper_rejects_the_wrong_owner(self):
        library = MagicMock()
        library.prctl.return_value = 0
        with patch("jarvis.voice.os.getppid", return_value=456), \
                patch("jarvis.voice.ctypes.CDLL", return_value=library), \
                patch("jarvis.voice.os.kill", side_effect=SystemExit(0)):
            with self.assertRaises((SystemExit, RuntimeError, OSError)):
                _parent_death(expected_parent=123)

    def test_already_orphaned_worker_exits_before_capture(self):
        library = MagicMock()
        library.prctl.return_value = 0
        with patch("jarvis.voice.os.getppid", return_value=1), \
                patch("jarvis.voice.ctypes.CDLL", return_value=library), \
                patch("jarvis.voice.os.kill", side_effect=SystemExit(0)):
            with self.assertRaises((SystemExit, RuntimeError, OSError)):
                _parent_death()

    def test_failed_parent_death_registration_fails_closed(self):
        library = MagicMock()
        library.prctl.return_value = -1
        with patch("jarvis.voice.os.getppid", return_value=123), \
                patch("jarvis.voice.ctypes.CDLL", return_value=library), \
                patch("jarvis.voice.os.kill", side_effect=SystemExit(0)):
            with self.assertRaises((SystemExit, RuntimeError, OSError)):
                _parent_death()


if __name__ == "__main__":
    unittest.main()
