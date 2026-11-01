"""Recording options are exercised with fake captures and private fixture IPC."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from murmur.config import validate
from murmur.encoding import CODECS, gpu_video_args, storage_estimate
from murmur.recorder import Recorder
from murmur.replay import command


class EncodingTests(unittest.TestCase):
    def test_old_settings_keep_software_quality_and_new_options_are_validated(self):
        r = validate({})["recording"]
        self.assertEqual((r["codec"], r["quality"], r["rate_control"]), ("libx264", 23, "quality"))
        cases = [("codec", v) for v in (*CODECS, "bogus", None, [])]
        cases += [(key, v) for key in ("bitrate_mbps", "fps") for v in (True, 0, 1, 8.0, 60, 100, 101)]
        cases += [("rate_control", v) for v in ("quality", "bitrate", "auto", False)]
        cases += [("quality_preset", v) for v in ("medium", "high", "very_high", "ultra", {})]
        from tests.test_rust_controller import BINARY, policies
        results = policies([{"op": "validate", "config": {"recording": {key: value}}} for key, value in cases]) if BINARY.exists() else None
        for i, (key, value) in enumerate(cases):
            with self.subTest(key=key, value=value):
                try:
                    expected = validate({"recording": {key: value}})
                except ValueError:
                    if results:
                        self.assertFalse(results[i]["ok"])
                else:
                    if results:
                        self.assertEqual(results[i]["result"]["config"], expected)

    def test_formats_hdr_and_rate_control_reach_replay(self):
        for codec in CODECS:
            for color in ("sdr", "hdr"):
                cfg = validate({"recording": {"codec": codec, "monitor": "DP-1", "rate_control": "bitrate", "bitrate_mbps": 8}})["recording"]
                args = command(cfg, {}, Path("/fake"), Path("/fake/ipc"), color=color)
                expected = ("av1_hdr" if codec == "av1" else "hevc_hdr") if color == "hdr" else codec if codec in ("av1", "hevc", "h264") else "h264"
                self.assertEqual(args[args.index("-k") + 1], expected)
                self.assertEqual(args[args.index("-q") + 1], "8000")
                self.assertEqual(args[args.index("-bm") + 1], "cbr")
                self.assertEqual(args[args.index("-fallback-cpu-encoding") + 1], "no")
        self.assertEqual(storage_estimate({"bitrate_mbps": 8}), 60)
        self.assertEqual(storage_estimate({"bitrate_mbps": 8}, 120), 120)

    def test_sdr_gpu_uses_gpu_backend_with_explicit_audio(self):
        for codec in ("h264", "hevc", "av1"):
            cfg = validate({"recording": {"configured": True, "monitor": "DP-1", "codec": codec,
                           "system_audio": True, "audio_device": "game.monitor", "rate_control": "bitrate"}})
            with patch("murmur.recorder.sources", return_value=[{"name": "DP-1"}]), \
                    patch("murmur.recorder.audio_sources", return_value=[{"name": "game.monitor", "monitor": True}]), \
                    patch("murmur.recorder.shutil.which", side_effect=lambda name: "/fake/" + name):
                active, _, args = Recorder(cfg)._validate()
            self.assertTrue(active["gpu"])
            self.assertEqual(args[0], "/fake/gpu-screen-recorder")
            self.assertEqual(args[args.index("-k") + 1], codec)
            self.assertEqual(args[args.index("-a") + 1], "game.monitor")

    def test_cpu_bitrate_mode_uses_bounded_encoder_buffer_and_keeps_thread_limit(self):
        cfg = validate({"recording": {"configured": True, "monitor": "DP-1", "rate_control": "bitrate", "bitrate_mbps": 6}})
        with patch("murmur.recorder.sources", return_value=[{"name": "DP-1"}]), \
                patch("murmur.recorder.shutil.which", side_effect=lambda name: "/fake/" + name):
            active, _, args = Recorder(cfg)._validate()
        self.assertFalse(active["gpu"])
        for param in ("b=6000000", "maxrate=6000000", "bufsize=12000000", "threads=2"):
            self.assertIn(param, args)
        self.assertFalse(any(p.startswith("crf=") for p in args))

    def test_wrong_codec_cannot_be_announced_as_the_selected_gpu_format(self):
        from murmur.recorder import RecorderError, verify_video
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clip.mkv"
            path.write_bytes(b"fake")
            probe = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360}]}
            with patch("murmur.recorder._query", return_value=json.dumps(probe)):
                with self.assertRaisesRegex(RecorderError, "expected av1"):
                    verify_video(path, codec="av1")
                verify_video(path, codec="h264")

    def test_native_gpu_record_and_replay_use_same_budget(self):
        from tests.test_rust_controller import BINARY, Harness
        if not BINARY.exists():
            self.skipTest("build Rust controller")
        h = Harness(dry_run=False, MURMUR_TEST_CODEC="av1", config_patch={"recording": {"codec": "av1", "fps": 24, "rate_control": "bitrate", "bitrate_mbps": 6}})
        self.addCleanup(h.close)
        self.assertTrue(h.rpc("record_start")["ok"])
        h.wait(lambda: bool(h.events("gpu-screen-recorder")))
        args = h.events("gpu-screen-recorder")[0]["args"]
        for key, value in (("-k", "av1"), ("-f", "24"), ("-q", "6000"), ("-bm", "cbr")):
            self.assertEqual(args[args.index(key) + 1], value)
        self.assertIn("-o", args)
        self.assertNotIn("--file", args)
        self.assertTrue(h.rpc("record_stop")["ok"])
        self.assertTrue(h.rpc("replay_start")["ok"])
        # The backend also logs IPC readiness/requests; these are not launches.
        h.wait(lambda: any(e.get("event") == "ipc_ready" for e in h.events("gpu-screen-recorder")))
        replay = next(e["args"] for e in h.events("gpu-screen-recorder")
                      if "event" not in e and "-ipc" in e["args"])
        for key in ("-k", "-f", "-q", "-bm"):
            self.assertEqual(replay[replay.index(key) + 1], args[args.index(key) + 1])
        self.assertTrue(h.rpc("replay_stop")["ok"])
