"""Core policy tests. No microphone, display, app launch, or capture is accessed."""

import asyncio
import copy
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from jarvis.actions import Dispatcher
from jarvis.apps import AppRegistry
from jarvis.commands import CommandError, Intent, Parser, normalize
from jarvis import config
from jarvis.daemon import Service
from jarvis.hyprland import Hyprland
from jarvis.state import Activation


class ParserTests(unittest.TestCase):
    def test_natural_wording_and_politeness_resolve_to_existing_intents(self):
        for phrase in ("please open Firefox", "could you launch Firefox for me please", "open up Firefox", "start Firefox"):
            with self.subTest(phrase=phrase):
                self.assertEqual(Parser().parse(phrase), Intent("open_app", "firefox"))
        self.assertEqual(Parser().parse("turn down the volume please"), Intent("volume_down"))
        self.assertEqual(Parser().parse("switch to workspace three"), Intent("workspace", 3))
        self.assertEqual(Parser().parse("start screen recording"), Intent("record_start"))
        self.assertEqual(Parser().parse("could you stop the recording"), Intent("record_stop"))

    def test_politeness_does_not_remove_middle_words_or_create_shell_actions(self):
        self.assertEqual(Parser().parse("open please reader"), Intent("open_app", "please reader"))
        for phrase in ("please execute shutdown", "could you delete all files", "please go to workspace -1"):
            with self.subTest(phrase=phrase), self.assertRaises(CommandError):
                Parser().parse(phrase)

    def test_required_phrases(self):
        expected = {
            "Open Dolphin.": Intent("open_app", "dolphin"),
            "Launch Firefox.": Intent("open_app", "firefox"),
            "Open Stremio.": Intent("open_app", "stremio"),
            "Go to workspace three.": Intent("workspace", 3),
            "Move this window to workspace two.": Intent("move_workspace", 2),
            "Focus Firefox.": Intent("focus_app", "firefox"),
            "Make this window fullscreen.": Intent("fullscreen"),
            "Turn the volume down.": Intent("volume_down"),
            "Start recording.": Intent("record_start"),
            "Stop recording.": Intent("record_stop"),
            "Open the video recorder.": Intent("open_recorder"),
            "Stop listening.": Intent("pause"),
        }
        for phrase, intent in expected.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(Parser().parse(phrase), intent)

    def test_workspace_range_and_unicode_digits(self):
        for invalid in ("go to workspace eleven", "go to workspace 0", "go to workspace 99",
                        "move this window to workspace -1"):
            with self.subTest(invalid=invalid), self.assertRaises(CommandError):
                Parser().parse(invalid)
        self.assertEqual(Parser().parse("go to workspace １０"), Intent("workspace", 10))

    def test_custom_phrases_use_only_canonical_allowlist(self):
        self.assertEqual(Parser({"be quiet": "stop listening"}).parse("Be quiet!"), Intent("pause"))
        for mapping in ({"start recording": "stop recording"}, {"destroy": "rm everything"},
                        {"alias": "other alias", "other alias": "stop listening"}):
            with self.assertRaises(CommandError):
                Parser(mapping)

    def test_speech_is_never_an_arbitrary_shell_command(self):
        for phrase in ("rm -rf home", "run shutdown", "execute echo hello", "delete all files"):
            with self.subTest(phrase=phrase), self.assertRaises(CommandError):
                Parser().parse(phrase)

    def test_phrase_length_is_bounded(self):
        with self.assertRaises(ValueError):
            normalize("x" * 257)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.state = Activation(timeout=8, confidence=0.35, clock=lambda: self.now)

    def test_paused_rejects_speech_and_listen_is_single_session(self):
        self.assertFalse(self.state.accept("open firefox", 0.9))
        self.assertTrue(self.state.listen())
        self.assertFalse(self.state.listen())
        self.assertEqual(self.state.deadline, 108)

    def test_confidence_and_processing_transition(self):
        self.state.listen()
        for invalid in (None, 0.34, float("nan"), 1.1):
            self.assertFalse(self.state.accept("open firefox", invalid))
        self.assertTrue(self.state.accept("open firefox", 0.35))
        self.assertEqual(self.state.state, "processing")
        self.assertIsNone(self.state.deadline)
        self.assertFalse(self.state.accept("open dolphin", 1))

    def test_timeout_and_cancel_prevent_dispatch(self):
        self.state.listen()
        self.now = 108
        self.assertTrue(self.state.timed_out())
        self.assertFalse(self.state.accept("open firefox", 1))
        self.state.pause()
        self.assertEqual(self.state.state, "paused")
        self.assertFalse(self.state.accept("start recording", 1))

    def test_duplicate_command_suppression_is_bounded(self):
        self.state.listen()
        self.assertTrue(self.state.accept("Open Firefox!", 1))
        self.state.arm()
        self.state.listen()
        self.assertFalse(self.state.accept("open firefox", 1))
        self.now += 1.5
        self.assertTrue(self.state.accept("open firefox", 1))


