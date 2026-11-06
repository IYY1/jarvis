"""Recorder ownership/finalization tests. No display, microphone, or capture is used."""

import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import unittest
from unittest.mock import patch

from jarvis.recorder import Recorder, RecorderError, sources, verify_video


class FakeProcess:
    def __init__(self, argv, *, crash=False, empty=False, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        self.stderr = None
        self.code = 7 if crash else 0
        self.exited = threading.Event()
        self.signals = []
        self.path = Path(argv[argv.index("--file" if "--file" in argv else "-o") + 1])
        if not empty:
            self.path.write_bytes(b"test-video")
        if crash:
            self.exited.set()

    def wait(self):
        self.exited.wait(3)
        return self.code

    def send_signal(self, sig):
        self.signals.append(sig)
        self.exited.set()

    def terminate(self):
        self.code = -signal.SIGTERM
        self.exited.set()

    def kill(self):
        self.code = -signal.SIGKILL
        self.exited.set()


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = {"configured": True, "monitor": "DP-1", "fps": 30,
                       "quality": 23, "output_dir": self.directory.name}
        self.patches = [patch("jarvis.recorder.shutil.which", side_effect=lambda x: "/usr/bin/" + x),
                        patch("jarvis.recorder.sources", return_value=[{"name": "DP-1"}]),
                        patch("jarvis.recorder._query", return_value=json.dumps(
                            {"streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}))]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_start_stop_finalizes_private_file_after_sigint(self):
        children = []
        def spawn(argv, **kwargs):
            process = FakeProcess(argv, **kwargs)
            children.append(process)
            return process
        with patch("jarvis.recorder.subprocess.Popen", side_effect=spawn):
            recorder = Recorder(self.config)
            initial = recorder.start()
            self.assertEqual(initial["state"], "recording")
            self.assertTrue(initial["file"].endswith(".part.mkv"))
            self.assertEqual(os.stat(initial["file"]).st_mode & 0o777, 0o600)
            self.assertEqual(recorder.start(), initial)
            self.assertEqual(len(children), 1)
            result = recorder.stop()
            self.assertEqual(children[0].signals, [signal.SIGINT])
            self.assertEqual(result["state"], "idle")
            self.assertTrue(Path(result["file"]).is_file())
            self.assertFalse(Path(initial["file"]).exists())
            self.assertEqual(children[0].kwargs["stdin"], -3)  # DEVNULL; cannot request input
            self.assertTrue(children[0].kwargs["start_new_session"])
            self.assertNotIn("shell", children[0].kwargs)
            self.assertIn("--output", children[0].argv)
            self.assertIn("matroska", children[0].argv)
            self.assertNotIn("--audio", children[0].argv)
            self.assertEqual(recorder.stop(), result)

    def test_unconfigured_does_not_spawn_or_create_files(self):
        self.config["configured"] = False
        with patch("jarvis.recorder.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(RecorderError, "finish"):
                Recorder(self.config).start()
            spawn.assert_not_called()
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_disconnected_source_fails_before_capture(self):
        self.config["monitor"] = "missing"
        with self.assertRaisesRegex(RecorderError, "unavailable"):
            Recorder(self.config).start()

    def test_missing_backend_is_actionable(self):
        with patch("jarvis.recorder.shutil.which", return_value=None):
            with self.assertRaisesRegex(RecorderError, "wf-recorder is missing"):
                Recorder(self.config).start()

    def test_crash_retains_partial_and_never_reports_saved(self):
        with patch("jarvis.recorder.subprocess.Popen", side_effect=lambda a, **kw: FakeProcess(a, crash=True, **kw)):
            recorder = Recorder(self.config)
            recorder.start()
            self.assertTrue(recorder._done.wait(2))
            status = recorder.status()
            self.assertEqual(status["state"], "error")
            self.assertIn("status 7", status["message"])
            self.assertTrue(Path(status["file"]).exists())
            self.assertTrue(status["file"].endswith(".part.mkv"))
            self.assertEqual(list(Path(self.directory.name).glob("Jarvis_*.mkv")), [])
            with self.assertRaisesRegex(RecorderError, "status 7"):
                recorder.stop()
            recorder.close()  # shutdown does not re-raise an already observed, inactive failure

    def test_vaapi_does_not_force_software_frame_format(self):
        self.config.update(codec="h264_vaapi", vaapi_device="/dev/dri/renderD128")
        with patch("jarvis.recorder.os.access", return_value=True):
            _, _, argv = Recorder(self.config)._validate()
        self.assertNotIn("--pixel-format", argv)
        self.assertNotIn("--filter", argv)  # backend adapts hwupload to compositor device
        self.assertIn("qp=23", argv)
        self.assertIn("/dev/dri/renderD128", argv)

    def test_hdr_recording_uses_gpu_backend_and_verifies_before_publishing(self):
        probe = {"streams": [{"codec_type": "video", "width": 1920, "height": 1080,
                              "pix_fmt": "yuv420p10le", "color_transfer": "smpte2084",
                              "color_space": "bt2020nc", "color_primaries": "bt2020", "color_range": "tv"}]}
        children = []
        def spawn(argv, **kwargs):
            children.append(FakeProcess(argv, **kwargs))
            return children[-1]
        with patch("jarvis.recorder.sources", return_value=[{"name": "DP-1", "colorManagementPreset": "hdr"}]), \
                patch("jarvis.recorder._query", return_value=json.dumps(probe)), \
                patch("jarvis.recorder.subprocess.Popen", side_effect=spawn):
            recorder = Recorder(self.config)
            initial = recorder.start()
            self.assertIn("HDR", initial["message"])
            argv = children[0].argv
            self.assertIn("/usr/bin/gpu-screen-recorder", argv)
            self.assertEqual(argv[argv.index("-k") + 1], "hevc_hdr")
            self.assertEqual(argv[argv.index("-c") + 1], "mkv")
            self.assertEqual(argv[argv.index("-w") + 1], "DP-1")
            self.assertNotIn("--pixel-format", argv)
            self.assertEqual(recorder.stop()["state"], "idle")
            self.assertFalse(Path(initial["file"]).exists())

    def test_hdr_silent_sdr_fallback_is_retained_as_partial(self):
        self.config["color_mode"] = "hdr"
        with patch("jarvis.recorder.subprocess.Popen", side_effect=FakeProcess):
            recorder = Recorder(self.config)
            initial = recorder.start()
            with self.assertRaisesRegex(RecorderError, "HDR verification failed"):
                recorder.stop()
            self.assertTrue(Path(initial["file"]).exists())
            self.assertEqual(list(Path(self.directory.name).glob("Jarvis_*.mkv")), [])

    def test_hdr_recording_preserves_explicit_audio_source(self):
        self.config.update(color_mode="hdr", system_audio=True, audio_device="game.monitor")
        with patch("jarvis.recorder.audio_sources", return_value=[{"name": "game.monitor", "monitor": True}]):
            _, _, argv = Recorder(self.config)._validate()
        self.assertEqual(argv[argv.index("-a") + 1], "game.monitor")
        self.assertNotIn("default_input", argv)

    def test_missing_hdr_backend_never_falls_back_to_sdr(self):
        self.config["color_mode"] = "hdr"
        with patch("jarvis.recorder.shutil.which", side_effect=lambda name: None if name == "gpu-screen-recorder" else "/usr/bin/" + name), \
                patch("jarvis.recorder.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(RecorderError, "gpu-screen-recorder is missing"):
                Recorder(self.config).start()
            spawn.assert_not_called()
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_clean_exit_with_empty_file_is_error(self):
        with patch("jarvis.recorder.subprocess.Popen", side_effect=lambda a, **kw: FakeProcess(a, empty=True, **kw)):
            recorder = Recorder(self.config)
            recorder.start()
            with self.assertRaisesRegex(RecorderError, "no video data"):
                recorder.stop()
            self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_failure_to_spawn_removes_reserved_staging_file(self):
        with patch("jarvis.recorder.subprocess.Popen", side_effect=OSError("cannot execute")):
            with self.assertRaisesRegex(RecorderError, "cannot execute"):
                Recorder(self.config).start()
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_rejects_unsupported_or_unbounded_settings(self):
        for key, value in [("codec", "arbitrary"), ("fps", 100000), ("fps", True), ("quality", -1)]:
            with self.subTest(key=key, value=value):
                cfg = dict(self.config, **{key: value})
                with self.assertRaises(RecorderError):
                    Recorder(cfg).start()

    def test_combined_audio_requires_explicit_source_setup(self):
        self.config.update(system_audio=True, microphone=True, audio_device="combined")
        with self.assertRaisesRegex(RecorderError, "one audio source"):
            Recorder(self.config).start()

    def test_audio_source_type_is_checked(self):
        self.config.update(system_audio=True, audio_device="mic")
        with patch("jarvis.recorder.audio_sources", return_value=[{"name": "mic", "monitor": False}]):
            with self.assertRaisesRegex(RecorderError, "sink monitor"):
                Recorder(self.config).start()

    def test_output_validation_requires_video_stream(self):
        path = Path(self.directory.name) / "test.mkv"
        path.write_bytes(b"audio-only")
        with patch("jarvis.recorder._query", return_value='{"streams":[{"codec_type":"audio"}]}'):
            with self.assertRaisesRegex(RecorderError, "no valid video"):
                verify_video(path)

    def test_callback_failure_does_not_orphan_capture(self):
        def broken_callback(_status):
            raise ValueError("UI disconnected")
        with patch("jarvis.recorder.subprocess.Popen", side_effect=FakeProcess):
            recorder = Recorder(self.config, on_change=broken_callback)
            recorder.start()
            self.assertEqual(recorder.stop()["state"], "idle")


class SourceTests(unittest.TestCase):
    def test_monitor_list_ignores_disabled_sources(self):
        with patch("jarvis.recorder._query", return_value='[{"name":"DP-1"},{"name":"DP-2","disabled":true}]'):
            self.assertEqual([x["name"] for x in sources()], ["DP-1"])

    def test_invalid_monitor_list_has_clear_error(self):
        for invalid in ('{"error":"no socket"}', 'not json'):
            with patch("jarvis.recorder._query", return_value=invalid):
                with self.assertRaises(RecorderError):
                    sources()


if __name__ == "__main__":
    unittest.main()
