"""Windows policies run on Linux too; no live audio, desktop actions or services."""

import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import struct
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from jarvis import config, host
from jarvis.recorder import RecorderError
from jarvis.windows import encoding, transport
from jarvis.windows.audio import PCMStream, select_device
from jarvis.windows.apps import WindowsApp
from jarvis.windows.capture import Replay, ReplayBuffer

try:
    import tkinter  # noqa: F401  (the Windows settings window needs Tk)
    TK = True
except ImportError:
    TK = False


@unittest.skipUnless(TK, "Tk is not installed")
class WindowRequestTests(unittest.TestCase):
    def test_requests_keep_the_client_per_operation_timeouts(self):
        from jarvis.windows.ui import Window
        expected = {"execute": 45, "record_start": 20, "status": 15,
                    "record_stop": 60, "save_config": 60}
        for op, timeout in expected.items():
            with self.subTest(op=op):
                runs = []
                window = types.SimpleNamespace(submit=lambda run, callback: runs.append(run), received=None)
                Window.call(window, op)
                with patch("jarvis.windows.ui.request", return_value={"ok": True}) as request:
                    runs[0]()
                self.assertEqual(request.call_args.kwargs["timeout"], timeout)


@unittest.skipUnless(TK, "Tk is not installed")
class SettingsWindowTests(unittest.TestCase):
    """The redesigned window: sidebar pages, settings that save on their own, Wada colors."""

    def setUp(self):
        import tkinter
        try:
            self.root = tkinter.Tk()
        except tkinter.TclError:
            self.skipTest("no display for Tk")
        self.root.withdraw()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        environment = patch.dict(os.environ, {"JARVIS_CONFIG": str(Path(folder.name) / "config.json")})
        environment.start()
        self.addCleanup(environment.stop)
        offline = patch("jarvis.windows.ui.request", side_effect=OSError("service offline"))
        offline.start()
        self.addCleanup(offline.stop)
        from jarvis.windows.ui import Window
        with patch.object(Window, "observe", lambda _window: None):   # no status watcher
            self.window = Window(self.root)
        self.addCleanup(self.root.destroy)

    def finish(self):
        """Let queued work end and deliver its results, as the Tk loop would."""
        self.window.executor.shutdown(wait=True)
        while not self.window.events.empty():
            callback, result, error = self.window.events.get_nowait()
            self.assertIsNone(error)
            if callback:
                callback(result)

    def test_every_page_opens_and_older_links_still_work(self):
        from jarvis.windows.ui import NAVIGATION
        for key, _title in NAVIGATION:
            self.window.show(key)
            self.assertEqual(self.window.current, key)
        for old, new in (("activation", "voice"), ("replay", "recording"), ("privacy", "general"), ("nope", "dashboard")):
            self.window.show(old)
            self.assertEqual(self.window.current, new)

    def test_clips_only_this_survives_late_silence_detection(self):
        from jarvis.clipedit import Source
        panel = self.window.clips
        pending = []
        panel.work = lambda run, done: pending.append((run, done))
        source = Source("clip.mkv", 3, {}, [
            {"name": "Firefox", "codec_name": "opus"},
            {"name": "Discord", "codec_name": "opus"}], [0])
        panel.loaded((source, None))
        panel.only(1)
        pending[0][1](([-91, -91], None))
        self.assertEqual(panel.selection(), [1], "a late scan must keep the user's choice")
        panel.keep_all(False)
        self.assertEqual(panel.selection(), [])
        panel.keep_all(True)
        self.assertEqual(panel.selection(), [0, 1])

    def test_clips_export_keeps_discord_and_mutes_firefox(self):
        import hashlib
        import shutil
        import subprocess
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("needs FFmpeg")
        with tempfile.TemporaryDirectory(prefix="jarvis-clip-ui-") as folder:
            path = Path(folder) / "clip's العربية.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=160x90:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
                "-t", "1", "-map", "0:v", "-map", "1:a", "-map", "2:a",
                "-c:v", "libx264", "-threads", "1", "-c:a", "aac", str(path)], check=True)
            (path.parent / f".{path.name}.tracks.json").write_text(json.dumps({"tracks": ["Firefox", "Discord"]}))
            original = hashlib.sha256(path.read_bytes()).hexdigest()
            panel = self.window.clips
            panel.work = lambda run, done: done((run(), None))
            panel.open(path)
            panel.only(1)
            panel.export_copy()
            exported = next(path.parent.glob("* edit 1.mkv"))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)
            data = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(exported)],
                                             capture_output=True, text=True, check=True).stdout)
            self.assertEqual(len([s for s in data["streams"] if s["codec_type"] == "audio"]), 1)
            def sound(file, track):
                return subprocess.run(["ffmpeg", "-v", "error", "-i", str(file), "-map", f"0:a:{track}",
                                       "-f", "hash", "-"], capture_output=True, check=True).stdout
            self.assertEqual(sound(exported, 0), sound(path, 1))
            self.assertNotEqual(sound(exported, 0), sound(path, 0))
            panel.keep_all(True)
            panel.mix.set(True)
            panel.export_copy()
            mixed = next(path.parent.glob("* edit 2.mkv"))
            metadata = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(mixed)],
                                                 capture_output=True, text=True, check=True).stdout)
            audio = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
            self.assertEqual(len(audio), 1)
            self.assertEqual(audio[0]["codec_name"], "aac")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)



    def test_windows_gpu_format_button_reports_all_probed_formats(self):
        display = {'name': self.window.variable('recording', 'monitor').get(), 'width': 2560, 'height': 1440}
        immediate = lambda **kw: types.SimpleNamespace(start=kw['target'])
        with patch('jarvis.windows.displays.sources', return_value=[display]), \
             patch('jarvis.windows.encoding.select_encoder', return_value=('ffmpeg', 'amf')) as probe, \
             patch('jarvis.windows.video_settings.threading.Thread', side_effect=immediate):
            self.window.video.test_formats()
        self.finish()
        self.assertEqual(len(probe.call_args_list), 5)
        self.assertTrue(self.window.video.formats['av1_10bit'])
        self.assertIn('Passed GPU tests', self.window.video.info.get())

    def test_start_recording_saves_reviewed_settings_before_capture(self):
        self.window.online = True
        self.window.variable('recording', 'fps').set('240')
        calls = []
        with patch('jarvis.windows.ui.save_config_restarting', side_effect=lambda value: calls.append(('save', value)) or {'ok': True}), \
             patch('jarvis.windows.ui.request', side_effect=lambda op, **kw: calls.append((op, kw)) or {'ok': True}):
            self.window.start_recording()
            self.finish()
        self.assertEqual([c[0] for c in calls], ['save', 'record_start'])
        self.assertTrue(calls[0][1]['recording']['configured'])
        self.assertEqual(calls[0][1]['recording']['fps'], 240)

    def test_windows_240fps_preset_and_replay_profile_save_independently(self):
        video = self.window.video
        video.formats = {"av1": True, "av1_10bit": True}
        video.presets['recording'].set('High frame rate · 240 fps')
        video.apply('recording')
        self.window.variable('replay', 'own_video').set(True)
        video.presets['replay'].set('Small files · 1080p')
        video.apply('replay')
        self.window.save()
        self.finish()
        saved = config.load()
        self.assertEqual(saved['recording']['fps'], 240)
        self.assertEqual(saved['recording']['codec'], 'av1')
        self.assertTrue(saved['recording']['bitrate_auto'])
        self.assertTrue(saved['replay']['own_video'])
        self.assertEqual((saved['replay']['fps'], saved['replay']['resolution']), (30, '1080'))

    def test_windows_arabic_dictation_controls_save_and_dispatch(self):
        self.window.arabic_enabled.set(True)
        with patch('jarvis.windows.voice_controls.models.arabic_model_ready', return_value=True):
            self.window.voice_controls.toggle_arabic()
        self.window.variable('dictation', 'language').set('ar')
        self.window.voice_controls.stop_phrases.set('stop dictation, وقف الكتابة')
        self.window.variable('privacy', 'onboarded').set(True)
        self.window.save()
        self.finish()
        saved = config.load()
        self.assertEqual(saved['recognition']['language'], 'ar_en')
        self.assertEqual(saved['dictation']['language'], 'ar')
        self.assertEqual(saved['dictation']['stop_phrases'], ['stop dictation', 'وقف الكتابة'])
        with patch.object(self.window, 'call') as call:
            self.window.voice_controls.toggle()
        call.assert_called_once_with('dictation', action='toggle')

    def test_windows_clip_sections_split_remove_join_and_undo(self):
        from jarvis.clipedit import Source
        panel = self.window.clips
        panel.work = lambda run, done: None
        source = Source('clip.mkv', 10, {}, [], [0, 2, 4, 6, 8])
        panel.loaded((source, None))
        panel.start.set('2'); panel.end.set('4'); panel.change('remove')
        self.assertAlmostEqual(panel.edit.duration, 8)
        panel.change('undo')
        self.assertAlmostEqual(panel.edit.duration, 10)
        panel.start.set('5'); panel.end.set('10'); panel.change('split')
        self.assertEqual(len(panel.edit.segments), 2)
        panel.edit.append(source); panel.edited()
        self.assertAlmostEqual(panel.edit.duration, 20)


    def test_a_change_saves_on_its_own_while_the_service_is_off(self):
        self.window.variable("replay", "sound").set(False)
        self.window.variable("replay", "seconds").set("300")
        self.window.save()
        self.finish()
        saved = config.load()
        self.assertEqual((saved["replay"]["sound"], saved["replay"]["seconds"]), (False, 300))
        self.assertIn("Saved.", self.window.notice.get())

    def test_shown_values_and_invalid_ones_are_not_saved(self):
        self.window.loading = True
        self.window.changed(0)
        self.assertIsNone(self.window.save_timer)
        self.window.loading = False
        self.window.variable("replay", "seconds").set("ten")
        self.window.save()
        self.assertIn("Not saved", self.window.notice.get())

    def test_shortcuts_are_a_list_not_json(self):
        self.window.new_phrase.set("Capture That")
        self.window.new_command.set("start recording")
        self.window.add_shortcut()
        self.finish()
        self.assertEqual(config.load()["commands"], {"capture that": "start recording"})

    def test_color_presets_restyle_the_window(self):
        from jarvis import palettes
        self.window.choose_palette("190")
        self.assertEqual(self.window.style.lookup("TFrame", "background"), palettes.roles("190")["bg"])
        self.assertEqual(palettes.chosen(), "190")


class EncoderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.validate({})["recording"]
        self.cfg.update(codec="h264", rate_control="bitrate", bitrate_mbps=8)
        self.display = {
            "name": r"\\.\DISPLAY2",
            "adapter": 1,
            "output": 2,
            "vendor": 0x10DE,
            "hdr_known": True,
            "colorManagementPreset": "srgb",
        }

    def test_families_use_budget_and_never_implicit_software_fallback(self):
        for family in ("nvenc", "amf", "qsv", "mf", "cpu"):
            args = encoding.encoder_options(self.cfg, family, "h264")
            self.assertEqual(args[args.index("-b:v") + 1], "8000000")
            if family == "mf":
                self.assertEqual(args[args.index("-hw_encoding") + 1], "1")
            if family == "cpu":
                self.assertIn("libx264", args)

    def test_capture_uses_dxgi_adapter_and_output_not_monitor_number(self):
        args, color, codec = encoding.command(
            self.cfg, self.display, "ffmpeg.exe", "nvenc"
        )
        self.assertIn("d3d11va=capture:1", args)
        self.assertIn(
            "ddagrab=output_idx=2:framerate=30:output_fmt=8bit:allow_fallback=0", args
        )
        self.assertNotIn("-vf", args)
        self.assertEqual((color, codec), ("sdr", "h264"))

    def test_audio_is_only_added_for_explicit_pipe(self):
        args, _, _ = encoding.command(
            self.cfg,
            self.display,
            "ffmpeg.exe",
            "amf",
            audio_pipes=[(r"\\.\pipe\Jarvis-test", None)],
        )
        self.assertIn("s16le", args)
        self.assertIn("aac", args)
        self.assertEqual(args.count("-i"), 2)
        self.assertNotIn("-metadata:s:a:0", args)

    def test_separate_replay_tracks_get_their_own_inputs_and_titles(self):
        replay = config.validate({"replay": {"audio_tracks": "separate", "separate_apps": ["firefox"]}})["replay"]
        cfg = dict(self.cfg, system_audio=True, microphone=True)
        plan = encoding.audio_plan(cfg, replay, build=22631)
        self.assertEqual([title for title, _ in plan], ["Everything else", "firefox", "Microphone"])
        self.assertEqual([c["track"] for _, c in plan],
                         [{"kind": "game", "app": "firefox"}, {"kind": "app", "app": "firefox"}, {"kind": "microphone"}])
        pipes = [(rf"\\.\pipe\Jarvis-t-audio{i}", title) for i, (title, _) in enumerate(plan)]
        args, _, _ = encoding.command(self.cfg, self.display, "ffmpeg.exe", "amf", audio_pipes=pipes)
        self.assertEqual(args.count("-i"), 4)
        for i in range(3):
            self.assertIn(f"{i + 1}:a:0", args)
        self.assertEqual(args[args.index("-metadata:s:a:1") + 1], "title=firefox")
        # Without a program, the game track is all system sound.
        alone = dict(replay, separate_apps=[])
        self.assertEqual(encoding.audio_plan(cfg, alone, build=22631)[0][1]["track"], {"kind": "game", "app": ""})
        # Mixed replays and recordings keep one input with the existing mix.
        self.assertEqual(encoding.audio_plan(cfg, dict(replay, audio_tracks="mixed"), build=19045), [(None, cfg)])
        self.assertEqual(encoding.audio_plan(cfg), [(None, cfg)])

    def test_separate_tracks_refuse_windows_10_and_several_programs(self):
        replay = config.validate({"replay": {"audio_tracks": "separate", "separate_apps": ["firefox"]}})["replay"]
        with self.assertRaisesRegex(RecorderError, "Windows 11"):
            encoding.audio_plan(self.cfg, replay, build=19045)
        with self.assertRaisesRegex(RecorderError, "one program"):
            encoding.audio_plan(self.cfg, dict(replay, separate_apps=["firefox", "spotify"]), build=22631)

    def test_app_audio_is_relayed_and_its_error_reported(self):
        from jarvis.windows import audio
        import subprocess
        import sys
        popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as folder:
            # A Python stand-in for jarvis-voice.exe, so this runs on Linux and Windows.
            worker = Path(folder) / "jarvis-voice.py"
            worker.write_text(
                "import sys\n"
                "open(sys.argv[0] + '.request', 'w').write(sys.stdin.readline())\n"
                "sys.stdout.buffer.write(b'PCMDATA!'); sys.stdout.flush()\n"
                "sys.stderr.write('{\"event\":\"error\",\"message\":\"firefox is not running.\"}\\n')\n"
                "sys.exit(1)\n")
            written = []
            pipe = MagicMock()
            pipe.return_value.__enter__.return_value.write.side_effect = written.append
            with patch("jarvis.native_worker.binary_path", return_value=worker), \
                    patch("jarvis.windows.pipes.PipeWriter", pipe), \
                    patch("subprocess.Popen", side_effect=lambda argv, **kw: popen([sys.executable, *argv], **kw)):
                with self.assertRaisesRegex(RuntimeError, "firefox is not running"):
                    audio.track_to_pipe({"track": {"kind": "game", "app": "firefox"}}, r"\\.\pipe\Jarvis-x")
            self.assertEqual(b"".join(written), b"PCMDATA!")
            self.assertEqual(json.loads((Path(folder) / "jarvis-voice.py.request").read_text()),
                             {"process": "firefox", "include": False})

    def test_audio_failure_names_the_track_and_reason(self):
        from jarvis.windows.capture import audio_failure
        process = types.SimpleNamespace(stderr=io.BytesIO(
            b"Traceback ...\nRuntimeError: firefox is not running. Start it before instant replay.\n"))
        self.assertEqual(audio_failure(process, "firefox"),
                         "The firefox track stopped: firefox is not running. Start it before instant replay.")
        self.assertIn("unexpectedly", audio_failure(types.SimpleNamespace(stderr=io.BytesIO(b"")), None))

    def test_240fps_scaled_ten_bit_sdr_is_explicit_and_verified(self):
        self.cfg.update(codec='av1_10bit', fps=240, resolution='1080')
        self.display.update(width=3840, height=2160)
        args, color, codec = encoding.command(self.cfg, self.display, 'ffmpeg.exe', 'amf')
        self.assertTrue(any('framerate=240' in x for x in args))
        filters = args[args.index('-vf') + 1]
        self.assertIn('scale=1920:1080', filters)
        self.assertIn('format=p010le', filters)
        self.assertEqual(args[args.index('-pix_fmt') + 1], 'p010le')
        self.assertEqual((color, codec), ('sdr', 'av1_10bit'))
        self.assertEqual(args[args.index('-c:v') + 1], 'av1_amf')

    def test_resolution_limits_preserve_portrait_shape_and_never_upscale(self):
        self.assertEqual(encoding.output_size({'resolution': '1080'}, {'width': 1080, 'height': 1920}), (606, 1080))
        self.assertEqual(encoding.output_size({'resolution': '2160'}, {'width': 1280, 'height': 720}), (1280, 720))

    def test_hdr_promotes_h264_and_refuses_known_sdr_display(self):
        self.cfg["color_mode"] = "hdr"
        with self.assertRaises(RecorderError):
            encoding.command(self.cfg, self.display, "ffmpeg.exe", "nvenc")
        self.display["colorManagementPreset"] = "hdr"
        args, color, codec = encoding.command(
            self.cfg, self.display, "ffmpeg.exe", "nvenc"
        )
        self.assertEqual((color, codec), ("hdr", "hevc"))
        for metadata in ("bt2020", "smpte2084", "bt2020nc"):
            self.assertIn(metadata, args)
        self.assertTrue(any("output_fmt=10bit" in arg for arg in args))

    def test_auto_never_guesses_unknown_hdr_state(self):
        self.display["hdr_known"] = False
        with self.assertRaises(RecorderError):
            encoding.command(self.cfg, self.display, "ffmpeg.exe", "nvenc")

    def test_auto_prefers_capture_adapter_vendor_then_hardware_mf(self):
        with (
            patch.object(encoding, "tool", return_value=__file__),
            patch.object(encoding, "_probe", side_effect=[False, True]) as probe,
        ):
            _, family = encoding.select_encoder(self.cfg, self.display, "h264")
        self.assertEqual(family, "mf")
        self.assertEqual(
            [call.args[2] for call in probe.call_args_list], ["nvenc", "mf"]
        )

    def test_no_driver_does_not_silently_switch_to_cpu(self):
        with (
            patch.object(encoding, "tool", return_value=__file__),
            patch.object(encoding, "_probe", return_value=False) as probe,
        ):
            with self.assertRaises(RecorderError):
                encoding.select_encoder(self.cfg, self.display, "h264")
        self.assertNotIn("cpu", [call.args[2] for call in probe.call_args_list])

    def test_cpu_choice_is_explicit_and_sdr_h264_only(self):
        self.cfg["encoder"] = "cpu"
        with (
            patch.object(encoding, "tool", return_value=__file__),
            patch.object(encoding, "_probe", return_value=True) as probe,
        ):
            self.assertEqual(
                encoding.select_encoder(self.cfg, self.display, "h264")[1], "cpu"
            )
            self.assertEqual(probe.call_args.args[2], "cpu")
            with self.assertRaises(RecorderError):
                encoding.select_encoder(self.cfg, self.display, "hevc")


