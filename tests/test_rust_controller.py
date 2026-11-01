"""Native-controller policy parity and isolated IPC/process integration.

All service tests use private temporary sockets/configs, a fake voice worker,
and fake desktop/capture backends. No live microphone or desktop action occurs.
"""
import copy
import dataclasses
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from murmur import config, commands
from murmur.apps import App, AppRegistry

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "native/controller/target/release/jarvis-controller"
FIXTURES = ROOT / "tests/fixtures"


def policies(requests):
    result = subprocess.run([str(BINARY), "--policy"], input="".join(json.dumps(r) + "\n" for r in requests),
                            text=True, capture_output=True, check=True, timeout=15)
    return [json.loads(line) for line in result.stdout.splitlines()]


def exited(pid):
    """True once the process is a zombie or gone; a dying process's stat can vanish mid-read."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].startswith("Z")
    except OSError:
        return True


class ControllerTablesTests(unittest.TestCase):
    def test_embedded_tables_match_the_python_api(self):
        data = ROOT / "native/controller/data"
        self.assertEqual(json.loads((data / "defaults.json").read_text()), config.DEFAULTS)
        tables = json.loads((data / "commands.json").read_text())
        for key, value in tables.items():
            reference = getattr(commands, key)
            if key == "FIXED":
                reference = {k: dataclasses.asdict(v) for k, v in reference.items()}
            self.assertEqual(value, json.loads(json.dumps(reference)), key)


@unittest.skipUnless(BINARY.is_file(), "build the optional Rust controller")
class ControllerPolicyTests(unittest.TestCase):
    def test_binary_runs_without_python_and_links_no_interpreter(self):
        output = subprocess.check_output([str(BINARY), "--version"], text=True,
                                         env=dict(os.environ, PATH="/nonexistent", LD_LIBRARY_PATH=""))
        self.assertIn("Rust service", output)
        linked = subprocess.check_output(["ldd", str(BINARY)], text=True).lower()
        for library in ("libpython", "libgtk", "sherpa", "onnxruntime"):
            self.assertNotIn(library, linked)

    def test_parser_matches_every_rule_and_rejects_invalid_commands(self):
        custom = {"computer be quiet": "mute volume", "please do this for me": "save replay"}
        parser = commands.Parser(custom)
        grammar = parser.grammar(["firefox", "tekken eight", "steam", "settings"])
        phrases = list(grammar["phrases"]) + [f"{verb} {app}" for verb in commands.APP_VERBS for app in grammar["apps"]]
        phrases += [prefix + " " + p for prefix in commands.POLITE_PREFIXES for p in phrases[:60]]
        phrases += [p + " " + suffix for suffix in commands.POLITE_SUFFIXES for p in phrases[:60]]
        phrases += ["go to workspace " + str(i) for i in range(0, 13)]
        phrases += ["go to workspace -3", "go to workspace + 3", "rm -rf /", "", "please", "x" * 257,
                    "OPEN FIREFOX!", "open ﬁrefox", "open straße", "go to workspace ٣", "open literal $() text"]
        actual = policies([{"op": "parse", "text": p, "custom": custom} for p in phrases])
        for phrase, result in zip(phrases, actual, strict=True):
            try:
                expected = dataclasses.asdict(parser.parse(phrase))
            except ValueError:
                self.assertFalse(result["ok"], phrase)
            else:
                self.assertTrue(result["ok"], (phrase, result))
                self.assertEqual(result["result"]["intent"], expected, phrase)

    def test_normalization_grammar_and_limits_match(self):
        phrases = ["ＧＯ　ＴＯ WORKSPACE 3", "Make this FULLSCREEN", "Straße", "İ Σ ς ﬃ café", "a\x1cb\t_c!", "x" * 257]
        results = policies([{"op": "normalize", "text": p} for p in phrases])
        for phrase, result in zip(phrases, results, strict=True):
            try:
                expected = commands.normalize(phrase)
            except ValueError:
                self.assertFalse(result["ok"])
            else:
                self.assertEqual(result["result"]["normalized"], expected)
        apps = ["app " + str(i) for i in range(2055)] + ["app 2", ""]
        custom = {"my shortcut": "cancel"}
        result = policies([{"op": "grammar", "apps": apps, "custom": custom}])[0]
        self.assertEqual(result["result"]["grammar"], commands.Parser(custom).grammar(apps))

    def test_config_validation_matches_types_ranges_and_privacy_rules(self):
        cases = [{}, {"recognition": {"noise_suppression": True}}, {"commands": {"computer be quiet": "mute volume"}},
                 {"unknown": 1}, {"audio": {"unknown": 1}}, [], {"privacy": None}, {"commands": {"cancel": "cancel"}},
                 {"commands": {"my phrase": "rm -rf /"}}, {"aliases": {"Firefox": "a", "firefox": "b"}},
                 {"aliases": {"": "a"}}, {"activation": {"wake_phrase": "!!!"}}, {"audio": {"device": "a\0b"}},
                 {"recording": {"system_audio": True, "microphone": True}},
                 {"recording": {"system_audio": True, "microphone": True, "combined_audio_source": True, "audio_device": "mix"}}]
        for section, key, values in [("activation", "command_timeout", [True, 1, 2, 15, 16, "8"]),
                                      ("activation", "confidence", [False, -1, 0, .5, 1, 2]),
                                      ("recording", "fps", [True, 0, 1, 30.0, 60, 61]),
                                      ("replay", "seconds", [True, 9, 10, 600, 601]),
                                      ("replay", "storage", ["ram", "disk", "auto", None]),
                                      ("replay", "max_mb", [True, 63, 64, 256.0, 16384, 16385]),
                                      ("replay", "buffer_dir", [None, "a\0b", "", "/tmp/private"]),
                                      ("recognition", "noise_suppression", [0, "yes", None, False, True])]:
            cases += [{section: {key: v}} for v in values]
        results = policies([{"op": "validate", "config": c} for c in cases])
        for case, result in zip(cases, results, strict=True):
            try:
                expected = config.validate(case)
            except ValueError:
                self.assertFalse(result["ok"], case)
            else:
                self.assertTrue(result["ok"], (case, result))
                self.assertEqual(result["result"]["config"], expected)

    def test_app_registry_precedence_aliases_and_window_classes_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user, system = root / "user", root / "system"
            user.mkdir(); system.mkdir()
            entries = {system / "firefox.desktop": ("Firefox", "firefox %u", "StartupWMClass=firefox"),
                       system / "org.mozilla.firefox.desktop": ("Firefox", "flatpak run org.mozilla.firefox", "StartupWMClass=firefox"),
                       system / "hidden.desktop": ("Hidden", "hidden", ""),
                       user / "hidden.desktop": ("Hidden", "hidden", "Hidden=true"),
                       system / "steam_game.desktop": ("TEKKEN 8", "steam steam://rungameid/1778820", ""),
                       system / "only.desktop": ("Only KDE", "only", "OnlyShowIn=KDE;"),
                       system / "dbus.desktop": ("DBus app", "", "DBusActivatable=true")}
            for path, (name, execute, extra) in entries.items():
                path.write_text(f"[Desktop Entry]\nType=Application\nName={name}\nExec={execute}\n{extra}\n")
            aliases = {"browser": "firefox.desktop"}
            with patch.dict(os.environ, XDG_CURRENT_DESKTOP="Hyprland"):
                expected = AppRegistry(aliases, [user, system])
                actual = policies([{"op": "apps", "aliases": aliases, "roots": [str(user), str(system)], "resolve": "browser"}])[0]
            self.assertEqual(actual["result"]["apps"], expected.list())
            self.assertEqual(actual["result"]["resolved"], dataclasses.asdict(expected.resolve("browser")))
        apps = [App("org.mozilla.firefox.desktop", "Firefox", "/usr/share/firefox.desktop", "firefox %u"),
                App("steam.desktop", "Steam", "/usr/share/steam.desktop", "steam"),
                App("game.desktop", "Game", "/usr/share/game.desktop", "steam steam://rungameid/123"),
                App("web.desktop", "Web", "/usr/share/web.desktop", "chromium --app-id=test"),
                App("F.E.A.R. 2.desktop", "Fear", "/usr/share/fear.desktop", "\"/a path/game\" %u"),
                App("broken.desktop", "Broken", "/usr/share/broken.desktop", "'broken")]
        results = policies([{"op": "classes", "app": dataclasses.asdict(a)} for a in apps])
        for app, result in zip(apps, results, strict=True):
            self.assertEqual(set(result["result"]["classes"]), app.classes())


class Harness:
    def __init__(self, *, onboarded=False, dry_run=True, config_patch=None, accurate=False,
                 real_worker=None, real_library=None, real_model=None, pcm=None, controller_argv=None, **environment):
        parent = None
        if (config_patch or {}).get("replay", {}).get("storage") == "disk":
            parent = ROOT / "build/replay-storage/test-tmp"
            parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="jarvis-controller-", dir=parent)
        self.root = Path(self.directory.name)
        self.socket = self.root / "ipc/control.sock"
        self.config_path = self.root / "config.json"
        self.log = self.root / "processes.jsonl"
        self.cfg = config.validate(config_patch or {})
        self.cfg["privacy"]["onboarded"] = onboarded
        self.cfg["recognition"]["accurate"] = accurate
        self.cfg["recording"].update(configured=True, monitor="DP-1", output_dir=str(self.root / "videos"))
        self.cfg["replay"]["sound"] = False
        model = self.root / "model"
        for name in ("am/final.mdl", "conf/model.conf", "graph/Gr.fst", "graph/HCLr.fst"):
            path = model / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stub")
        self.cfg["recognition"]["model_path"] = str(real_model or model)
        self.config_path.write_text(json.dumps(self.cfg))
        library = self.root / "libvosk.so"; library.write_bytes(b"stub")
        self.bin = self.root / "bin"; self.bin.mkdir()
        for program in ("hyprctl", "pactl", "pw-dump", "ffprobe", "wf-recorder", "gpu-screen-recorder", "gio", "wpctl", "pw-play", "systemctl"):
            path = self.bin / program
            path.write_bytes((FIXTURES / "controller_backend.py").read_bytes())
            path.chmod(0o755)
        worker = self.root / "voice"
        worker.write_bytes((FIXTURES / "controller_voice.py").read_bytes()); worker.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.root), XDG_DATA_HOME=str(self.root / "data"), XDG_DATA_DIRS=str(self.root / "system"),
                        XDG_CONFIG_HOME=str(self.root / "config"), MURMUR_CONFIG=str(self.config_path), MURMUR_SOCKET=str(self.socket),
                        MURMUR_RUST_VOICE_BINARY=str(real_worker or worker), MURMUR_VOSK_LIBRARY=str(real_library or library), MURMUR_MANAGED_SERVICE="0",
                        MURMUR_TEST_LOG=str(self.log), MURMUR_DRY_RUN="1" if dry_run else "0",
                        PATH=str(self.bin) + ":" + os.environ.get("PATH", ""))
        self.env.update(environment)
        if pcm:
            capture = self.bin / "pw-record"
            capture.write_bytes((FIXTURES / "controller_pcm_capture.py").read_bytes())
            capture.chmod(0o755)
            self.env["MURMUR_TEST_PCM"] = str(pcm)
        self.output = (self.root / "stderr").open("w+")
        self.process = subprocess.Popen(controller_argv or [str(BINARY)], env=self.env, stdout=self.output, stderr=self.output)
        self.wait(lambda: self.socket.exists())

    def wait(self, predicate, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return
            if self.process.poll() is not None:
                self.output.seek(0)
                raise AssertionError("Controller exited: " + self.output.read())
            time.sleep(.01)
        self.output.seek(0)
        raise AssertionError("Timed out: " + self.output.read())

    def rpc(self, op, **fields):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(15)
            connection.connect(str(self.socket))
            connection.sendall((json.dumps({"op": op, **fields}) + "\n").encode())
            with connection.makefile("rb") as stream:
                return json.loads(stream.readline())

    def status(self):
        return self.rpc("status")["status"]

    def events(self, program=None):
        if not self.log.exists():
            return []
        values = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [v for v in values if program is None or v["program"] == program]

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=25)
        finally:
            if self.process.poll() is None:
                self.process.kill(); self.process.wait()
            self.output.close()
            self.directory.cleanup()


@unittest.skipUnless(BINARY.is_file(), "build the optional Rust controller")
class ControllerServiceTests(unittest.TestCase):
    def start(self, **kwargs):
        harness = Harness(**kwargs)
        self.addCleanup(harness.close)
        return harness

    def test_private_ipc_consent_and_single_service_lock(self):
        h = self.start()
        status = h.status()
        self.assertEqual(status["controller"], "rust")
        self.assertTrue(status["muted"])
        self.assertFalse(status["microphone_open"])
        self.assertFalse(h.rpc("listen")["ok"])
        self.assertFalse(h.rpc("mute", muted=False)["ok"])
        self.assertEqual(h.events("voice"), [])
        self.assertEqual(h.socket.stat().st_mode & 0o777, 0o600)
        self.assertEqual(h.socket.parent.stat().st_mode & 0o777, 0o700)
        with (h.socket.parent / "service.lock").open("a") as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        duplicate = subprocess.run([str(BINARY)], env=h.env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("already running", duplicate.stderr)
        self.assertTrue(h.rpc("status")["ok"])

    def test_status_and_doctor_include_owned_worker_memory(self):
        h = self.start(onboarded=True)
        h.wait(lambda: h.status()["engine"] is not None)
        response = h.rpc("status")
        self.assertEqual(response["pid"], h.process.pid)
        memory = response["memory"]
        self.assertTrue(memory["complete"])
        self.assertIn(h.events("voice")[0]["pid"], [row["pid"] for row in memory["processes"]])
        for key in ("rss_bytes", "pss_bytes"):
            self.assertEqual(memory["totals"][key], sum(row[key] for row in memory["processes"]))
        cli = ROOT / "native/cli/target/release/jarvis"
        if cli.is_file():
            result = subprocess.run([str(cli), "doctor"], env=h.env, capture_output=True, text=True, timeout=15, check=True)
            self.assertEqual(json.loads(result.stdout)["memory"]["roots"], [h.process.pid])

    def test_configuration_is_atomic_private_and_restarts_voice_after_consent(self):
        h = self.start()
        updated = h.rpc("config")["config"]
        updated["privacy"]["onboarded"] = True
        saved = h.rpc("save_config", config=updated)
        self.assertTrue(saved["ok"], saved)
        h.wait(lambda: h.status()["engine"] is not None)
        self.assertEqual(json.loads(h.config_path.read_text()), updated)
        self.assertEqual(h.config_path.stat().st_mode & 0o777, 0o600)
        invalid = copy.deepcopy(updated); invalid["commands"] = {"evil": "rm -rf /"}
        self.assertFalse(h.rpc("save_config", config=invalid)["ok"])
        self.assertEqual(h.rpc("config")["config"], updated)
        self.assertEqual(list(h.root.glob(".config-*")), [])

    def test_cli_status_and_allowlisted_execution_keep_the_existing_api(self):
        h = self.start()
        result = subprocess.run([sys.executable, "-m", "murmur", "status"], env=h.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"]["controller"], "rust")
        self.assertEqual(h.rpc("commands")["commands"], commands.REFERENCE)
        self.assertTrue(h.rpc("execute", phrase="go to workspace three")["ok"])
        self.assertFalse(h.rpc("execute", phrase="rm -rf /", command="anything")["ok"])
        self.assertFalse(h.rpc("execute", phrase="go to workspace -3")["ok"])
        self.assertEqual(h.events("hyprctl"), [])
        self.assertEqual(h.rpc("devices")["devices"][1]["id"], "fake-mic")
        self.assertEqual(h.rpc("monitors")["monitors"][0]["name"], "DP-1")
        self.assertTrue(h.rpc("recording_devices")["ok"])

    def test_existing_daemon_launcher_execs_rust_without_a_python_parent(self):
        h = self.start(controller_argv=[sys.executable, "-m", "murmur", "daemon"], MURMUR_CONTROLLER="rust")
        self.assertTrue(Path(f"/proc/{h.process.pid}/exe").readlink().name.startswith("jarvis-controller"))
        self.assertNotIn("libpython", Path(f"/proc/{h.process.pid}/maps").read_text().lower())

    def test_listen_and_cancel_keep_the_same_worker_and_stale_events_do_not_dispatch(self):
        h = self.start(onboarded=True, MURMUR_TEST_VOICE="stale")
        h.wait(lambda: h.status()["engine"] is not None)
        pid = h.events("voice")[0]["pid"]
        self.assertTrue(h.rpc("listen")["ok"])
        h.wait(lambda: h.status()["message"] == "Dry run: workspace")
        self.assertTrue(h.rpc("cancel")["ok"])
        h.wait(lambda: h.status()["message"].startswith("Waiting for"))
        self.assertEqual([v["pid"] for v in h.events("voice") if v["event"] == "start"], [pid])
        self.assertEqual([v["line"] for v in h.events("voice") if v["event"] == "control"], ["listen", "cancel"])
        self.assertTrue(h.rpc("mute", muted=True)["ok"])
        h.wait(lambda: not Path(f"/proc/{pid}").exists())
        self.assertFalse(h.status()["microphone_open"])

    def test_mute_wins_while_the_worker_is_loading(self):
        h = self.start(onboarded=True, MURMUR_TEST_VOICE="delayed")
        h.wait(lambda: bool(h.events("voice")))
        self.assertTrue(h.rpc("mute", muted=True)["ok"])
        time.sleep(.1)
        self.assertTrue(h.status()["muted"])
        self.assertFalse(h.status()["microphone_open"])
        self.assertEqual(h.status()["state"], "paused")

    def test_push_to_talk_dispatches_its_result_before_reaping_the_worker(self):
        h = self.start(onboarded=True, dry_run=False, MURMUR_TEST_VOICE="command",
                       config_patch={"activation": {"mode": "push_to_talk"}})
        self.assertFalse(h.status()["microphone_open"])
        self.assertTrue(h.rpc("listen")["ok"])
        h.wait(lambda: any(v["args"][:1] == ["dispatch"] for v in h.events("hyprctl")))
        h.wait(lambda: not h.status()["microphone_open"])
        self.assertEqual(h.status()["state"], "paused")
        self.assertEqual(len([v for v in h.events("voice") if v["event"] == "start"]), 1)

    def test_missing_native_dependencies_report_an_error_without_starting_voice(self):
        h = self.start(onboarded=True, MURMUR_VOSK_LIBRARY="/not/a/real/vosk/library")
        h.wait(lambda: h.status()["state"] == "error")
        self.assertFalse(h.status()["microphone_open"])
        self.assertEqual(h.events("voice"), [])

    def test_bad_worker_output_becomes_an_error_without_a_restart_loop(self):
        for scenario in ("invalid", "oversized", "exit"):
            with self.subTest(scenario=scenario):
                h = self.start(onboarded=True, MURMUR_TEST_VOICE=scenario)
                h.wait(lambda: h.status()["state"] == "error")
                self.assertEqual(len([v for v in h.events("voice") if v["event"] == "start"]), 1)
                self.assertIsNone(h.process.poll())

    def test_watch_receives_changes_and_disconnects_without_polling(self):
        h = self.start()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(3); connection.connect(str(h.socket)); connection.sendall(b'{"op":"watch"}\n')
            with connection.makefile("rb") as stream:
                self.assertTrue(json.loads(stream.readline())["ok"])
                h.rpc("execute", phrase="cancel")
                self.assertEqual(json.loads(stream.readline())["status"]["message"], "Dry run: cancel")

    def test_oversized_or_nonobject_requests_cannot_break_the_service(self):
        h = self.start()
        for payload in (b"[]\n", b"x" * 65537 + b"\n", b"not json\n", b'{"op":"unknown"}\n'):
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(3); connection.connect(str(h.socket)); connection.sendall(payload)
                with connection.makefile("rb") as stream:
                    self.assertFalse(json.loads(stream.readline())["ok"])
            self.assertTrue(h.rpc("status")["ok"])

    def test_native_controller_maps_and_shutdown_cleanup(self):
        h = self.start(onboarded=True)
        h.wait(lambda: h.status()["engine"] is not None)
        pid = h.events("voice")[0]["pid"]
        maps = Path(f"/proc/{h.process.pid}/maps").read_text().lower()
        self.assertNotIn("libpython", maps)
        h.process.terminate(); h.process.wait(timeout=5)
        self.assertFalse(h.socket.exists())
        self.assertFalse(Path(f"/proc/{pid}").exists())

    def test_parent_death_signal_stops_voice_after_hard_controller_failure(self):
        # The default fixture also exits when the control pipe closes; this one does not.
        h = self.start(onboarded=True, MURMUR_TEST_VOICE="linger")
        h.wait(lambda: h.status()["engine"] is not None)
        pid = h.events("voice")[0]["pid"]
        # An already-exited worker would pass without exercising PR_SET_PDEATHSIG.
        self.assertFalse(exited(pid), "Voice worker exited before the controller's death")
        h.process.kill(); h.process.wait(timeout=5)
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if exited(pid):
                break
            time.sleep(.01)
        else:
            self.fail("Voice worker survived the controller's death")

    def test_parent_death_signal_stops_recorders_and_preserves_partial_video(self):
        h = self.start(dry_run=False)
        self.assertTrue(h.rpc("record_start")["ok"])
        self.assertTrue(h.rpc("replay_start")["ok"])
        h.wait(lambda: bool(h.events("wf-recorder")) and bool(h.events("gpu-screen-recorder")))
        pids = [h.events(name)[0]["pid"] for name in ("wf-recorder", "gpu-screen-recorder")]
        self.assertFalse(any(exited(pid) for pid in pids), "A recorder exited before the controller's death")
        h.process.kill(); h.process.wait(timeout=5)
        for pid in pids:
            end = time.monotonic() + 3
            while time.monotonic() < end:
                if exited(pid):
                    break
                time.sleep(.01)
            else:
                os.kill(pid, signal.SIGKILL)
                self.fail("Capture survived the controller's death")
        partials = list((h.root / "videos").glob(".Murmur_*.part.mkv"))
        self.assertEqual(len(partials), 1)
        self.assertGreater(partials[0].stat().st_size, 0)

    def test_startup_setting_uses_the_existing_unit_and_persists_only_the_flag(self):
        h = self.start()
        before = h.rpc("config")["config"]
        self.assertTrue(h.rpc("set_startup", enabled=True)["ok"])
        before["startup"] = True
        self.assertEqual(h.rpc("config")["config"], before)
        self.assertEqual(h.events("systemctl")[0]["args"], ["--user", "enable", "murmur.service"])
        self.assertEqual(json.loads(h.config_path.read_text()), before)
        self.assertFalse(h.rpc("set_startup", enabled="yes")["ok"])

    def test_recording_finalizes_once_and_capture_settings_are_guarded(self):
        h = self.start(dry_run=False)
        first = h.rpc("record_start"); self.assertTrue(first["ok"], first)
        self.assertTrue(h.rpc("record_start")["ok"])
        h.wait(lambda: bool(h.events("wf-recorder")))
        self.assertEqual(len(h.events("wf-recorder")), 1)
        changed = h.rpc("config")["config"]; changed["recording"]["fps"] = 60
        self.assertFalse(h.rpc("save_config", config=changed)["ok"])
        stopped = h.rpc("record_stop"); self.assertTrue(stopped["ok"], stopped)
        path = Path(h.status()["recording"]["file"])
        self.assertTrue(path.exists()); self.assertFalse(path.name.startswith("."))
        self.assertEqual(list(path.parent.glob("*.part.mkv")), [])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_replay_preserves_ram_buffer_and_saves_through_the_native_socket(self):
        h = self.start(dry_run=False)
        started = h.rpc("replay_start"); self.assertTrue(started["ok"], started)
        h.wait(lambda: (h.socket.parent / "replay.sock").exists())
        argv = h.events("gpu-screen-recorder")[0]["args"]
        self.assertEqual(argv[argv.index("-replay-storage") + 1], "ram")
        self.assertEqual(argv[argv.index("-a") + 1], "default_output|default_input")
        saved = h.rpc("replay_save"); self.assertTrue(saved["ok"], saved)
        self.assertTrue(Path(h.status()["replay"]["file"]).exists())
        requests = [e["request"] for e in h.events("gpu-screen-recorder") if e.get("event") == "ipc_request"]
        self.assertEqual(requests, [{"id": 1, "name": "save-replay", "data": {"seconds": 120}}])
        changed = h.rpc("config")["config"]; changed["replay"]["seconds"] = 60
        self.assertFalse(h.rpc("save_config", config=changed)["ok"])
        self.assertTrue(h.rpc("replay_stop")["ok"])
        self.assertEqual(h.status()["replay"]["state"], "off")

    def test_disk_replay_is_private_and_saved_clips_survive_buffer_cleanup(self):
        h = self.start(dry_run=False, config_patch={"replay": {"storage": "disk", "max_mb": 512}})
        self.assertTrue(h.rpc("replay_start")["ok"])
        h.wait(lambda: (h.socket.parent / "replay.sock").exists())
        argv = next(e["args"] for e in h.events("gpu-screen-recorder") if "-ipc" in e["args"])
        self.assertEqual(argv[argv.index("-replay-storage") + 1], "disk")
        buffer = Path(argv[argv.index("-o") + 1])
        self.assertEqual(buffer.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("-ro", argv)
        self.assertTrue(h.rpc("replay_save")["ok"])
        clip = Path(h.status()["replay"]["file"])
        self.assertEqual(clip.parent, h.root / "videos")
        self.assertEqual(clip.read_bytes(), b"fake replay 1")
        requests = [e["request"] for e in h.events("gpu-screen-recorder") if e.get("event") == "ipc_request"]
        self.assertEqual(requests, [{"id": 1, "name": "save-replay", "data": {"seconds": 120}}])
        for key, value in (("storage", "ram"), ("max_mb", 1024), ("buffer_dir", str(h.root / "other"))):
            changed = h.rpc("config")["config"]
            changed["replay"][key] = value
            self.assertFalse(h.rpc("save_config", config=changed)["ok"])
        self.assertTrue(h.rpc("replay_stop")["ok"])
        self.assertFalse(buffer.exists())
        self.assertTrue(clip.exists())

    def test_replay_save_requests_configured_seconds_after_each_start(self):
        for storage in ("ram", "disk"):
            with self.subTest(storage=storage):
                h = self.start(dry_run=False, config_patch={"replay": {"storage": storage, "seconds": 30, "max_mb": 512}})
                for starts, seconds in enumerate((30, 60), 1):
                    if seconds != 30:
                        changed = h.rpc("config")["config"]
                        changed["replay"]["seconds"] = seconds
                        self.assertTrue(h.rpc("save_config", config=changed)["ok"])
                    self.assertTrue(h.rpc("replay_start")["ok"])
                    # The old socket pathname can survive a stopped fake;
                    # wait until the new backend has actually rebound it.
                    h.wait(lambda: sum(e.get("event") == "ipc_ready" for e in h.events("gpu-screen-recorder")) == starts)
                    result = h.rpc("replay_save")
                    self.assertTrue(result["ok"], result)
                    requests = [e["request"] for e in h.events("gpu-screen-recorder") if e.get("event") == "ipc_request"]
                    self.assertEqual(requests[-1], {"id": 1, "name": "save-replay", "data": {"seconds": seconds}})
                    self.assertTrue(h.rpc("replay_stop")["ok"])
                self.assertEqual([request["data"]["seconds"] for request in requests], [30, 60])

    def test_disk_replay_repeated_backend_names_never_overwrite_saved_clips(self):
        h = self.start(dry_run=False, config_patch={"replay": {"storage": "disk", "max_mb": 512}})
        folder = h.root / "videos"
        folder.mkdir()
        existing = folder / "Replay_test.mp4"
        existing.write_bytes(b"previous user clip")
        self.assertTrue(h.rpc("replay_start")["ok"])
        h.wait(lambda: (h.socket.parent / "replay.sock").exists())
        clips = []
        for index in (1, 2):
            saved = h.rpc("replay_save")
            self.assertTrue(saved["ok"], saved)
            clip = Path(h.status()["replay"]["file"])
            self.assertEqual(clip.parent, folder)
            self.assertNotIn(clip, [existing, *clips])
            self.assertEqual(clip.read_bytes(), f"fake replay {index}".encode())
            clips.append(clip)
        self.assertTrue(h.rpc("replay_stop")["ok"])
        self.assertEqual(existing.read_bytes(), b"previous user clip")
        self.assertEqual([clip.read_bytes() for clip in clips], [b"fake replay 1", b"fake replay 2"])

    def test_disk_replay_invalid_hdr_clip_survives_verification_and_cleanup(self):
        h = self.start(dry_run=False, config_patch={"replay": {"storage": "disk", "max_mb": 512}},
                       MURMUR_TEST_COLOR="hdr", MURMUR_TEST_BAD_HDR="1")
        self.assertTrue(h.rpc("replay_start")["ok"])
        h.wait(lambda: (h.socket.parent / "replay.sock").exists())
        result = h.rpc("replay_save")
        self.assertFalse(result["ok"], result)
        self.assertIn("HDR verification failed", result["error"])
        clips = list((h.root / "videos").glob("*.mp4"))
        self.assertEqual(len(clips), 1)
        clip = clips[0]
        self.assertIn(str(clip), result["error"])
        self.assertEqual(clip.read_bytes(), b"fake replay 1")
        self.assertTrue(h.rpc("replay_stop")["ok"])
        self.assertEqual(clip.read_bytes(), b"fake replay 1")
        self.assertEqual(list((h.root / "data/murmur/replay-buffer").glob("session-*")), [])

    def test_disk_replay_publishes_to_a_different_filesystem(self):
        shared_memory = Path("/dev/shm")
        if not shared_memory.is_dir() or not os.access(shared_memory, os.W_OK):
            self.skipTest("a private writable tmpfs destination is unavailable")
        with tempfile.TemporaryDirectory(prefix="jarvis-replay-clips-", dir=shared_memory) as directory:
            folder = Path(directory)
            h = self.start(dry_run=False, config_patch={"replay": {"storage": "disk", "max_mb": 512}})
            if folder.stat().st_dev == h.root.stat().st_dev:
                self.skipTest("the clip and disk-buffer filesystems are identical")
            changed = h.rpc("config")["config"]
            changed["recording"]["output_dir"] = str(folder)
            self.assertTrue(h.rpc("save_config", config=changed)["ok"])
            self.assertTrue(h.rpc("replay_start")["ok"])
            h.wait(lambda: (h.socket.parent / "replay.sock").exists())
            event = next(e for e in h.events("gpu-screen-recorder") if "-ipc" in e["args"])
            session = Path(event["args"][event["args"].index("-o") + 1])
            self.assertNotEqual(session.stat().st_dev, folder.stat().st_dev)
            result = h.rpc("replay_save")
            self.assertTrue(result["ok"], result)
            clip = Path(h.status()["replay"]["file"])
            self.assertEqual(clip.parent, folder)
            self.assertEqual(clip.read_bytes(), b"fake replay 1")
            self.assertEqual(clip.stat().st_mode & 0o777, 0o600)
            self.assertTrue(h.rpc("replay_stop")["ok"])
            self.assertFalse(session.exists())
            self.assertEqual(clip.read_bytes(), b"fake replay 1")

    def test_disk_replay_clip_survives_recorder_exit_during_verification(self):
        for reason in ("pressure", "exit"):
            with self.subTest(reason=reason):
                h = self.start(dry_run=False, config_patch={"replay": {"storage": "disk", "max_mb": 512}},
                               MURMUR_TEST_VERIFY_DELAY="1.2")
                self.assertTrue(h.rpc("replay_start")["ok"])
                h.wait(lambda: (h.socket.parent / "replay.sock").exists())
                event = next(e for e in h.events("gpu-screen-recorder") if "-ipc" in e["args"])
                session = Path(event["args"][event["args"].index("-o") + 1])
                results = []
                thread = threading.Thread(target=lambda: results.append(h.rpc("replay_save")))
                thread.start()
                self.addCleanup(thread.join)
                h.wait(lambda: bool(h.events("ffprobe")))
                if reason == "pressure":
                    # Sparse data triggers supervision without allocating 513 MiB.
                    with (session / "gsr-replay-fake.gsr/Replay_0.gsr").open("r+b") as stream:
                        stream.truncate(513 * 1024**2)
                else:
                    os.kill(event["pid"], signal.SIGTERM)
                h.wait(lambda: exited(event["pid"]))
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0]["ok"], results)
                h.wait(lambda: not session.exists())
                status = h.status()["replay"]
                self.assertEqual(status["state"], "error")
                if reason == "pressure":
                    self.assertIn("size budget", status["message"])
                clip = Path(status["file"])
                self.assertEqual(clip.parent, h.root / "videos")
                self.assertEqual(clip.read_bytes(), b"fake replay 1")
                self.assertTrue(h.rpc("replay_stop")["ok"])
                self.assertEqual(clip.read_bytes(), b"fake replay 1")

    def test_disk_budget_and_write_failure_stop_only_the_owned_recorder(self):
        # The fake buffer file is sparse, so this needs no real disk space.
        for environment, expected in (({"MURMUR_TEST_REPLAY_BYTES": str(513 * 1024**2)}, "size budget"),
                                      ({"MURMUR_TEST_REPLAY_WRITE_ERROR": "1"}, "could not write")):
            with self.subTest(environment=environment):
                h = self.start(dry_run=False, config_patch={"replay": {"storage": "disk", "max_mb": 512}}, **environment)
                self.assertTrue(h.rpc("replay_start")["ok"])
                h.wait(lambda: h.status()["replay"]["state"] == "error")
                self.assertIn(expected, h.status()["replay"]["message"])
                event = next(e for e in h.events("gpu-screen-recorder") if "-ipc" in e["args"])
                self.assertTrue(exited(event["pid"]))
                self.assertEqual(list((h.root / "data/murmur/replay-buffer").glob("session-*")), [])

    def test_disk_budget_holds_a_full_recorder_file_and_refuses_smaller_budgets(self):
        # GSR fills a 256 MiB file (overshooting by its last packet) before it
        # can remove anything, so 512 MiB must keep running while 256 cannot.
        h = self.start(dry_run=False, config_patch={"replay": {"storage": "disk", "max_mb": 512}},
                       MURMUR_TEST_REPLAY_BYTES=str(257 * 1024**2))
        self.assertTrue(h.rpc("replay_start")["ok"])
        h.wait(lambda: (h.socket.parent / "replay.sock").exists())
        time.sleep(1)
        self.assertEqual(h.status()["replay"]["state"], "on")
        self.assertTrue(h.rpc("replay_stop")["ok"])
        small = self.start(dry_run=False, config_patch={"replay": {"storage": "disk", "max_mb": 256}})
        response = small.rpc("replay_start")
        self.assertFalse(response["ok"])
        self.assertIn("at least 512 MiB", response["error"])
        self.assertFalse(any("-ipc" in e["args"] for e in small.events("gpu-screen-recorder")))
        self.assertFalse((small.root / "data/murmur/replay-buffer").exists())

    def test_disk_replay_cleans_crash_leftovers_on_next_start(self):
        h = self.start(dry_run=False, config_patch={"replay": {"storage": "disk", "max_mb": 512}})
        self.assertTrue(h.rpc("replay_start")["ok"])
        h.wait(lambda: (h.socket.parent / "replay.sock").exists())
        event = next(e for e in h.events("gpu-screen-recorder") if "-ipc" in e["args"])
        buffer = Path(event["args"][event["args"].index("-o") + 1])
        h.process.kill()
        h.process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while not exited(event["pid"]) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(exited(event["pid"]))
        self.assertTrue(buffer.exists())
        h.socket.unlink(missing_ok=True)
        h.process = subprocess.Popen([str(BINARY)], env=h.env, stdout=h.output, stderr=h.output)
        h.wait(lambda: h.socket.exists())
        self.assertTrue(h.rpc("replay_start")["ok"])
        self.assertFalse(buffer.exists())
        self.assertTrue(h.rpc("replay_stop")["ok"])

    def test_ram_replay_is_not_stopped_by_recorder_memory(self):
        # GSR has no RAM byte cap and its RSS includes encoder overhead, so an
        # RSS limit would end replay rather than shorten it.
        h = self.start(dry_run=False, config_patch={"replay": {"max_mb": 64}},
                       MURMUR_TEST_REPLAY_RAM_BYTES=str(65 * 1024**2))
        self.assertTrue(h.rpc("replay_start")["ok"])
        h.wait(lambda: (h.socket.parent / "replay.sock").exists())
        time.sleep(1)
        self.assertEqual(h.status()["replay"]["state"], "on")
        self.assertIn("in memory", h.status()["replay"]["message"])
        self.assertTrue(h.rpc("replay_stop")["ok"])

    def test_status_and_mute_remain_responsive_during_video_verification(self):
        h = self.start(onboarded=True, dry_run=False, MURMUR_TEST_VERIFY_DELAY=".7")
        h.wait(lambda: h.status()["engine"] is not None)
        self.assertTrue(h.rpc("record_start")["ok"])
        h.wait(lambda: bool(h.events("wf-recorder")))
        result = []
        thread = threading.Thread(target=lambda: result.append(h.rpc("record_stop")))
        thread.start(); self.addCleanup(thread.join)
        h.wait(lambda: bool(h.events("ffprobe")))
        start = time.monotonic()
        self.assertTrue(h.rpc("mute", muted=True)["ok"])
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(h.status()["muted"])
        thread.join(timeout=3); self.assertTrue(result[0]["ok"], result)

    def test_device_discovery_does_not_block_mute_or_status(self):
        h = self.start(onboarded=True, MURMUR_TEST_QUERY_DELAY=".7")
        h.wait(lambda: h.status()["engine"] is not None)
        result = []
        thread = threading.Thread(target=lambda: result.append(h.rpc("devices")))
        thread.start(); self.addCleanup(thread.join)
        h.wait(lambda: bool(h.events("pw-dump")))
        start = time.monotonic()
        self.assertTrue(h.rpc("mute", muted=True)["ok"])
        self.assertTrue(h.status()["muted"])
        self.assertLess(time.monotonic() - start, .5)
        thread.join(timeout=3); self.assertTrue(result[0]["ok"], result)

    def test_hdr_verification_failure_retains_the_partial_recording(self):
        h = self.start(dry_run=False, MURMUR_TEST_COLOR="hdr", MURMUR_TEST_BAD_HDR="1")
        self.assertTrue(h.rpc("record_start")["ok"])
        h.wait(lambda: bool(h.events("gpu-screen-recorder")))
        result = h.rpc("record_stop"); self.assertFalse(result["ok"])
        status = h.status()["recording"]
        self.assertEqual(status["state"], "error")
        self.assertIn("HDR verification failed", status["message"])
        self.assertTrue(Path(status["file"]).exists())
        self.assertTrue(Path(status["file"]).name.endswith(".part.mkv"))


@unittest.skipUnless(BINARY.is_file() and os.environ.get("MURMUR_SPEECH_TEST_RAW")
                     and os.environ.get("MURMUR_VOSK_MODEL") and os.environ.get("MURMUR_WHISPER_MODEL")
                     and os.environ.get("MURMUR_GTCRN_MODEL"), "supply the public fixture and local voice models")
class ControllerRealVoiceTests(unittest.TestCase):
    def test_real_rust_voice_and_denoiser_accept_a_fixture_without_actions(self):
        from murmur.native_worker import binary_path, package_dir
        worker = binary_path()
        if worker is None or package_dir("vosk") is None:
            self.skipTest("build the Rust voice worker and install Vosk")
        models = Path(os.environ["MURMUR_VOSK_MODEL"]).parent
        # A private config/socket and fake paced pw-record use the actual native
        # worker/weights. Dry-run dispatch prevents any desktop or recorder action.
        patch_config = {"activation": {"wake_phrase": "go forward"},
                        "recognition": {"noise_suppression": True},
                        "commands": {"ten meters": "cancel"}}
        h = Harness(onboarded=True, accurate=True, config_patch=patch_config,
                    real_worker=worker, real_library=package_dir("vosk") / "libvosk.so",
                    real_model=os.environ["MURMUR_VOSK_MODEL"], pcm=os.environ["MURMUR_SPEECH_TEST_RAW"],
                    XDG_DATA_HOME=str(models.parents[1]))
        self.addCleanup(h.close)
        h.wait(lambda: h.status()["engine"] is not None, seconds=15)
        h.wait(lambda: h.status()["message"] == "Dry run: cancel", seconds=15)
        self.assertEqual(h.status()["controller"], "rust")
        self.assertEqual(h.status()["engine"], "vosk+whisper-small")
        self.assertTrue(h.status()["dry_run"])
        self.assertEqual(h.events("wf-recorder"), [])
        self.assertEqual(h.events("gpu-screen-recorder"), [])
        self.assertTrue(h.rpc("mute", muted=True)["ok"])
        self.assertFalse(h.status()["microphone_open"])
