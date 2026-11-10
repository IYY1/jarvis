"""Regression tests for the October 2026 review fixes (see README "Review fixes").

No microphone, display, app launch or capture is used.
"""

import asyncio
import importlib.util
import itertools
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from jarvis import cli, client, config
from jarvis.apps import App, AppRegistry
from jarvis.commands import (APP_VERBS, POLITE_SUFFIXES, REFERENCE, CommandError,
                             GrammarMatcher, Intent, Parser, normalize)
from jarvis.hyprland import Hyprland, volume
from jarvis.recorder import Recorder


def matcher_for(grammar: dict) -> GrammarMatcher:
    return GrammarMatcher(grammar["phrases"], grammar["apps"], grammar["verbs"])


class GrammarParserConsistencyTests(unittest.TestCase):
    """H1: what the recognizer may hear and what the parser accepts must agree."""

    def setUp(self):
        self.parser = Parser({"capture that": "start recording"})
        self.grammar = self.parser.grammar(["firefox", "dolphin", "web browser"])
        self.matcher = matcher_for(self.grammar)

    def test_every_documented_example_is_speakable_and_parses(self):
        from jarvis.phrases import has_arabic
        for example in (e for entry in REFERENCE for e in entry["examples"]):
            with self.subTest(example=example):
                text = normalize(example)
                # Arabic is heard by the Arabic model, never the English Vosk
                # grammar; it must still parse to a supported command.
                if not has_arabic(text):
                    self.assertTrue(self.matcher.accepts(text), f"{example!r} is shown in Commands but not in the grammar")
                self.parser.parse(text)

    def test_everything_the_grammar_allows_parses(self):
        prefixes, suffixes = ("", *self.grammar["prefixes"]), ("", *POLITE_SUFFIXES)
        cores = list(self.grammar["phrases"])
        cores += [f"{verb} {app}" for verb in self.grammar["verbs"] for app in self.grammar["apps"]]
        for core, prefix, suffix in itertools.product(cores, prefixes, suffixes):
            text = " ".join(part for part in (prefix, core, suffix) if part)
            with self.subTest(text=text):
                self.assertTrue(self.matcher.accepts(text))
                self.parser.parse(text)

    def test_spoken_app_forms_resolve_to_the_right_intent(self):
        for verb, action in APP_VERBS.items():
            with self.subTest(verb=verb):
                self.assertEqual(self.parser.parse(f"could you {verb} web browser for me"), Intent(action, "web browser"))

    def test_unknown_or_partial_phrases_are_not_accepted(self):
        for text in ("open", "please", "open chrome", "go to workspace eleven", "please please open firefox"):
            with self.subTest(text=text):
                self.assertFalse(self.matcher.accepts(text))

    def test_large_app_lists_degrade_instead_of_failing(self):
        """H4: thousands of desktop entries must not stop voice control from starting."""
        names = [f"game {i}" for i in range(3000)]
        grammar = Parser().grammar(names)
        self.assertEqual(len(grammar["apps"]) + len(grammar["overflow"]), 3000)
        self.assertTrue(grammar["overflow"][0].startswith("open "))

    def test_mute_volume_is_explicit_and_unmute_uses_dictionary_words(self):
        self.assertEqual(self.parser.parse("mute volume"), Intent("volume_mute"))
        for phrase in ("turn the sound back on", "turn the sound on", "restore the volume"):
            self.assertEqual(self.parser.parse(phrase), Intent("volume_unmute"))


