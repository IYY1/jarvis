"""Native controls tested against isolated sockets and temporary user directories."""
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import tempfile
import threading
import unittest

from jarvis.cli import waybar_line
from jarvis import models
from tests.test_rust_controller import Harness, BINARY as CONTROLLER

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "native/cli/target/release/jarvis"


@unittest.skipUnless(BINARY.is_file(), "build the optional Rust CLI")
class NativeCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jarvis-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = dict(os.environ, HOME=str(self.root), XDG_DATA_HOME=str(self.root / "data"),
                        XDG_CONFIG_HOME=str(self.root / "config"), JARVIS_CONFIG=str(self.root / "config.json"),
                        JARVIS_SOCKET=str(self.root / "control.sock"), PATH="/nonexistent")

    def cli(self, *args, env=None, check=False):
        return subprocess.run([str(BINARY), *args], env=env or self.env, capture_output=True,
                              text=True, timeout=10, check=check)

    def line(self, process, timeout=5):
        self.assertTrue(select.select([process.stdout], [], [], timeout)[0], "No Waybar output")
        return json.loads(process.stdout.readline())

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        for pipe in (process.stdout, process.stderr):
            if pipe:
                pipe.close()

    def test_native_version_and_readonly_doctor_need_no_python(self):
        self.assertIn("(Rust)", self.cli("--version", check=True).stdout)
        report = json.loads(self.cli("doctor", check=True).stdout)
        self.assertEqual(report["runtime"], "Rust")
        self.assertIsNone(report["interpreter"])
        self.assertFalse(report["settings_ui"]["imports_checked"])
        self.assertFalse(any(t["ok"] for t in report["tools"].values()))
        self.assertFalse((self.root / "config.json").exists())
        self.assertFalse((self.root / "data").exists())

    def test_invalid_commands_do_not_connect(self):
        for args in [("record", "save"), ("replay", "invalid"), ("execute",), ("status", "extra"),
                     ("alias", "--unknown"), ("install-model", "--archive"), ("install-user", "--enable")]:
            with self.subTest(args=args):
                response = self.cli(*args)
                self.assertEqual(response.returncode, 2)
                self.assertNotIn("No such file", response.stderr)
        self.assertEqual(self.cli("status").returncode, 1)

    def rpc_once(self, args, response):
        received = []
        server = socket.socket(socket.AF_UNIX)
        server.bind(self.env["JARVIS_SOCKET"]); server.listen(1); server.settimeout(5)
        def serve():
            try:
                with server.accept()[0] as client:
                    client.settimeout(5)
                    received.append(json.loads(client.makefile("rb").readline()))
                    client.sendall(response)
            finally:
                server.close()
        thread = threading.Thread(target=serve); thread.start()
        try:
            result = self.cli(*args)
        finally:
            thread.join(timeout=6)
            Path(self.env["JARVIS_SOCKET"]).unlink(missing_ok=True)
        self.assertFalse(thread.is_alive())
        return result, received[0]

    def test_control_protocol_and_service_errors(self):
        cases = [(('status',), {"op": "status"}), (('config',), {"op": "config"}),
                 (('listen',), {"op": "listen"}), (('cancel',), {"op": "cancel"}),
                 (('mute',), {"op": "mute", "muted": True}), (('resume',), {"op": "mute", "muted": False}),
                 (('execute', 'open browser'), {"op": "execute", "phrase": "open browser"})]
        cases += [((kind, action), {"op": kind + "_" + action})
                  for kind, actions in [("record", ["start", "stop"]), ("replay", ["start", "stop", "save"])]
                  for action in actions]
        for args, expected in cases:
            with self.subTest(args=args):
                result, payload = self.rpc_once(args, b'{"ok":true,"message":"done"}\n')
                self.assertEqual(payload, expected)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"ok": True, "message": "done"})
        result, _ = self.rpc_once(("status",), b'{"ok":false,"error":"paused"}\n')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"], "paused")

    def test_malformed_and_oversized_responses(self):
        for response in [b'not json\n', b'[]\n', b'', b'x' * (1048576 + 1)]:
            with self.subTest(length=len(response)):
                result, _ = self.rpc_once(("status",), response)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Jarvis:", result.stderr)

    def test_waybar_offline_backoff_and_signal(self):
        process = subprocess.Popen([str(BINARY), "waybar"], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, process)
        self.assertEqual(self.line(process), json.loads(waybar_line(None)))
        self.assertFalse(select.select([process.stdout], [], [], 2.3)[0], "Offline state emitted twice")
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=2), 0)

    def test_waybar_closed_output_exits_cleanly(self):
        process = subprocess.Popen([str(BINARY), "waybar"], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, process)
        process.stdout.close()
        self.assertEqual(process.wait(timeout=3), 0)

    def test_waybar_reconnects_and_matches_reference(self):
        server = socket.socket(socket.AF_UNIX); server.bind(self.env["JARVIS_SOCKET"]); server.listen(2); server.settimeout(6)
        self.addCleanup(server.close)
        process = subprocess.Popen([str(BINARY), "waybar"], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, process)
        for event in [{"status": {"state": "listening", "message": "Ready"}},
                      {"status": {"state": "paused", "message": "Recording", "recording": {"state": "recording"}, "replay": {"state": "on"}}}]:
            with server.accept()[0] as connection:
                self.assertEqual(json.loads(connection.makefile("rb").readline()), {"op": "watch"})
                connection.sendall((json.dumps(event) + "\n").encode())
                self.assertEqual(self.line(process), json.loads(waybar_line(event)))
            self.assertEqual(self.line(process), json.loads(waybar_line(None)))

    def test_installer_dry_run_and_conflict_prevalidation(self):
        preview = self.cli("install-user", "--service", "--dry-run", check=True)
        self.assertIn("jarvis-controller", preview.stdout)
        self.assertNotIn(" -m jarvis daemon", preview.stdout)
        self.assertFalse((self.root / "data").exists())
        self.assertFalse((self.root / "config").exists())
        unit = self.root / "config/systemd/user/jarvis.service"
        unit.parent.mkdir(parents=True); unit.write_text("custom service\n")
        self.assertEqual(self.cli("install-user", "--service").returncode, 1)
        self.assertFalse((self.root / "data").exists())
        self.assertEqual(unit.read_text(), "custom service\n")

    def test_installer_launcher_and_reload_only(self):
        spy = self.root / "bin"; spy.mkdir()
        log = self.root / "systemctl.log"
        tool = spy / "systemctl"
        tool.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$JARVIS_TEST_RELOAD_LOG"\n'); tool.chmod(0o755)
        env = dict(self.env, PATH=str(spy), JARVIS_TEST_RELOAD_LOG=str(log))
        result = self.cli("install-user", "--service", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text().splitlines(), ["--user daemon-reload"])
        desktop = self.root / "data/applications/org.jarvis.Controller.desktop"
        self.assertIn(f'Exec="{BINARY}" ui', desktop.read_text())
        self.assertEqual(desktop.stat().st_mode & 0o777, 0o644)
        self.assertIn("KillMode=mixed", (self.root / "config/systemd/user/jarvis.service").read_text())

    def test_model_wrong_checksum_cleans_staging(self):
        archive = self.root / "model.zip"; archive.write_bytes(b"incorrect archive")
        response = self.cli("install-model", "--basic", "--archive", str(archive))
        self.assertEqual(response.returncode, 1)
        self.assertIn("checksum mismatch", response.stderr)
        root = self.root / "data/jarvis/models"
        self.assertEqual(list(root.iterdir()), [])

    def test_models_reused_and_incomplete_destination_preserved(self):
        model = self.root / "data/jarvis/models" / models.MODEL_NAME
        model.mkdir(parents=True)
        (model / "user-note").write_text("preserve")
        response = self.cli("install-model", "--basic")
        self.assertEqual(response.returncode, 1)
        self.assertIn("Incomplete", response.stderr)
        self.assertEqual((model / "user-note").read_text(), "preserve")
        for name in models.REQUIRED:
            path = model / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("stub")
        self.assertEqual(self.cli("install-model", "--basic", "--archive", "/nonexistent").returncode, 0)

    @unittest.skipUnless(CONTROLLER.is_file(), "build Rust controller")
    def test_alias_native_roundtrip_against_isolated_controller(self):
        harness = Harness(); self.addCleanup(harness.close)
        env = dict(harness.env, PATH="/nonexistent")
        apps = harness.root / "data/applications"; apps.mkdir(parents=True)
        (apps / "stremio.desktop").write_text('[Desktop Entry]\nType=Application\nName=Stremio\nExec=/bin/true\n')
        result = self.cli("alias", " StreamIO ", "stremio", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("jarvis, open streamio", result.stdout)
        self.assertEqual(harness.rpc("config")["config"]["aliases"]["streamio"], "stremio.desktop")
        self.assertIn("streamio  ->  stremio.desktop", self.cli("alias", env=env).stdout)
        self.assertEqual(self.cli("alias", "streamio", "--remove", env=env).returncode, 0)
        self.assertNotIn("streamio", harness.rpc("config")["config"]["aliases"])
        self.assertEqual(harness.status()["state"], "paused")

    def test_checkout_wrapper_uses_native_client(self):
        if not (ROOT / "src/jarvis/bin/jarvis").is_file():
            self.skipTest("run scripts/build_cli.py")
        env = dict(self.env, PATH="/usr/bin:/bin", JARVIS_CLI="rust")
        result = subprocess.run([str(ROOT / "run-jarvis"), "--version"], env=env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(Rust)", result.stdout)


class ModelManifestTests(unittest.TestCase):
    def test_pinned_native_manifest_matches_existing_models(self):
        source = (ROOT / "native/cli/src/models.rs").read_text()
        for expected in [models.MODEL_NAME, models.MODEL_SHA256, models.ACCURATE_MODELS[models.DEFAULT_ACCURATE_MODEL]["archive"],
                         models.ACCURATE_MODELS[models.DEFAULT_ACCURATE_MODEL]["sha256"], models.DENOISER["name"], models.DENOISER["sha256"]]:
            self.assertIn(expected, source)