class BufferTests(unittest.TestCase):
    def test_out_of_order_pipes_are_reassembled_in_capture_order(self):
        buffer = ReplayBuffer(10)
        buffer.append(b"b", 1)
        buffer.append(b"c", 2)
        with self.assertRaises(RecorderError):
            buffer.snapshot()
        buffer.append(b"a", 0)
        self.assertEqual(buffer.snapshot(), (b"a", b"b", b"c"))

    def test_duration_and_ram_caps_evict_oldest(self):
        buffer = ReplayBuffer(2, max_bytes=5)
        for index, block in enumerate((b"aa", b"bb", b"ccc")):
            buffer.append(block, index)
        self.assertEqual(buffer.snapshot(), (b"bb", b"ccc"))
        self.assertEqual(buffer.size, 5)
        buffer.append(b"dddd", 3)
        self.assertEqual(buffer.snapshot(), (b"dddd",))

    def test_snapshot_shares_immutable_blocks_and_survives_eviction(self):
        buffer = ReplayBuffer(1)
        block = bytes(bytearray(b"x" * 20))
        buffer.append(block, 0)
        saved = buffer.snapshot()
        buffer.append(b"new", 1)
        self.assertIs(saved[0], block)
        self.assertEqual(saved, (block,))

    def test_duplicates_large_gaps_and_oversized_segments_fail(self):
        for sequence in (-1, 3):
            with self.assertRaises(RecorderError):
                ReplayBuffer(10).append(b"a", sequence)
        buffer = ReplayBuffer(10, max_bytes=2)
        buffer.append(b"a", 0)
        with self.assertRaises(RecorderError):
            buffer.append(b"a", 0)
        with self.assertRaises(RecorderError):
            buffer.append(b"big", 1)

    def test_save_waits_for_current_segment_and_has_a_deadline(self):
        buffer = ReplayBuffer(10)
        buffer.append(b"old", 0)
        after = time.monotonic()
        thread = threading.Thread(
            target=lambda: (time.sleep(0.02), buffer.append(b"current", 1))
        )
        thread.start()
        self.assertEqual(buffer.snapshot(after=after, timeout=1)[-1], b"current")
        thread.join()
        with self.assertRaises(RecorderError):
            buffer.snapshot(after=time.monotonic() + 1, timeout=0.01)