class WindowMatchingTests(unittest.TestCase):
    """H2: launcher programs must not make unrelated windows look like the app."""

    def test_steam_game_does_not_match_steam_client(self):
        game = App("Cyberpunk 2077.desktop", "Cyberpunk 2077", "/x", "steam steam://rungameid/1091500")
        self.assertNotIn("steam", game.classes())
        self.assertIn("steam_app_1091500", game.classes())
        clients = [{"class": "steam", "initialClass": "steam", "address": "0x1", "focusHistoryID": 0}]
        with patch.object(Hyprland, "query", return_value=clients):
            self.assertEqual(Hyprland().running(game), [])
        clients.append({"class": "steam_app_1091500", "initialClass": "steam_app_1091500", "address": "0x2", "focusHistoryID": 1})
        with patch.object(Hyprland, "query", return_value=clients):
            self.assertEqual([c["address"] for c in Hyprland().running(game)], ["0x2"])

    def test_wrappers_and_web_apps(self):
        cases = [
            App("chrome-abc-Default.desktop", "YouTube", "/x", "/opt/google/chrome/google-chrome --app-id=abc", wm_class="crx_abc"),
            App("org.prismlauncher.PrismLauncher.desktop", "Prism", "/x", "flatpak run org.prismlauncher.PrismLauncher"),
            App("game.desktop", "Game", "/x", "env DXVK_HUD=1 gamemoderun game-bin"),
        ]
        for app, wrapper in zip(cases, ("google-chrome", "flatpak", "env"), strict=True):
            with self.subTest(app=app.id):
                self.assertNotIn(wrapper, app.classes())
        self.assertIn("firefox", App("firefox.desktop", "Firefox", "/x", "/usr/lib/firefox/firefox %u").classes())

    def test_only_show_in_entries_for_other_desktops_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, extra in (("gnome-settings", "OnlyShowIn=GNOME;"), ("hypr-tool", "OnlyShowIn=Hyprland;"),
                                ("not-here", "NotShowIn=Hyprland;"), ("plain", "")):
                (root / f"{name}.desktop").write_text(f"[Desktop Entry]\nType=Application\nName={name}\nExec={name}\n{extra}\n")
            with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "Hyprland"}):
                registry = AppRegistry(roots=[root])
                self.assertEqual(sorted(registry.apps), ["hypr-tool.desktop", "plain.desktop"])
                (root / "new-app.desktop").write_text("[Desktop Entry]\nType=Application\nName=new app\nExec=new-app\n")
                self.assertTrue(registry.rescan())
                self.assertIn("new app", registry.index)
                self.assertFalse(registry.rescan())


