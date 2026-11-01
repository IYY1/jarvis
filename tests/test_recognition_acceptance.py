from pathlib import Path
import tempfile
import unittest

from scripts.benchmark_recognition_acceptance import audio_case, evaluate


class AcceptanceGateTests(unittest.TestCase):
    def test_wrong_neighbour_and_extra_command_are_false_actions(self):
        events = [{"event": "wake"}, {"event": "command", "text": "stop recording"},
                  {"event": "command", "text": "start recording"}]
        result = evaluate(events, ["start recording"], 1)
        self.assertFalse(result["passed"])
        self.assertEqual(result["false_actions"], 2)

    def test_missing_command_and_false_wake_fail_separately(self):
        result = evaluate([], ["start recording"], 1)
        self.assertFalse(result["passed"])
        self.assertEqual(result["false_actions"], 0)
        result = evaluate([{"event": "wake"}], [], 0)
        self.assertFalse(result["passed"])
        self.assertEqual(result["extra_wakes"], 1)

    def test_background_audio_requires_no_commands_and_no_wakes(self):
        self.assertTrue(evaluate([], [], 0)["passed"])
        self.assertFalse(evaluate([{"event": "error"}], [], 0)["passed"])

    def test_repetition_preserves_background_quiet_wake_sequence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            clip = b"\x01\x02" * 3200
            (root / "fixture.raw").write_bytes(clip)
            data, clips = audio_case({"repeat": 3, "segments": [
                {"pcm": "fixture.raw"}, {"silence": .03}, {"pcm": "fixture.raw", "reverse": True}]}, root)
            padded = clip + bytes((-len(clip)) % 960)
            self.assertEqual(data, (padded + bytes(960) + padded) * 3)
            self.assertEqual(len(clips), 2)

    def test_invalid_or_unbounded_pcm_is_rejected_before_playback(self):
        with self.assertRaises(ValueError):
            audio_case({"repeat": 100, "segments": [{"silence": 60}]}, Path("."))
        with self.assertRaises(ValueError):
            audio_case({"segments": [{"silence": float("nan")}]}, Path("."))


if __name__ == "__main__":
    unittest.main()
