"""Instant replay: engine lifecycle with a fake gpu-screen-recorder, phrases, config, dispatch.

No display, microphone or capture is used.
"""

import asyncio
import copy
import io
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from murmur import config
from murmur.actions import Dispatcher
from murmur.commands import Intent, Parser
from murmur.recorder import RecorderError
from murmur.replay import Replay


class FakeReplayProcess:
    """Answers save-replay on its -ipc socket like gpu-screen-recorder: writes the file, replies with its path."""
    fail_with = None

    def __init__(self, argv, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        self.signals = []
        self.code = None
        self.exited = threading.Event()
        self.stderr = io.BytesIO(b"")
        self.folder = Path(argv[argv.index("-o") + 1])
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(argv[argv.index("-ipc") + 1])
        self.server.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection, connection.makefile("rwb") as stream:
                request = json.loads(stream.readline())
                if FakeReplayProcess.fail_with:
                    reply = {"id": request["id"], "result": "error", "data": FakeReplayProcess.fail_with}
                else:
                    path = self.folder / "Replay_2026-10-21_18-30-00.mp4"
                    path.write_bytes(b"replay-video")
                    reply = {"id": request["id"], "result": "ok", "data": str(path)}
                stream.write((json.dumps(reply) + "\n").encode())

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGINT:
            self.exit(0)

    def exit(self, code):
        self.code = code
        self.server.close()
        os.unlink(self.argv[self.argv.index("-ipc") + 1])
        self.exited.set()

    def wait(self, timeout=None):
        self.exited.wait(timeout or 3)
        return self.code

    def kill(self):
        self.exit(-signal.SIGKILL)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = config.validate({"recording": {"monitor": "DP-1", "fps": 60, "output_dir": self.directory.name}})
        self.children = []
        def spawn(argv, **kwargs):
            self.children.append(FakeReplayProcess(argv, **kwargs))
            return self.children[-1]
        for patcher in (patch("murmur.replay.shutil.which", side_effect=lambda name: "/usr/bin/" + name),
                        patch("murmur.replay.sources", return_value=[{"name": "DP-1"}]),
                        patch("murmur.replay.verify_video"),
                        patch("murmur.recorder.subprocess.Popen", side_effect=spawn)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.changes = []
        FakeReplayProcess.fail_with = None
        self.replay = Replay(self.config, self.changes.append, ipc=Path(self.directory.name) / "replay.sock")

    def wait_for(self, predicate):
        for _ in range(200):
            if predicate():
                return
            threading.Event().wait(0.01)
        self.fail("condition not reached: " + str(self.replay.status()))

    def test_start_keeps_buffer_in_ram_for_the_configured_display(self):
        status = self.replay.start()
        self.assertEqual(status["state"], "on")
        argv = self.children[0].argv
        self.assertEqual(argv[argv.index("-w") + 1], "DP-1")
        self.assertEqual(argv[argv.index("-r") + 1], "120")
        self.assertEqual(argv[argv.index("-f") + 1], "60")
        self.assertEqual(argv[argv.index("-replay-storage") + 1], "ram")
        self.assertEqual(argv[argv.index("-ipc") + 1], str(self.replay.ipc))
        self.assertEqual(argv[argv.index("-v") + 1], "no")
        self.assertEqual(argv[argv.index("-a") + 1], "default_output|default_input")
        self.assertTrue(self.children[0].kwargs["start_new_session"])
        self.assertEqual(self.children[0].kwargs["umask"], 0o077)
        self.assertEqual(self.replay.start(), self.replay.status())   # one child at most
        self.assertEqual(len(self.children), 1)
        self.replay.stop()

    def test_save_reports_the_saved_file_and_stop_discards_the_buffer(self):
        self.replay.start()
        saved = self.replay.save()
        self.assertTrue(saved["file"].endswith("Replay_2026-10-21_18-30-00.mp4"))
        self.assertIn("Saved replay", saved["message"])
        stopped = self.replay.stop()
        self.assertEqual(self.children[0].signals, [signal.SIGINT])
        self.assertEqual(stopped["state"], "off")

    def test_backend_save_error_is_reported(self):
        self.replay.start()
        FakeReplayProcess.fail_with = "failed to save replay: failed to find a video keyframe"
        with self.assertRaisesRegex(RecorderError, "video keyframe"):
            self.replay.save()
        self.assertIn("video keyframe", self.replay.status()["message"])
        self.assertEqual(self.replay.status()["state"], "on")
        self.replay.stop()

    def test_game_sound_and_microphone_are_mixed_from_the_default_devices(self):
        for game, mic, expected in ((True, True, "default_output|default_input"), (True, False, "default_output"),
                                    (False, True, "default_input"), (False, False, None)):
            with self.subTest(game=game, mic=mic):
                self.config["replay"].update(game_audio=game, microphone=mic)
                self.replay.start()
                argv = self.children[-1].argv
                self.assertEqual(argv[argv.index("-a") + 1] if "-a" in argv else None, expected)
                self.replay.stop()

    def test_crash_is_an_error_not_a_silent_stop(self):
        self.replay.start()
        self.children[0].exit(1)
        self.wait_for(lambda: self.replay.status()["state"] == "error")
        self.assertIn("status 1", self.replay.status()["message"])

    def test_missing_backend_or_display_explains_the_fix(self):
        with patch("murmur.replay.shutil.which", return_value=None), self.assertRaisesRegex(RecorderError, "pacman -S gpu-screen-recorder"):
            self.replay.start()
        self.config["recording"]["monitor"] = ""
        with self.assertRaisesRegex(RecorderError, "Choose a display"):
            Replay(self.config, ipc=self.replay.ipc).start()
        self.assertEqual(self.children, [])

    def test_save_while_off_is_an_error(self):
        with self.assertRaisesRegex(RecorderError, "start replay"):
            self.replay.save()

    def test_hdr_replay_selects_hdr_codec_and_checks_saved_color(self):
        with patch("murmur.replay.sources", return_value=[{"name": "DP-1", "colorManagementPreset": "hdr"}]), \
                patch("murmur.replay.verify_video") as verify:
            status = self.replay.start()
            try:
                argv = self.children[-1].argv
                self.assertEqual(argv[argv.index("-k") + 1], "hevc_hdr")
                self.assertIn("HDR", status["message"])
                saved = self.replay.save()
                verify.assert_called_once_with(Path(saved["file"]), color="hdr", codec=None)
            finally:
                self.replay.stop()

    def test_failed_hdr_verification_does_not_announce_a_saved_clip(self):
        self.config["recording"]["color_mode"] = "hdr"
        self.replay.start()
        try:
            with patch("murmur.replay.verify_video", side_effect=RecorderError("HDR verification failed")):
                with self.assertRaisesRegex(RecorderError, "HDR verification failed"):
                    self.replay.save()
            self.assertIsNone(self.replay.status()["file"])
            self.assertNotIn("Saved replay", self.replay.status()["message"])
            self.assertTrue(list(Path(self.directory.name).glob("*.mp4")))
        finally:
            self.replay.stop()


class ReplayPhraseTests(unittest.TestCase):
    def test_phrases(self):
        parser = Parser()
        for phrase, action in (("clip that", "replay_save"), ("save that", "replay_save"), ("save replay", "replay_save"),
                               ("please save the replay", "replay_save"), ("start replay", "replay_start"),
                               ("turn on instant replay", "replay_start"), ("stop replay", "replay_stop")):
            with self.subTest(phrase=phrase):
                self.assertEqual(parser.parse(phrase), Intent(action))
        self.assertIn("clip that", parser.grammar([])["phrases"])

    def test_config_defaults_and_bounds(self):
        self.assertEqual(config.validate({})["replay"], {"seconds": 120, "autostart": False, "game_audio": True,
                                                       "microphone": True, "sound": True,
                                                       "storage": "ram", "max_mb": 256, "buffer_dir": ""})
        for bad in ({"seconds": 5}, {"seconds": 601}, {"seconds": "120"}, {"autostart": 1}, {"game_audio": "yes"},
                    {"microphone": 0}, {"sound": None}, {"storage": "auto"}, {"storage": None},
                    {"max_mb": True}, {"max_mb": 63}, {"max_mb": 16385}, {"max_mb": 64.0},
                    {"buffer_dir": None}, {"buffer_dir": "a\0b"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate({"replay": bad})

    def test_dispatcher_routes_replay_actions(self):
        replay = MagicMock()
        replay.start.return_value = replay.stop.return_value = replay.save.return_value = {"message": "ok"}
        dispatch = Dispatcher(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), replay=replay)
        for action, method in (("replay_start", replay.start), ("replay_stop", replay.stop), ("replay_save", replay.save)):
            with self.subTest(action=action):
                self.assertEqual(dispatch.execute(Intent(action)), "ok")
                method.assert_called_once_with()


class ReplayWindowTests(unittest.TestCase):
    def test_header_and_controls_follow_replay_state(self):
        from murmur.ui import JarvisWindow
        window = MagicMock(closed=False, connected=True, _test_active=False, config={"privacy": {"onboarded": True}})
        for state, visible, toggle in (("on", True, "Stop replay"), ("off", False, "Start replay")):
            with self.subTest(state=state):
                JarvisWindow._set_status(window, {"ok": True, "status": {"state": "idle", "muted": False,
                                                                       "replay": {"state": state, "message": "m"}}})
                window.replay_pill.set_visible.assert_called_with(visible)
                window.replay_save.set_sensitive.assert_called_with(visible)
                window.replay_toggle.set_label.assert_called_with(toggle)


class ClipLibraryTests(unittest.TestCase):
    def test_lists_saved_videos_newest_first_without_partial_files(self):
        from murmur.ui import clips
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for age, name in ((30, "Murmur_old.mkv"), (10, "Replay_new.mp4"), (20, "Replay_mid.MP4"),
                              (0, ".Murmur_x.part.mkv"), (0, "notes.txt")):
                (folder / name).write_bytes(b"x" * 10)
                os.utime(folder / name, (1_000_000 - age, 1_000_000 - age))
            self.assertEqual([c["name"] for c in clips(folder)], ["Replay_new.mp4", "Replay_mid.MP4", "Murmur_old.mkv"])
            self.assertEqual(len(clips(folder, limit=1)), 1)
            self.assertEqual(clips(folder / "missing"), [])

    def test_closing_or_leaving_the_player_releases_the_video(self):
        from murmur.ui import JarvisWindow
        window = MagicMock()
        window.stack.get_visible_child_name.return_value = "recording"
        window._close_player = lambda: JarvisWindow._close_player(window)
        JarvisWindow._clips_page_shown(window)
        window.player.set_file.assert_called_once_with(None)
        window.player_card.set_visible.assert_called_with(False)
        window.player = None                       # never played: nothing to release, no GStreamer
        JarvisWindow._close_player(window)


class ReplayCliTests(unittest.TestCase):
    def test_save_timeout_covers_backend_save_and_media_validation(self):
        from murmur.client import TIMEOUTS
        # Backend reply can take 30 s and ffprobe another 10 s.
        self.assertGreater(TIMEOUTS["replay_save"], 30 + 10)

    def test_waybar_marks_replay_and_cli_maps_actions(self):
        import json
        from murmur import cli
        line = json.loads(cli.waybar_line({"status": {"state": "idle", "message": "Waiting", "replay": {"state": "on"}}}))
        self.assertTrue(line["text"].endswith("⟲"))
        self.assertIn("Instant replay on", line["tooltip"])
        self.assertNotIn("⟲", json.loads(cli.waybar_line({"status": {"state": "idle"}}))["text"])
        for action in ("start", "stop", "save"):
            with self.subTest(action=action), patch("sys.argv", ["jarvis", "replay", action]), \
                    patch("murmur.client.request", return_value={"ok": True}) as request, patch("builtins.print"):
                self.assertEqual(cli.main(), 0)
                request.assert_called_once_with("replay_" + action)


class ClipSoundTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, sound=True):
        from murmur.daemon import Service
        registry = MagicMock()
        registry.index = {}
        with patch("murmur.daemon.AppRegistry", return_value=registry):
            service = Service({"activation": {"mode": "push_to_talk"}, "replay": {"sound": sound}})
        service.loop = asyncio.get_running_loop()
        return service

    async def test_a_new_clip_chirps_once_and_a_failed_save_sounds_low(self):
        async def inline(function, *args, **kwargs):
            return function(*args, **kwargs)
        for sound, expected in ((True, ["clip", "error"]), (False, [])):
            with self.subTest(sound=sound), patch("murmur.daemon.asyncio.to_thread", side_effect=inline), \
                    patch("murmur.feedback.play") as play:
                service = self.make_service(sound)
                saved = {"state": "on", "message": "Saved replay: a.mp4", "file": "/v/a.mp4"}
                service.on_replay_changed(saved)
                service.on_replay_changed(saved)                 # same clip again: no second chirp
                service.dispatcher = MagicMock()
                service.dispatcher.execute.side_effect = RecorderError("Replay not saved: no keyframe yet")
                with patch.dict("os.environ", {"MURMUR_DRY_RUN": "0"}):
                    result = await service.execute("clip that")
                await asyncio.gather(*service.tasks)
                self.assertFalse(result["ok"])
                self.assertEqual([c.args[0] for c in play.call_args_list], expected)

    def test_clip_chirp_is_two_short_tones(self):
        from murmur import feedback
        with patch("murmur.feedback.shutil.which", return_value="/usr/bin/pw-play"), \
                patch("murmur.feedback.subprocess.run") as run:
            feedback.play("clip")
            feedback.play("error")
        self.assertEqual(len(run.call_args_list[0].kwargs["input"]), 2 * len(run.call_args_list[1].kwargs["input"]))


class ReplayServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_replay_audio_changes_require_restart_before_saving(self):
        from murmur.daemon import Service
        registry = MagicMock(index={})
        with patch("murmur.daemon.AppRegistry", return_value=registry):
            service = Service({})
        service.replay = MagicMock()
        service.replay.status.return_value = {"state": "on"}
        for key, value in (("seconds", 180), ("game_audio", False), ("microphone", False),
                           ("storage", "disk"), ("max_mb", 512), ("buffer_dir", "/another/buffer")):
            cfg = copy.deepcopy(service.config)
            cfg["replay"][key] = value
            with self.subTest(key=key), patch("murmur.daemon.configuration.save") as save:
                with self.assertRaisesRegex(ValueError, "Stop instant replay"):
                    await service.handle({"op": "save_config", "config": cfg})
                save.assert_not_called()

    async def test_status_reports_replay_and_save_while_off_is_a_json_error(self):
        from murmur.daemon import Service
        registry = MagicMock()
        registry.index = {}
        with patch("murmur.daemon.AppRegistry", return_value=registry):
            service = Service({"activation": {"mode": "push_to_talk"}})
        self.assertEqual(service.status()["replay"]["state"], "off")
        async def inline(function, *args, **kwargs):
            return function(*args, **kwargs)
        with patch("murmur.daemon.asyncio.to_thread", side_effect=inline):
            result = await service.handle({"op": "replay_save"})
        self.assertFalse(result["ok"])
        self.assertIn("start replay", result["error"])


if __name__ == "__main__":
    unittest.main()