class RecorderOwnershipTests(unittest.TestCase):
    """H3: a recorder must not outlive the service, and leftovers must be visible."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = {"configured": True, "monitor": "DP-1", "fps": 30, "quality": 23,
                       "output_dir": self.directory.name}

    def test_capture_is_bound_to_service_lifetime_from_a_long_lived_thread(self):
        seen = {}
        class Child:
            stderr = None
            def wait(self):
                return 0
        def spawn(argv, **kwargs):
            seen["argv"], seen["thread"] = argv, threading.current_thread().name
            Path(argv[argv.index("--file") + 1]).write_bytes(b"x")
            return Child()
        with patch("jarvis.recorder.shutil.which", side_effect=lambda x: "/usr/bin/" + x), \
                patch("jarvis.recorder.sources", return_value=[{"name": "DP-1"}]), \
                patch("jarvis.recorder.orphaned_recorders", return_value=[]), \
                patch("jarvis.recorder.subprocess.Popen", side_effect=spawn), \
                patch("jarvis.recorder._query", return_value=json.dumps({"streams": []})):
            Recorder(self.config).start()
        self.assertEqual(seen["argv"][:4], ["/usr/bin/setpriv", "--pdeathsig", "INT", "--"])
        self.assertEqual(seen["argv"][4], "/usr/bin/wf-recorder")
        self.assertTrue(seen["thread"].startswith("jarvis-recorder-spawn"))

    def test_running_orphan_blocks_a_second_capture(self):
        with patch("jarvis.recorder.orphaned_recorders", return_value=[4242]):
            recorder = Recorder(self.config)
            recorder.inspect_previous()
            self.assertEqual(recorder.status()["state"], "error")
            self.assertIn("kill -INT 4242", recorder.status()["message"])
            with self.assertRaisesRegex(Exception, "4242"):
                recorder.start()

    def test_leftover_partial_files_are_reported(self):
        partial = Path(self.directory.name) / ".Jarvis_2026-10-20_10-00-00_abc.part.mkv"
        partial.write_bytes(b"x")
        recorder = Recorder(self.config)
        recorder.inspect_previous(orphans=[])
        status = recorder.status()
        self.assertEqual(status["state"], "idle")
        self.assertIn("unfinished recording", status["message"])
        self.assertEqual(status["file"], str(partial))


class ConfigValidationTests(unittest.TestCase):
    """H4/M7: anything validate() accepts must also let the service start."""

    def test_custom_command_targets_are_checked_at_validation(self):
        with self.assertRaises(ValueError):
            config.validate({"commands": {"zap": "rm everything"}})
        self.assertEqual(config.validate({"commands": {"capture that": "start recording"}})["commands"],
                         {"capture that": "start recording"})

    def test_unknown_settings_are_rejected_on_save_but_tolerated_on_load(self):
        with self.assertRaisesRegex(ValueError, "activation.comand_timeout"):
            config.validate({"activation": {"comand_timeout": 3}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            # A file from the first version: no closest_match key, plus a typo.
            path.write_text(json.dumps({"activation": {"comand_timeout": 3},
                                        "recognition": {"backend": "auto", "model_path": ""}}))
            with patch("sys.stderr"):
                loaded = config.load(path)
            self.assertNotIn("comand_timeout", loaded["activation"])
            self.assertTrue(loaded["recognition"]["closest_match"])
            config.save(loaded, path)
            self.assertNotIn("comand_timeout", json.loads(path.read_text())["activation"])

    def test_settings_another_version_wrote_are_repaired_and_backed_up(self):
        old = {"activation": {"wake_phrase": "alfred"},
               "aliases": {"streamio": "com.stremio.Stremio.desktop"}, "app_prefixes": {"streamio": "watch"},
               "recording": {"fps": 999, "quality_preset": "insane", "codec": "av1", "system_audio": True,
                             "microphone": True, "audio_device": "combined", "combined_audio_source": True},
               "replay": {"seconds": 300}}
        fixed, reset = config.repair(old)
        self.assertEqual(reset, ["recording.fps", "recording.quality_preset"])   # same as config.rs
        self.assertEqual((fixed["app_prefixes"]["streamio"], fixed["recording"]["microphone"], fixed["replay"]["seconds"]),
                         ("watch", True, 300))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(old))
            with patch("sys.stderr"):
                loaded = config.load(path)
            self.assertEqual((loaded["activation"]["wake_phrase"], loaded["recording"]["fps"]), ("alfred", config.DEFAULTS["recording"]["fps"]))
            self.assertEqual(json.loads(path.read_text())["recording"]["fps"], config.DEFAULTS["recording"]["fps"])   # repaired on disk
            [kept] = (Path(folder) / "backups").glob("config-before-repair-*.json")
            self.assertEqual(json.loads(kept.read_text())["recording"]["fps"], 999)
            path.write_text("{not json")
            with patch("sys.stderr"):
                self.assertEqual(config.load(path), config.validate({}))
            self.assertEqual(len(list((Path(folder) / "backups").glob("config-unreadable-*.json"))), 1)
            config.note_version("9.9.9", path)
            config.note_version("9.9.9", path)
            self.assertEqual(len(list((Path(folder) / "backups").glob("config-from-*.json"))), 1)

    def test_settings_export_and_import_round_trip(self):
        from jarvis import settings_file
        with tempfile.TemporaryDirectory() as folder:
            live = Path(folder) / "config.json"
            config.save(config.validate({"activation": {"wake_phrase": "alfred"}, "recording": {"fps": 120}}), live)
            exported = Path(folder) / "export.json"
            with patch.dict(os.environ, {"JARVIS_CONFIG": str(live)}), \
                    patch("jarvis.client.request", side_effect=ConnectionError("service off")):
                settings_file.export_settings(exported)
                self.assertEqual(oct(exported.stat().st_mode & 0o777), "0o600")
                config.save(config.validate({}), live)
                data = json.loads(exported.read_text())
                data["recording"]["quality_preset"] = "insane"   # as if a newer version wrote it
                exported.write_text(json.dumps(data))
                message = settings_file.import_settings(exported)
            restored = json.loads(live.read_text())
            self.assertEqual((restored["activation"]["wake_phrase"], restored["recording"]["fps"]), ("alfred", 120))
            self.assertIn("recording.quality_preset", message)
            self.assertEqual(len(list((Path(folder) / "backups").glob("config-before-import-*.json"))), 1)

    def test_old_name_folders_launcher_and_service_move_to_jarvis(self):
        from jarvis import migrate
        old = migrate.OLD
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / f".config/{old}").mkdir(parents=True)
            (home / f".config/{old}/config.json").write_text(json.dumps({
                "recording": {"output_dir": f"~/Videos/{old.title()}"},
                "recognition": {"model_path": str(home / f".local/share/{old}/models/vosk")}}))
            (home / f".local/share/{old}/models").mkdir(parents=True)
            (home / f"Videos/{old.title()}/Deadlock").mkdir(parents=True)
            (home / f"Videos/{old.title()}/Deadlock/clip.mp4").write_text("video")
            (home / ".local/share/applications").mkdir(parents=True)
            (home / f".local/share/applications/org.{old}.Controller.desktop").write_text(f"Exec=python -m {old} ui\n")
            (home / ".config/systemd/user").mkdir(parents=True)
            (home / f".config/systemd/user/{old}.service").write_text(f"ExecStart=python -m {old} daemon\n")
            (home / f".config/systemd/user/{old}.service.d").mkdir()
            (home / f".config/systemd/user/{old}.service.d/95-rust.conf").write_text(
                f"ExecStart=/x/src/{old}/bin/jarvis-controller\nEnvironment={old.upper()}_CONTROLLER=rust\n")
            calls = []
            def systemctl(argv, **_):
                calls.append(argv[2:])
                return types.SimpleNamespace(stdout="enabled\n" if argv[2] == "is-enabled" else "")
            env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share")}
            with patch.dict(os.environ, env), patch("pathlib.Path.home", return_value=home), \
                    patch("jarvis.migrate.subprocess.run", side_effect=systemctl):
                done = migrate.from_old_name()
                self.assertEqual(migrate.from_old_name(), [])   # once
            self.assertEqual(len(done), 6)
            self.assertEqual((home / ".config/systemd/user/jarvis.service.d/95-rust.conf").read_text(),
                             "ExecStart=/x/src/jarvis/bin/jarvis-controller\nEnvironment=JARVIS_CONTROLLER=rust\n")
            self.assertFalse((home / f".config/systemd/user/{old}.service.d").exists())
            self.assertFalse((home / f".config/{old}").exists())
            settings = json.loads((home / ".config/jarvis/config.json").read_text())
            self.assertEqual(settings["recording"]["output_dir"], "~/Videos/Jarvis")
            self.assertEqual(settings["recognition"]["model_path"], str(home / ".local/share/jarvis/models/vosk"))
            self.assertEqual((home / "Videos/Jarvis/Deadlock/clip.mp4").read_text(), "video")
            self.assertEqual((home / ".local/share/applications/org.jarvis.Controller.desktop").read_text(), "Exec=python -m jarvis ui\n")
            self.assertEqual((home / ".config/systemd/user/jarvis.service").read_text(), "ExecStart=python -m jarvis daemon\n")
            self.assertIn(["enable", "jarvis.service"], calls)
            self.assertIn(["disable", f"{old}.service"], calls)

    def test_recorder_limits_match_config(self):
        self.assertEqual(config.validate({"recording": {"fps": 240, "bitrate_mbps": 500}})["recording"]["fps"], 240)
        for bad in ({"fps": 241}, {"bitrate_mbps": 501}, {"resolution": "1600"}):
            with self.assertRaises(ValueError):
                config.validate({"recording": bad})
        with self.assertRaises(ValueError):
            config.validate({"recording": {"vaapi_device": "/etc/passwd"}})


class VolumeTests(unittest.TestCase):
    """M3: mute and unmute set an explicit state instead of toggling."""

    def test_explicit_mute_state(self):
        with patch("jarvis.hyprland.shutil.which", return_value="/usr/bin/wpctl"), \
                patch("jarvis.hyprland.subprocess.run", return_value=MagicMock(returncode=0)) as run:
            volume("volume_mute")
            volume("volume_unmute")
        self.assertEqual(run.call_args_list[0].args[0], ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"])
        self.assertEqual(run.call_args_list[1].args[0], ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])


class WaybarTests(unittest.TestCase):
    """M1: the bar shows 'off' and reconnects instead of disappearing."""

    def test_offline_then_online_then_offline(self):
        stop, calls, waits, lines = threading.Event(), [0], [], []
        def watch(callback, _stop):
            calls[0] += 1
            if calls[0] == 2:
                callback({"status": {"state": "idle", "message": "ready", "recording": {}}})
                return
            raise ConnectionRefusedError
        def wait(delay):
            waits.append(delay)
            if len(waits) == 4:
                stop.set()
        with patch("builtins.print", side_effect=lambda line, **_: lines.append(json.loads(line))):
            cli.run_waybar(watch, stop, wait)
        self.assertEqual([line["class"] for line in lines], ["offline", "idle", "offline"])
        self.assertEqual(waits, [2.0, 2.0, 4.0, 8.0])  # backoff resets after a successful connection


class TimeoutTests(unittest.TestCase):
    """M5: slow operations get a client timeout longer than the server's own wait."""

    def test_stop_recording_outlasts_recorder_finalization(self):
        # Recorder.stop: 12 s graceful + 2 s terminate + 2 s kill; ffprobe check up to 10 s.
        self.assertGreater(client.TIMEOUTS["record_stop"], 12 + 2 + 2 + 10)
        self.assertGreater(client.TIMEOUTS["execute"], 12 + 2 + 2 + 10)


