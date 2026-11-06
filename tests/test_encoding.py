"""Recording options are exercised with fake captures and private fixture IPC."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis.config import validate
from jarvis.encoding import CODECS, GPU_CODECS, gpu_video_args, storage_estimate
from jarvis.recorder import Recorder
from jarvis.replay import command


class EncodingTests(unittest.TestCase):
    def test_old_settings_keep_software_quality_and_new_options_are_validated(self):
        r = validate({})["recording"]
        # Fixed bitrate that follows resolution and frame rate is the default.
        self.assertEqual((r["codec"], r["quality"], r["rate_control"], r["bitrate_auto"]), ("libx264", 23, "bitrate", True))
        cases = [("codec", v) for v in (*CODECS, "bogus", None, [])]
        cases += [(key, v) for key in ("bitrate_mbps", "fps") for v in (True, 0, 1, 8.0, 60, 100, 101, 120, 240, 241, 500, 501)]
        cases += [("resolution", v) for v in ("native", "2160", "1440", "1080", "720", "1600", 1080)]
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
                expected = ("av1_hdr" if codec.startswith("av1") else "hevc_hdr") if color == "hdr" else codec if codec in GPU_CODECS else "h264"
                self.assertEqual(args[args.index("-k") + 1], expected)
                self.assertEqual(args[args.index("-q") + 1], "8000")
                self.assertEqual(args[args.index("-bm") + 1], "cbr")
                self.assertEqual(args[args.index("-fallback-cpu-encoding") + 1], "no")
        self.assertEqual(storage_estimate({"bitrate_mbps": 8}), 60)
        self.assertEqual(storage_estimate({"bitrate_mbps": 8}, 120), 120)

    def test_resolution_caps_output_size_only_when_chosen(self):
        for resolution, size in (("native", None), ("2160", "3840x2160"), ("1080", "1920x1080")):
            cfg = validate({"recording": {"codec": "av1", "resolution": resolution, "fps": 144}})["recording"]
            args = gpu_video_args("sdr", cfg)
            self.assertEqual(args[args.index("-s") + 1] if "-s" in args else None, size)
            args = command(cfg, {}, Path("/fake"), Path("/fake/ipc"))
            self.assertEqual(args[args.index("-f") + 1], "144")

    def test_replay_uses_recording_video_unless_it_has_its_own(self):
        from jarvis.encoding import replay_video
        same = validate({"recording": {"codec": "av1_10bit", "fps": 60}, "replay": {"fps": 30}})
        self.assertEqual((replay_video(same)["codec"], replay_video(same)["fps"]), ("av1_10bit", 60))
        own = validate({"recording": {"codec": "av1_10bit", "fps": 60},
                        "replay": {"own_video": True, "codec": "hevc", "fps": 120, "resolution": "1080"}})
        video = replay_video(own)
        self.assertEqual((video["codec"], video["fps"], video["resolution"], video["monitor"]), ("hevc", 120, "1080", ""))
        args = command(video, own["replay"], Path("/fake"), Path("/fake/ipc"))
        self.assertEqual((args[args.index("-k") + 1], args[args.index("-f") + 1], args[args.index("-s") + 1]),
                         ("hevc", "120", "1920x1080"))
        with self.assertRaises(ValueError):
            validate({"replay": {"codec": "libx264"}})  # replay always records on the GPU

    def test_saving_while_replay_runs_restarts_it_with_the_new_settings(self):
        from tests.test_rust_controller import BINARY, Harness
        from jarvis.client import restart_note
        if not BINARY.exists():
            self.skipTest("build Rust controller")
        h = Harness(dry_run=False, JARVIS_TEST_CODEC="av1", config_patch={"recording": {"codec": "av1", "fps": 60}})
        self.addCleanup(h.close)
        self.assertTrue(h.rpc("replay_start")["ok"])
        launches = lambda: [e["args"] for e in h.events("gpu-screen-recorder") if not e.get("event") and "-ipc" in e["args"]]
        h.wait(lambda: h.status()["replay"]["state"] == "on" and len(launches()) == 1)
        config = h.rpc("config")["config"]
        config["recording"]["fps"] = 120
        result = h.rpc("save_config", config=config)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["restarted"], result["restart_errors"]), (["replay"], []))
        self.assertIn("instant replay restarted", restart_note(result))
        self.assertEqual(h.status()["replay"]["state"], "on")
        h.wait(lambda: len(launches()) == 2)
        args = launches()[-1]
        self.assertEqual((len(launches()), args[args.index("-f") + 1]), (2, "120"))
        # A save that changes nothing about capture leaves replay running.
        result = h.rpc("save_config", config=h.rpc("config")["config"])
        self.assertEqual((result.get("restarted", []), len(launches())), ([], 2))
        # So does one of the settings read when needed (tones, setup completion):
        # the settings window saves each change on its own.
        config = h.rpc("config")["config"]
        config["recording"]["sound_feedback"] = not config["recording"]["sound_feedback"]
        config["recording"]["configured"] = True
        result = h.rpc("save_config", config=config)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result.get("restarted", []), len(launches())), ([], 2))

    def test_automatic_bitrate_matches_the_rust_controller(self):
        from jarvis.encoding import auto_bitrate, with_auto_bitrate
        cases = [("av1", 60, "native", 2560, 1440, 17), ("av1", 120, "native", 2560, 1440, 33),
                 ("av1", 60, "1080", 2560, 1440, 9), ("av1", 60, "2160", 2560, 1440, 17),
                 ("h264", 60, "native", 2560, 1440, 24), ("av1", 30, "720", 2560, 1440, 4),
                 ("av1", 60, "native", 3840, 2160, 37)]   # same table as capture.rs
        for codec, fps, resolution, width, height, mbps in cases:
            self.assertEqual(auto_bitrate({"codec": codec, "fps": fps, "resolution": resolution}, width, height), mbps)
        display = {"width": 2560, "height": 1440}
        self.assertEqual(with_auto_bitrate({"codec": "av1", "fps": 60, "bitrate_auto": True, "bitrate_mbps": 8}, display)["bitrate_mbps"], 17)
        self.assertEqual(with_auto_bitrate({"codec": "av1", "fps": 60, "bitrate_auto": False, "bitrate_mbps": 8}, display)["bitrate_mbps"], 8)

    def test_sdr_gpu_uses_gpu_backend_with_explicit_audio(self):
        for codec in GPU_CODECS:
            cfg = validate({"recording": {"configured": True, "monitor": "DP-1", "codec": codec,
                           "system_audio": True, "audio_device": "game.monitor", "rate_control": "bitrate"}})
            with patch("jarvis.recorder.sources", return_value=[{"name": "DP-1"}]), \
                    patch("jarvis.recorder.audio_sources", return_value=[{"name": "game.monitor", "monitor": True}]), \
                    patch("jarvis.recorder.shutil.which", side_effect=lambda name: "/fake/" + name):
                active, _, args = Recorder(cfg)._validate()
            self.assertTrue(active["gpu"])
            self.assertEqual(args[0], "/fake/gpu-screen-recorder")
            self.assertEqual(args[args.index("-k") + 1], codec)
            self.assertEqual(args[args.index("-a") + 1], "game.monitor")

    def test_cpu_bitrate_mode_uses_bounded_encoder_buffer_and_keeps_thread_limit(self):
        cfg = validate({"recording": {"configured": True, "monitor": "DP-1", "rate_control": "bitrate", "bitrate_mbps": 6,
                                      "bitrate_auto": False}})
        with patch("jarvis.recorder.sources", return_value=[{"name": "DP-1"}]), \
                patch("jarvis.recorder.shutil.which", side_effect=lambda name: "/fake/" + name):
            active, _, args = Recorder(cfg)._validate()
        self.assertFalse(active["gpu"])
        for param in ("b=6000000", "maxrate=6000000", "bufsize=12000000", "threads=2"):
            self.assertIn(param, args)
        self.assertFalse(any(p.startswith("crf=") for p in args))

    def test_wrong_codec_cannot_be_announced_as_the_selected_gpu_format(self):
        from jarvis.recorder import RecorderError, verify_video
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clip.mkv"
            path.write_bytes(b"fake")
            probe = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360}]}
            with patch("jarvis.recorder._query", return_value=json.dumps(probe)):
                with self.assertRaisesRegex(RecorderError, "expected av1"):
                    verify_video(path, codec="av1")
                verify_video(path, codec="h264")

    def test_ten_bit_formats_are_verified_as_ten_bit(self):
        from jarvis.recorder import RecorderError, verify_video
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clip.mkv"
            path.write_bytes(b"fake")
            for pixel in ("yuv420p", "yuv420p10le"):
                probe = {"streams": [{"codec_type": "video", "codec_name": "av1", "width": 640, "height": 360, "pix_fmt": pixel}]}
                with self.subTest(pixel=pixel), patch("jarvis.recorder._query", return_value=json.dumps(probe)):
                    if pixel == "yuv420p":
                        with self.assertRaisesRegex(RecorderError, "expected 10-bit av1"):
                            verify_video(path, codec="av1_10bit")
                    else:
                        verify_video(path, codec="av1_10bit")
                    verify_video(path, codec="av1")

    def test_native_gpu_record_and_replay_use_same_budget(self):
        from tests.test_rust_controller import BINARY, Harness
        if not BINARY.exists():
            self.skipTest("build Rust controller")
        h = Harness(dry_run=False, JARVIS_TEST_CODEC="av1", config_patch={"recording": {"codec": "av1", "fps": 24, "rate_control": "bitrate", "bitrate_mbps": 6,
                                                                               "bitrate_auto": False}})
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