class ReplaySaveTests(unittest.TestCase):
    def test_failed_verification_reports_the_retained_partial_clip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            segment = root / 'segment-0000.mkv'
            segment.write_bytes(b'generated segment')
            capture = Replay(config.validate({}))
            capture._buffer, capture._folder, capture._process = MagicMock(), root, MagicMock()
            capture._buffer.files.return_value.__enter__.return_value = (segment,)

            def remux(argv, **kwargs):
                Path(argv[-1]).write_bytes(b'completed clip retained for inspection')
                return types.SimpleNamespace(returncode=0)

            with patch('jarvis.windows.capture.tool', return_value='fake-ffmpeg'), \
                    patch('jarvis.windows.capture.subprocess.run', side_effect=remux), \
                    patch('jarvis.windows.capture.verify_video', side_effect=RecorderError('invalid HDR metadata')):
                with self.assertRaisesRegex(RecorderError, 'invalid HDR metadata'):
                    capture.save()
            status = capture.status()
            self.assertIsNotNone(status['file'])
            self.assertEqual(Path(status['file']).read_bytes(), b'completed clip retained for inspection')
            self.assertIn('retained', status['message'])


class TransportTests(unittest.TestCase):
    def test_auth_is_removed_and_non_string_or_incorrect_secret_is_rejected(self):
        token = transport.new_token()
        self.assertEqual(len(token), 64)
        payload = {"op": "status", "auth": token}
        self.assertTrue(transport.authenticate(payload, token))
        self.assertNotIn("auth", payload)
        for auth in (None, True, token + "x", "wrong", "é" * 64):
            self.assertFalse(transport.authenticate({"auth": auth}, token))

    def test_endpoint_cannot_redirect_client_to_remote_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "endpoint.json"
            path.write_text(
                json.dumps({"host": "evil.example", "port": 1234, "token": "a" * 64})
            )
            with (
                patch.object(transport, "endpoint_path", return_value=path),
                patch.object(
                    transport.socket, "create_connection", return_value=object()
                ) as connect,
            ):
                transport.connect(2)
            connect.assert_called_once_with(("127.0.0.1", 1234), 2)

    def test_invalid_endpoint_never_connects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "endpoint.json"
            for data in (
                [],
                {},
                {"port": True, "token": "a" * 64},
                {"port": 65536, "token": "a" * 64},
                {"port": 1234, "token": "short"},
            ):
                path.write_text(json.dumps(data))
                with (
                    patch.object(transport, "endpoint_path", return_value=path),
                    patch.object(transport.socket, "create_connection") as connect,
                ):
                    with self.assertRaises((ConnectionError, ValueError)):
                        transport.connect(1)
                    connect.assert_not_called()
            path.write_bytes(b"x" * 1025)
            with patch.object(transport, "endpoint_path", return_value=path):
                with self.assertRaises(ConnectionError):
                    transport.connect(1)


