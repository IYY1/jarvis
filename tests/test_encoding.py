"""Recording options are exercised with fake captures and private fixture IPC."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from murmur.config import validate
from murmur.encoding import CODECS, GPU_CODECS, gpu_video_args, storage_estimate
from murmur.recorder import Recorder
from murmur.replay import command


class EncodingTests(unittest.TestCase):
    def test_old_settings_keep_software_quality_and_new_options_are_validated(self):
        r = validate({})["recording"]
        self.assertEqual((r["codec"], r["quality"], r["rate_control"]), ("libx264", 23, "quality"))
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
        from murmur.encoding import replay_video
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

    def test_saving_restarts_only_running_captures_whose_settings_changed(self):
        from murmur import client
        old = validate({"recording": {"codec": "av1", "fps": 60}})
        calls = []
        def fake(op, **payload):
            calls.append(op)
            if op == "config":
                return {"ok": True, "config": old}
            if op == "status":
                return {"ok": True, "status": {"recording": {"state": "recording"}, "replay": {"state": "on"}}}
            return {"ok": True}
        new = validate({"recording": {"codec": "av1", "fps": 120}})
        with patch("murmur.client.request", side_effect=fake):
            result = client.save_config_restarting(new)
        self.assertEqual(calls, ["config", "status", "record_stop", "replay_stop", "save_config", "record_start", "replay_start"])
        self.assertIn("a new one started", client.restart_note(result))
        # Replay-only change with its own settings: only replay restarts.
        calls.clear()
        replay_only = validate({"recording": {"codec": "av1", "fps": 60}, "replay": {"own_video": True, "fps": 144}})
        with patch("murmur.client.request", side_effect=fake):
            client.save_config_restarting(replay_only)
        self.assertEqual(calls, ["config", "status", "replay_stop", "save_config", "replay_start"])
        # Nothing capture-related changed: nothing stops.
        calls.clear()
        with patch("murmur.client.request", side_effect=fake):
            self.assertEqual(client.restart_note(client.save_config_restarting(old)), "")
        self.assertEqual(calls, ["config", "status", "save_config"])

    def test_sdr_gpu_uses_gpu_backend_with_explicit_audio(self):
        for codec in GPU_CODECS:
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

    def test_ten_bit_formats_are_verified_as_ten_bit(self):
        from murmur.recorder import RecorderError, verify_video
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clip.mkv"
            path.write_bytes(b"fake")
            for pixel in ("yuv420p", "yuv420p10le"):
                probe = {"streams": [{"codec_type": "video", "codec_name": "av1", "width": 640, "height": 360, "pix_fmt": pixel}]}
                with self.subTest(pixel=pixel), patch("murmur.recorder._query", return_value=json.dumps(probe)):
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
