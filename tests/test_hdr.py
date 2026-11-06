"""Color selection and media validation; no screen or microphone access."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.recorder import RecorderError, capture_color, sources, verify_video


class ColorTests(unittest.TestCase):
    def test_auto_uses_hdr_presets_and_not_bit_depth_or_capability(self):
        for preset, expected in (("hdr", "hdr"), ("hdredid", "hdr"), ("srgb", "sdr"),
                                 ("wide", "sdr"), ("auto", "sdr"), (None, "sdr")):
            with self.subTest(preset=preset):
                self.assertEqual(capture_color({}, {"colorManagementPreset": preset,
                                                    "currentFormat": "XRGB2101010", "supportsHDR": True}), expected)

    def test_manual_hdr_covers_old_compositors_and_fullscreen_passthrough(self):
        self.assertEqual(capture_color({"color_mode": "hdr"}, {}), "hdr")
        self.assertEqual(capture_color({"color_mode": "hdr"}, {"colorManagementPreset": "srgb"}), "hdr")

    def test_sdr_on_hdr_display_fails_with_actionable_message(self):
        with self.assertRaisesRegex(RecorderError, "does not tone map"):
            capture_color({"color_mode": "sdr"}, {"colorManagementPreset": "hdr"})

    def test_color_setting_migrates_and_rejects_invalid_values(self):
        self.assertEqual(config.validate({})["recording"]["color_mode"], "auto")
        for mode in ("auto", "hdr", "sdr"):
            self.assertEqual(config.validate({"recording": {"color_mode": mode}})["recording"]["color_mode"], mode)
        for invalid in (True, None, "10bit", ["hdr"]):
            with self.subTest(mode=invalid), self.assertRaises(ValueError):
                config.validate({"recording": {"color_mode": invalid}})

    def test_source_discovery_retains_color_metadata(self):
        with patch("jarvis.recorder._query", return_value=json.dumps([
                {"name": "DP-1", "colorManagementPreset": "hdr", "currentFormat": "XRGB2101010"}])):
            self.assertEqual(sources()[0]["colorManagementPreset"], "hdr")


class HDRValidationTests(unittest.TestCase):
    def test_hdr_requires_ten_bit_pq_and_bt2020_on_the_same_video_stream(self):
        stream = {"codec_type": "video", "width": 1280, "height": 720, "pix_fmt": "yuv420p10le",
                  "color_transfer": "smpte2084", "color_primaries": "bt2020",
                  "color_space": "bt2020nc", "color_range": "tv"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mkv"
            path.write_bytes(b"fixture")
            with patch("jarvis.recorder._query", return_value=json.dumps({"streams": [stream]})):
                verify_video(path, color="hdr")
            for field, value in (("pix_fmt", "yuv420p"), ("color_transfer", "bt709"),
                                 ("color_primaries", "bt709"), ("color_space", "bt709"),
                                 ("color_range", "pc")):
                with self.subTest(field=field), patch("jarvis.recorder._query", return_value=json.dumps(
                        {"streams": [{**stream, field: value}]})):
                    with self.assertRaisesRegex(RecorderError, "HDR verification failed"):
                        verify_video(path, color="hdr")
                    self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