class HostTests(unittest.TestCase):
    def test_new_windows_settings_are_gpu_budgeted_and_require_setup(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(host, "is_windows", return_value=True),
        ):
            value = config.load(Path(directory) / "absent.json")
        self.assertEqual(value["recording"]["codec"], "h264")
        self.assertEqual(value["recording"]["rate_control"], "bitrate")
        self.assertFalse(value["recording"]["configured"])
        self.assertFalse(value["privacy"]["onboarded"])

    def test_existing_settings_are_not_replaced_by_windows_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps({"recording": {"codec": "libx264", "quality": 17}})
            )
            with patch.object(host, "is_windows", return_value=True):
                value = config.load(path)
        self.assertEqual(value["recording"]["codec"], "libx264")
        self.assertEqual(value["recording"]["quality"], 17)

    def test_frozen_workers_use_console_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "JarvisConsole.exe").touch()
            with (
                patch.object(host.sys, "frozen", True, create=True),
                patch.object(host, "app_dir", return_value=root),
            ):
                self.assertEqual(
                    host.command("audio", "pipe"),
                    [str(root / "JarvisConsole.exe"), "--internal", "audio", "pipe"],
                )
                (root / "JarvisConsole.exe").unlink()
                with self.assertRaises(RuntimeError):
                    host.command("voice")

    def test_browser_shortcuts_never_match_every_window_of_the_browser(self):
        plain = WindowsApp("firefox", "Firefox", "", r"C:\Firefox\firefox.exe")
        site = WindowsApp(
            "webapp-id",
            "Web App",
            "",
            r"C:\Firefox\firefox.exe",
            wm_class="--app=example.com",
        )
        self.assertIn(r"c:\firefox\firefox.exe", plain.classes())
        self.assertEqual(site.classes(), {"webapp-id"})

    def test_discord_moderation_is_absent_from_shipped_source(self):
        root = Path(__file__).resolve().parents[1]
        for file in (
            "src/jarvis/discord_control.py",
            "src/jarvis/discord_commands.py",
            "native/controller/src/discord.rs",
        ):
            self.assertFalse((root / file).exists())
        self.assertNotIn("discord", config.DEFAULTS)


