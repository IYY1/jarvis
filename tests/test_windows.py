"""Windows policies run on Linux too; no live audio, desktop actions or services."""

import importlib.util
import io
import json
from pathlib import Path
import queue
import struct
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from murmur import config, host
from murmur.recorder import RecorderError
from murmur.windows import encoding, transport
from murmur.windows.audio import PCMStream, select_device
from murmur.windows.apps import WindowsApp
from murmur.windows.capture import Replay, ReplayBuffer

try:
    import tkinter  # noqa: F401  (the Windows settings window needs Tk)
    TK = True
except ImportError:
    TK = False


@unittest.skipUnless(TK, "Tk is not installed")
class WindowRequestTests(unittest.TestCase):
    def test_requests_keep_the_client_per_operation_timeouts(self):
        from murmur.windows.ui import Window
        expected = {"execute": 45, "record_start": 20, "status": 15,
                    "record_stop": 60, "save_config": 60}
        for op, timeout in expected.items():
            with self.subTest(op=op):
                runs = []
                window = types.SimpleNamespace(submit=lambda run, callback: runs.append(run), received=None)
                Window.call(window, op)
                with patch("murmur.windows.ui.request", return_value={"ok": True}) as request:
                    runs[0]()
                self.assertEqual(request.call_args.kwargs["timeout"], timeout)


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
        self.assertEqual([title for title, _ in plan], ["Game and other apps", "firefox", "Microphone"])
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
        from murmur.windows import audio
        with tempfile.TemporaryDirectory() as folder:
            worker = Path(folder) / "jarvis-voice"
            worker.write_text("#!/bin/sh\nread request\necho \"$request\" > \"$0.request\"\nprintf 'PCMDATA!'\n"
                              "echo '{\"event\":\"error\",\"message\":\"firefox is not running.\"}' >&2\nexit 1\n")
            worker.chmod(0o755)
            written = []
            pipe = MagicMock()
            pipe.return_value.__enter__.return_value.write.side_effect = written.append
            with patch("murmur.native_worker.binary_path", return_value=worker), \
                    patch("murmur.windows.pipes.PipeWriter", pipe):
                with self.assertRaisesRegex(RuntimeError, "firefox is not running"):
                    audio.track_to_pipe({"track": {"kind": "game", "app": "firefox"}}, r"\\.\pipe\Jarvis-x")
            self.assertEqual(b"".join(written), b"PCMDATA!")
            self.assertEqual(json.loads((Path(folder) / "jarvis-voice.request").read_text()),
                             {"process": "firefox", "include": False})

    def test_audio_failure_names_the_track_and_reason(self):
        from murmur.windows.capture import audio_failure
        process = types.SimpleNamespace(stderr=io.BytesIO(
            b"Traceback ...\nRuntimeError: firefox is not running. Start it before instant replay.\n"))
        self.assertEqual(audio_failure(process, "firefox"),
                         "The firefox track stopped: firefox is not running. Start it before instant replay.")
        self.assertIn("unexpectedly", audio_failure(types.SimpleNamespace(stderr=io.BytesIO(b"")), None))

    def test_ten_bit_settings_record_the_same_family_until_windows_supports_them(self):
        self.assertEqual(encoding.format_for({"codec": "av1_10bit"}, "sdr"), "av1")
        self.assertEqual(encoding.format_for({"codec": "hevc_10bit"}, "hdr"), "hevc")

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

            with patch('murmur.windows.capture.tool', return_value='fake-ffmpeg'), \
                    patch('murmur.windows.capture.subprocess.run', side_effect=remux), \
                    patch('murmur.windows.capture.verify_video', side_effect=RecorderError('invalid HDR metadata')):
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
            "src/murmur/discord_control.py",
            "src/murmur/discord_commands.py",
            "native/controller/src/discord.rs",
        ):
            self.assertFalse((root / file).exists())
        self.assertNotIn("discord", config.DEFAULTS)


class StartupTests(unittest.TestCase):
    """Setup's start-at-sign-in component uses the same entry and setting as Settings."""

    def test_running_service_applies_the_change_itself(self):
        from murmur.windows import startup
        with patch("murmur.client.request", return_value={"ok": True}) as request, \
                patch.object(startup, "set_startup") as registry:
            self.assertEqual(startup.configure_startup(True), {"ok": True, "startup": True})
        request.assert_called_once_with("set_startup", enabled=True, timeout=5)
        registry.assert_not_called()

    def test_stopped_service_updates_run_entry_and_saved_setting(self):
        from murmur.windows import startup
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("os.environ", MURMUR_CONFIG=str(Path(directory) / "config.json")), \
                patch("murmur.client.request", side_effect=ConnectionRefusedError), \
                patch.object(startup, "set_startup") as registry:
            startup.configure_startup(True)
            self.assertTrue(config.load()["startup"])
            startup.configure_startup(False)
            self.assertFalse(config.load()["startup"])
        self.assertEqual([c.args for c in registry.call_args_list], [(True,), (False,)])

    def test_service_refusal_is_reported(self):
        from murmur.windows import startup
        with patch("murmur.client.request", return_value={"ok": False, "error": "nope"}), \
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
            patch("murmur.windows.audio._audioop", return_value=object()),
            patch("murmur.windows.audio.time.monotonic", return_value=10.2),
            patch.object(stream.queue, "get", side_effect=queue.Empty),
        ):
            self.assertEqual(stream.read(960), bytes(960 * 4))
        self.assertEqual(len(stream.pending), (9600 - 960) * 4)
        microphone = self.stream(1, 1)
        with (
            patch("murmur.windows.audio._audioop", return_value=object()),
            patch.object(microphone.queue, "get", side_effect=queue.Empty),
        ):
            with self.assertRaisesRegex(RuntimeError, "no audio"):
                microphone.read(480)


if __name__ == "__main__":
    unittest.main()
