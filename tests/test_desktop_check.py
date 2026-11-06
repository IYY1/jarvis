"""Regression checks for reporting failed integrations without capturing video."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from jarvis.recorder import RecorderError


spec = importlib.util.spec_from_file_location(
    "jarvis_check_desktop", Path(__file__).resolve().parents[1] / "scripts" / "check_desktop.py")
desktop_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop_check)


class DesktopCheckTests(unittest.TestCase):
    def test_backend_failure_without_a_file_preserves_diagnostic(self):
        recorder = MagicMock()
        recorder.status.return_value = {"state": "error", "file": None, "message": "encoder unavailable"}
        recorder.stop.side_effect = RecorderError("encoder unavailable")
        desktop = MagicMock()
        desktop.query.return_value = {"address": "0x123"}
        args = SimpleNamespace(monitor="DP-1", codec="h264_vaapi", seconds=3,
                               vaapi_device="/dev/dri/renderD128")
        with patch.object(desktop_check, "Recorder", return_value=recorder), \
                patch.object(desktop_check.time, "sleep"):
            result = desktop_check.recording_check(args, Path("/tmp/unused-test-fixture"), desktop)
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["error"], "encoder unavailable")
        self.assertFalse(result["finalized_and_ffprobe_validated"])
        self.assertFalse(result["test_video_deleted"])
        self.assertIsNone(result["recorder_status"]["file"])
        recorder.close.assert_called_once()

    def test_cleanup_error_does_not_replace_original_capture_error(self):
        recorder = MagicMock()
        recorder.start.side_effect = RecorderError("capture startup failed")
        recorder.close.side_effect = RecorderError("cleanup failed")
        recorder.status.return_value = {"state": "error", "file": None}
        args = SimpleNamespace(monitor="DP-1", codec="libx264", seconds=3,
                               vaapi_device="/dev/dri/renderD128")
        with patch.object(desktop_check, "Recorder", return_value=recorder):
            result = desktop_check.recording_check(args, Path("/tmp/unused-test-fixture"), MagicMock())
        self.assertEqual(result["error"], "capture startup failed")
        self.assertEqual(result["cleanup_error"], "cleanup failed")


if __name__ == "__main__":
    unittest.main()