class ConfigTests(unittest.TestCase):
    def test_config_merging_preserves_defaults_and_does_not_mutate(self):
        before = copy.deepcopy(config.DEFAULTS)
        actual = config.validate({"activation": {"mode": "push_to_talk"}})
        self.assertEqual(actual["activation"]["confidence"], 0.35)
        self.assertFalse(actual["privacy"]["onboarded"])
        self.assertEqual(config.DEFAULTS, before)

    def test_invalid_confidence_consent_and_normalized_alias_collision(self):
        for invalid in ({"activation": {"confidence": float("nan")}},
                        {"activation": {"confidence": True}},
                        {"privacy": {"onboarded": "true"}},
                        {"aliases": {"Browser": "one.desktop", "browser!": "two.desktop"}},
                        {"recording": {"fps": 0}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                config.validate(invalid)

    def test_saved_config_is_private_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "config.json"
            value = config.save({"aliases": {"browser": "firefox.desktop"}}, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.load(path), value)
            self.assertEqual(list(path.parent.glob(".config-*")), [])

    def test_oversized_config_is_rejected_before_it_breaks_next_start(self):
        aliases = {f"alias {number} " + "x" * 105: "y" * 256 for number in range(256)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with self.assertRaisesRegex(ValueError, "64|large|size|KiB"):
                config.save({"aliases": aliases}, path)
            self.assertFalse(path.exists())


def write_entry(root, name, *, title="Browser", hidden=False, exec_line="firefox %u"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Desktop Entry]\nType=Application\nName=" + title + "\nExec=" + exec_line
                    + ("\nHidden=true" if hidden else "") + "\n")
    return path


class AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_alias_resolves_and_ambiguity_is_explicit(self):
        write_entry(self.root, "one.desktop", exec_line="browser-one %u")
        write_entry(self.root, "two.desktop", exec_line="browser-two %u")
        registry = AppRegistry({"my browser": "two.desktop"}, [self.root])
        self.assertEqual(registry.resolve("MY BROWSER!").id, "two.desktop")
        with self.assertRaisesRegex(CommandError, "Ambiguous"):
            registry.resolve("browser")

    def test_display_name_and_id_normalizing_to_same_name_are_one_app(self):
        write_entry(self.root, "firefox.desktop", title="Firefox")
        registry = AppRegistry(roots=[self.root])
        self.assertEqual(registry.resolve("Firefox").id, "firefox.desktop")
        self.assertEqual(len(registry.index["firefox"]), 1)

    def test_same_app_installed_twice_resolves_to_native_entry(self):
        # Native + Flatpak Firefox used to be "Ambiguous" (reported October 2026).
        write_entry(self.root, "firefox.desktop", title="Firefox")
        write_entry(self.root, "org.mozilla.firefox.desktop", title="Firefox",
                    exec_line="/usr/bin/flatpak run --branch=stable org.mozilla.firefox @@u %u @@")
        registry = AppRegistry({"flatpak browser": "org.mozilla.firefox.desktop"}, [self.root])
        self.assertEqual(registry.resolve("Firefox").id, "firefox.desktop")
        self.assertEqual([a.id for a in registry.duplicates(registry.resolve("Firefox"))],
                         ["org.mozilla.firefox.desktop"])
        self.assertEqual(registry.resolve("flatpak browser").id, "org.mozilla.firefox.desktop")

    def test_hidden_user_override_masks_system_entry(self):
        user, system = self.root / "user", self.root / "system"
        write_entry(user, "browser.desktop", hidden=True)
        write_entry(system, "browser.desktop")
        self.assertEqual(AppRegistry(roots=[user, system]).apps, {})

    def test_missing_alias_target_and_missing_application_are_clear(self):
        registry = AppRegistry({"browser": "missing.desktop"}, [self.root])
        with self.assertRaisesRegex(CommandError, "missing application"):
            registry.resolve("browser")
        with self.assertRaisesRegex(CommandError, "not found"):
            registry.resolve("other application")

    def test_launch_uses_desktop_mechanism_and_structured_arguments(self):
        entry = write_entry(self.root, "my browser.desktop", exec_line="firefox %u")
        registry = AppRegistry(roots=[self.root])
        with patch("jarvis.apps.shutil.which", return_value="/usr/bin/gio"), \
                patch("jarvis.apps.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            registry.launch(registry.resolve("browser"))
            self.assertEqual(run.call_args.args[0], ["/usr/bin/gio", "launch", str(entry)])
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_overlong_display_name_does_not_break_all_discovery(self):
        write_entry(self.root, "long.desktop", title="x" * 300)
        write_entry(self.root, "normal.desktop", title="Firefox")
        registry = AppRegistry(roots=[self.root])
        self.assertEqual(registry.resolve("firefox").id, "normal.desktop")


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.apps, self.desktop, self.recorder = MagicMock(), MagicMock(), MagicMock()
        self.pause, self.open_ui = MagicMock(), MagicMock()
        self.dispatch = Dispatcher(self.apps, self.desktop, self.recorder, self.pause, self.open_ui)

    def test_already_running_application_is_focused_without_relaunch(self):
        app = self.apps.resolve.return_value
        app.name = "Firefox"
        self.desktop.running.return_value = [{"address": "0x123"}]
        self.assertEqual(self.dispatch.execute(Intent("open_app", "firefox")), "Focused Firefox")
        self.desktop.dispatch.assert_called_once_with("focus_address", "0x123")
        self.apps.launch.assert_not_called()

    def test_focus_missing_application_does_not_launch(self):
        self.desktop.running.return_value = []
        with self.assertRaisesRegex(CommandError, "not running"):
            self.dispatch.execute(Intent("focus_app", "firefox"))
        self.apps.launch.assert_not_called()

    def test_open_recorder_and_start_capture_are_distinct(self):
        self.dispatch.execute(Intent("open_recorder"))
        self.open_ui.assert_called_once_with("recording")
        self.recorder.start.assert_not_called()
        self.recorder.start.return_value = {"message": "Recorder running"}
        self.assertEqual(self.dispatch.execute(Intent("record_start")), "Recorder running")
        self.recorder.start.assert_called_once()

    def test_pause_and_desktop_allowlist(self):
        self.dispatch.execute(Intent("pause"))
        self.pause.assert_called_once()
        with self.assertRaises(CommandError):
            self.dispatch.execute(Intent("shell", "echo arbitrary"))
        with patch("jarvis.hyprland.subprocess.run") as run:
            for action, target in (("workspace", "2; anything"), ("focus_address", "0x1; anything"),
                                   ("shell", "x"), ("workspace", True)):
                with self.assertRaises(CommandError):
                    Hyprland().dispatch(action, target)
            run.assert_not_called()


class FakeWorker:
    def __init__(self):
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stdin = MagicMock()
        self.stdin.drain = AsyncMock()
        self.done = asyncio.Event()
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -signal.SIGTERM
        self.done.set()
        self.stdout.feed_eof()

    def kill(self):
        self.terminate()

    async def wait(self):
        await self.done.wait()
        return self.returncode

    def send(self, **event):
        self.stdout.feed_data((json.dumps(event) + "\n").encode())


async def until(condition):
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("The service did not handle the worker's events")


class ServiceConsentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Thread scheduling is outside these policy tests. The execution sandbox
        # can block asyncio's cross-thread shutdown wakeup; real IPC integration
        # separately exercises the production executor.
        async def inline(function, *args, **kwargs):
            return function(*args, **kwargs)
        patcher = patch("jarvis.daemon.asyncio.to_thread", side_effect=inline)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_service(self, onboarded=False):
        registry = MagicMock()
        registry.index = {}
        registry.list.return_value = []
        with patch("jarvis.daemon.AppRegistry", return_value=registry):
            service = Service({"privacy": {"onboarded": onboarded},
                               "activation": {"mode": "push_to_talk"}})
        service.loop = asyncio.get_running_loop()
        return service

    async def test_no_consent_prevents_ipc_unmute_listen_and_microphone_test(self):
        service = self.make_service()
        with patch("jarvis.daemon.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            for payload in ({"op": "mute", "muted": False}, {"op": "listen"},
                            {"op": "mic_test", "enabled": True}):
                with self.subTest(payload=payload), self.assertRaises(CommandError):
                    await service.handle(payload)
            spawn.assert_not_called()
        self.assertTrue(service.muted)
        self.assertIsNone(service.worker)

    async def test_first_consent_in_push_to_talk_leaves_capture_closed(self):
        service = self.make_service()
        updated = copy.deepcopy(service.config)
        updated["privacy"]["onboarded"] = True
        with patch("jarvis.daemon.configuration.save", side_effect=lambda c: c), \
                patch("jarvis.daemon.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            result = await service.handle({"op": "save_config", "config": updated})
            self.assertTrue(result["ok"])
            spawn.assert_not_called()
        self.assertFalse(service.muted)
        self.assertIsNone(service.worker)

    async def test_mute_wins_over_inflight_microphone_start(self):
        service = self.make_service(onboarded=True)
        worker = FakeWorker()
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed_spawn(*_args, **_kwargs):
            started.set()
            await release.wait()
            return worker
        with patch("jarvis.daemon.asyncio.create_subprocess_exec", side_effect=delayed_spawn):
            listen = asyncio.create_task(service.handle({"op": "listen"}))
            await asyncio.wait_for(started.wait(), 1)
            mute = asyncio.create_task(service.handle({"op": "mute", "muted": True}))
            await asyncio.sleep(0)
            release.set()
            try:
                await asyncio.wait_for(asyncio.gather(listen, mute, return_exceptions=True), 2)
                self.assertTrue(service.muted)
                self.assertIsNone(service.worker, "An in-flight start opened capture after microphone mute")
                self.assertTrue(worker.terminated)
            finally:
                await service.stop_worker()

    async def test_switching_wake_mode_to_push_to_talk_closes_existing_capture(self):
        service = self.make_service(onboarded=True)
        service.config["activation"]["mode"] = "wake_word"
        worker = FakeWorker()
        service.worker = worker
        updated = copy.deepcopy(service.config)
        updated["activation"]["mode"] = "push_to_talk"
        try:
            with patch("jarvis.daemon.configuration.save", side_effect=lambda c: c), \
                    patch("jarvis.daemon.AppRegistry", return_value=service.apps):
                await service.handle({"op": "save_config", "config": updated})
            self.assertTrue(worker.terminated, "Switching to push-to-talk must release the old wake microphone")
            self.assertIsNone(service.worker)
            self.assertFalse(service.status()["microphone_open"])
        finally:
            await service.stop_worker()

    async def test_one_shot_finish_restores_wake_mode_only_when_still_selected(self):
        for mode, error, expected in (("wake_word", False, True),
                                      ("push_to_talk", False, False),
                                      ("wake_word", True, False)):
            with self.subTest(mode=mode, error=error):
                service = self.make_service(onboarded=True)
                service.config["activation"]["mode"] = mode
                worker = FakeWorker()
                worker.returncode = 0
                worker.done.set()
                worker.stdout.feed_eof()
                service.worker = worker
                if error:
                    service.activation.error()
                with patch.object(service, "start_worker", new_callable=AsyncMock) as start:
                    await service.read_voice(worker, "once")
                    await asyncio.sleep(0)
                    if expected:
                        start.assert_awaited_once_with("wake_word")
                    else:
                        start.assert_not_awaited()
                self.assertIsNone(service.worker)

    async def test_queued_wake_resume_does_not_reopen_capture_in_push_to_talk(self):
        service = self.make_service(onboarded=True)
        with patch("jarvis.daemon.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            await service.start_worker("wake_word")
            spawn.assert_not_awaited()
        self.assertIsNone(service.worker)

    async def test_finished_microphone_test_is_reported_and_stopping_it_keeps_wake_capture(self):
        service = self.make_service(onboarded=True)
        service.config["activation"]["mode"] = "wake_word"
        workers = []
        async def spawn(*_args, **_kwargs):
            workers.append(FakeWorker())
            return workers[-1]
        with patch("jarvis.daemon.asyncio.create_subprocess_exec", side_effect=spawn):
            try:
                await service.handle({"op": "mic_test", "enabled": True})
                self.assertTrue(service.status()["mic_test"])
                test, reader = workers[-1], service.voice_task
                test.returncode = 0          # the 15-second test ends by itself
                test.done.set()
                test.stdout.feed_eof()
                await reader
                await asyncio.gather(*service.tasks)
                wake = service.worker
                self.assertIsNot(wake, test)
                self.assertFalse(service.status()["mic_test"])
                await service.handle({"op": "mic_test", "enabled": False})
                self.assertIs(service.worker, wake, "Stopping a finished test reloaded wake capture")
                self.assertFalse(wake.terminated)
            finally:
                await service.stop_worker()

    async def ready_wake_worker(self, service, workers):
        service.config["activation"]["mode"] = "wake_word"
        await service.start_worker("wake_word")
        workers[0].send(event="ready", listening=False, deadline=None)
        await until(lambda: service.activation.state == "idle")
        return workers[0]

    async def test_listen_starts_a_session_in_the_loaded_wake_worker(self):
        service = self.make_service(onboarded=True)
        workers = []
        async def spawn(*_args, **_kwargs):
            workers.append(FakeWorker())
            return workers[-1]
        with patch("jarvis.daemon.asyncio.create_subprocess_exec", side_effect=spawn), \
                patch.object(service, "execute", new_callable=AsyncMock) as execute:
            try:
                worker = await self.ready_wake_worker(service, workers)
                await service.handle({"op": "listen"})
                self.assertEqual(len(workers), 1, "Listen once reloaded the voice models")
                self.assertFalse(worker.terminated)
                self.assertEqual(worker.stdin.write.call_args.args[0], b"listen\n")
                # Heard before the worker read the line; the new session replaces it.
                worker.send(event="wake", deadline=time.monotonic() + service.activation.timeout)
                worker.send(event="command", text="open steam", confidence=0.9, captured_at=time.monotonic())
                deadline = time.monotonic() + service.activation.timeout
                worker.send(event="ready", listening=True, deadline=deadline)
                await until(lambda: service.message == "Say a command")
                self.assertEqual((service.activation.state, service.activation.deadline), ("listening", deadline))
                worker.send(event="command", text="open firefox", confidence=0.9, captured_at=time.monotonic())
                await until(lambda: execute.await_count)
                execute.assert_awaited_once_with("open firefox")
                self.assertEqual(service.activation.state, "idle")
                self.assertIs(service.worker, worker)
            finally:
                await service.stop_worker()

    async def test_cancel_ends_the_session_in_the_loaded_wake_worker(self):
        service = self.make_service(onboarded=True)
        workers = []
        async def spawn(*_args, **_kwargs):
            workers.append(FakeWorker())
            return workers[-1]
        with patch("jarvis.daemon.asyncio.create_subprocess_exec", side_effect=spawn), \
                patch.object(service, "execute", new_callable=AsyncMock) as execute:
            try:
                worker = await self.ready_wake_worker(service, workers)
                worker.send(event="wake", deadline=time.monotonic() + service.activation.timeout)
                await until(lambda: service.activation.state == "listening")
                await service.handle({"op": "cancel"})
                self.assertEqual(len(workers), 1, "Cancel reloaded the voice models")
                self.assertFalse(worker.terminated)
                self.assertEqual(worker.stdin.write.call_args.args[0], b"cancel\n")
                self.assertEqual(service.activation.state, "idle")
                # Sent before the worker read the line: must not run.
                worker.send(event="command", text="open firefox", confidence=0.9, captured_at=time.monotonic())
                worker.send(event="ready", listening=False, deadline=None)
                await until(lambda: service.message.startswith("Waiting for"))
                execute.assert_not_awaited()
                self.assertEqual(service.activation.state, "idle")
                self.assertIs(service.worker, worker)
            finally:
                await service.stop_worker()

    async def test_action_dispatch_errors_are_json_safe_and_do_not_escape_ipc(self):
        service = self.make_service()
        service.dispatcher = MagicMock()
        service.dispatcher.execute.side_effect = CommandError("App missing")
        result = await service.handle({"op": "execute", "phrase": "open missing"})
        self.assertEqual(result, {"ok": False, "error": "App missing"})
        service.dispatcher.execute.assert_called_once_with(Intent("open_app", "missing"))

    async def test_non_text_phrase_gets_a_json_error(self):
        service = self.make_service()
        for phrase in (5, None):
            with self.subTest(phrase=phrase):
                self.assertFalse((await service.handle({"op": "execute", "phrase": phrase}))["ok"])


class SettingsStatusTests(unittest.TestCase):
    def test_window_leaves_test_mode_when_the_service_ends_the_test(self):
        from jarvis.ui import JarvisWindow
        window = MagicMock(closed=False, connected=True, _test_active=True, config={"privacy": {"onboarded": True}})
        window._show_test = lambda active: JarvisWindow._show_test(window, active)
        JarvisWindow._set_status(window, {"ok": True, "status": {"state": "idle", "muted": False, "mic_test": False}})
        self.assertFalse(window._test_active)
        window.test_button.set_label.assert_called_with("Test microphone")
        window.level_text.set_text.assert_called_with("Input test is off")

    def test_download_button_hidden_when_models_are_installed_before_voice_starts(self):
        from jarvis.ui import JarvisWindow
        window = MagicMock(closed=False, connected=True, _test_active=False, config=config.validate({}))
        with patch("jarvis.models.describe", return_value={"model_ready": True, "accurate_model_ready": True}):
            JarvisWindow._load_settings(window)
        JarvisWindow._set_status(window, {"ok": True, "status": {"state": "paused", "muted": True, "engine": None}})
        window.install_model_button.set_visible.assert_called_with(False)


if __name__ == "__main__":
    unittest.main()