class ServiceResilienceTests(unittest.IsolatedAsyncioTestCase):
    """H4/M2: voice failures are shown in the status instead of killing the service."""

    async def test_background_task_failure_is_reported(self):
        from jarvis.daemon import Service
        with patch("jarvis.daemon.AppRegistry", return_value=MagicMock(index={})):
            service = Service({"privacy": {"onboarded": True}, "activation": {"mode": "push_to_talk"}})
        service.loop = asyncio.get_running_loop()
        async def broken():
            raise CommandError("microphone missing")
        task = service.background(broken(), "voice restart")
        self.assertIn(task, service.tasks)  # strong reference while running
        with patch("sys.stderr"):
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
        self.assertNotIn(task, service.tasks)
        self.assertEqual(service.activation.state, "error")
        self.assertIn("microphone missing", service.message)

    async def test_close_yourself_stops_the_service(self):
        from jarvis.daemon import Service
        with patch("jarvis.daemon.AppRegistry", return_value=MagicMock(index={})):
            service = Service({"privacy": {"onboarded": True}, "activation": {"mode": "push_to_talk"}})
        for phrase in ("close yourself", "quit jarvis", "سكّر نفسك"):
            service.stopping.clear()
            result = await service.execute(phrase)
            self.assertTrue(result["ok"], phrase)
            self.assertTrue(service.stopping.is_set(), phrase)

    async def test_service_starts_even_if_voice_cannot(self):
        from jarvis import daemon
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "run").mkdir(mode=0o700)
            config.save({"privacy": {"onboarded": True}}, directory / "config.json")
            env = {"JARVIS_CONFIG": str(directory / "config.json"),
                   "JARVIS_SOCKET": str(directory / "run" / "control.sock")}
            with patch.dict(os.environ, env), \
                    patch("jarvis.daemon.AppRegistry", return_value=MagicMock(index={})), \
                    patch("jarvis.daemon.Recorder.inspect_previous"), \
                    patch.object(daemon.Service, "start_worker", side_effect=CommandError("grammar too large")):
                server = asyncio.create_task(daemon.serve())
                for _ in range(100):
                    if Path(env["JARVIS_SOCKET"]).is_socket():
                        break
                    await asyncio.sleep(0.02)
                status = await asyncio.to_thread(client.request, "status")
                os.kill(os.getpid(), __import__("signal").SIGTERM)
                await asyncio.wait_for(server, 5)
        self.assertEqual(status["status"]["state"], "error")
        self.assertIn("grammar too large", status["status"]["message"])

    async def test_shutdown_ends_open_status_subscriptions(self):
        """Settings and Waybar keep a watch open; stopping must not wait for them."""
        from jarvis import daemon
        import threading
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "run").mkdir(mode=0o700)
            config.save({}, directory / "config.json")
            env = {"JARVIS_CONFIG": str(directory / "config.json"),
                   "JARVIS_SOCKET": str(directory / "run" / "control.sock")}
            with patch.dict(os.environ, env), \
                    patch("jarvis.daemon.AppRegistry", return_value=MagicMock(index={})), \
                    patch("jarvis.daemon.Recorder.inspect_previous"):
                server = asyncio.create_task(daemon.serve())
                for _ in range(100):
                    if Path(env["JARVIS_SOCKET"]).is_socket():
                        break
                    await asyncio.sleep(0.02)
                seen, ended, stop = [], threading.Event(), threading.Event()
                def subscribe():
                    client.watch(seen.append, stop)
                    ended.set()
                threading.Thread(target=subscribe, daemon=True).start()
                for _ in range(100):
                    if seen:
                        break
                    await asyncio.sleep(0.02)
                os.kill(os.getpid(), __import__("signal").SIGTERM)
                await asyncio.wait_for(server, 5)
                self.assertTrue(await asyncio.to_thread(ended.wait, 5))
        self.assertTrue(seen)