class StartupTests(unittest.TestCase):
    """Setup's start-at-sign-in component uses the same entry and setting as Settings."""

    def test_running_service_applies_the_change_itself(self):
        from jarvis.windows import startup
        with patch("jarvis.client.request", return_value={"ok": True}) as request, \
                patch.object(startup, "set_startup") as registry:
            self.assertEqual(startup.configure_startup(True), {"ok": True, "startup": True})
        request.assert_called_once_with("set_startup", enabled=True, timeout=5)
        registry.assert_not_called()

    def test_stopped_service_updates_run_entry_and_saved_setting(self):
        from jarvis.windows import startup
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("os.environ", JARVIS_CONFIG=str(Path(directory) / "config.json")), \
                patch("jarvis.client.request", side_effect=ConnectionRefusedError), \
                patch.object(startup, "set_startup") as registry:
            startup.configure_startup(True)
            self.assertTrue(config.load()["startup"])
            startup.configure_startup(False)
            self.assertFalse(config.load()["startup"])
        self.assertEqual([c.args for c in registry.call_args_list], [(True,), (False,)])

    def test_service_refusal_is_reported(self):
        from jarvis.windows import startup
        with patch("jarvis.client.request", return_value={"ok": False, "error": "nope"}), \
                patch.object(startup, "set_startup") as registry:
            with self.assertRaisesRegex(RuntimeError, "nope"):
                startup.configure_startup(False)
        registry.assert_not_called()


class AudioTests(unittest.TestCase):
    def stream(self, source_channels, target_channels):
        # Build the conversion stage directly; no PortAudio instance/device.
        stream = object.__new__(PCMStream)
        stream.source_channels = source_channels
        stream.target_channels = target_channels
        stream.source_rate = stream.target_rate = 48000
        stream.silence_on_idle = False
        stream.last_samples = time.monotonic()
        stream.state = None
        stream.pending = bytearray()
        stream.error = None
        stream.queue = queue.Queue()
        return stream

    @unittest.skipUnless(
        importlib.util.find_spec("audioop"),
        "Windows audio conversion dependency is optional on Linux",
    )
    def test_recording_preserves_left_and_right_channels(self):
        stream = self.stream(2, 2)
        data = struct.pack("<hhhh", 1000, -2000, 3000, -4000)
        stream.queue.put(data)
        self.assertEqual(stream.read(2), data)

    @unittest.skipUnless(
        importlib.util.find_spec("audioop"),
        "Windows audio conversion dependency is optional on Linux",
    )
    def test_microphone_stereo_is_downmixed_for_speech(self):
        stream = self.stream(2, 1)
        stream.queue.put(struct.pack("<hhhh", 1000, 3000, -1000, -3000))
        self.assertEqual(struct.unpack("<hh", stream.read(2)), (2000, -2000))

    @unittest.skipUnless(
        importlib.util.find_spec("audioop"),
        "Windows audio conversion dependency is optional on Linux",
    )
    def test_mono_recording_input_is_duplicated_to_both_channels(self):
        stream = self.stream(1, 2)
        stream.queue.put(struct.pack("<hh", 1000, -1000))
        self.assertEqual(
            struct.unpack("<hhhh", stream.read(2)), (1000, 1000, -1000, -1000)
        )

    @unittest.skipUnless(
        importlib.util.find_spec("audioop"),
        "Windows audio conversion dependency is optional on Linux",
    )
    def test_callback_error_is_not_silently_replaced_with_silence(self):
        stream = self.stream(1, 1)
        stream.error = RuntimeError("overflow")
        with self.assertRaisesRegex(RuntimeError, "overflow"):
            stream.read(1)

    def test_duplicate_device_names_are_refused(self):
        class Engine:
            def get_host_api_info_by_type(self, _):
                return {"index": 2}

            def get_device_count(self):
                return 2

            def get_device_info_by_index(self, i):
                return {"index": i, "name": "Mic", "hostApi": 2, "maxInputChannels": 1}

        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            select_device(Engine(), types.SimpleNamespace(paWASAPI=1), "input:Mic")

    def test_quiet_loopback_adds_timed_silence_but_microphone_never_does(self):
        stream = self.stream(2, 2)
        stream.silence_on_idle = True
        stream.last_samples = 10
        with (
            patch("jarvis.windows.audio._audioop", return_value=object()),
            patch("jarvis.windows.audio.time.monotonic", return_value=10.2),
            patch.object(stream.queue, "get", side_effect=queue.Empty),
        ):
            self.assertEqual(stream.read(960), bytes(960 * 4))
        self.assertEqual(len(stream.pending), (9600 - 960) * 4)
        microphone = self.stream(1, 1)
        with (
            patch("jarvis.windows.audio._audioop", return_value=object()),
            patch.object(microphone.queue, "get", side_effect=queue.Empty),
        ):
            with self.assertRaisesRegex(RuntimeError, "no audio"):
                microphone.read(480)


if __name__ == "__main__":
    unittest.main()