@unittest.skipUnless(importlib.util.find_spec("pocketsphinx"), "optional voice dependency is not installed")
class DecoderGrammarTests(unittest.TestCase):
    """H1 with the real decoder: the optional-word grammar compiles and decodes."""

    def test_grammar_compiles_and_reports_unpronounceable_apps(self):
        from jarvis.speech import SpeechEngine
        engine = SpeechEngine(Parser().grammar(["firefox", "stremio"]), "hey desktop")
        self.assertIn("firefox", engine.apps)
        self.assertIn("open stremio", engine.unsupported_phrases)
        self.assertTrue(engine._matcher.accepts("could you please open firefox for me"))
        engine.close()

    def test_upstream_fixture_through_polite_grammar(self):
        """Regression: optional trailing words made the decoder end on a stray
        'for' and drop this real recording, so the grammar offers prefixes only."""
        path = os.environ.get("JARVIS_SPEECH_TEST_RAW")
        if not path:
            self.skipTest("set JARVIS_SPEECH_TEST_RAW to upstream goforward.raw (not bundled)")
        from jarvis.speech import SpeechEngine
        grammar = Parser().grammar(["firefox"])
        grammar["phrases"] = ["go forward ten meters"]
        engine = SpeechEngine(grammar)
        engine.begin_command()
        pcm = Path(path).read_bytes() + bytes(32000)
        events = [r for o in range(0, len(pcm) - engine.frame_bytes + 1, engine.frame_bytes)
                  if (r := engine.feed(pcm[o:o + engine.frame_bytes]))]
        self.assertEqual([e.text for e in events], ["go forward ten meters"])
        # Adding polite forms must not lower the score of the plain command
        # (it was 0.49 with the original flat grammar; an optional prefix gave 0.33).
        self.assertGreaterEqual(events[0].confidence, 0.45)
        engine.close()


if __name__ == "__main__":
    unittest.main()
