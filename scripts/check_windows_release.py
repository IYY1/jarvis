"""Offline frozen-app acceptance; use only a private Windows/Wine test desktop."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from jarvis import __version__, config
    from jarvis.client import request
    import win32con
    import win32gui
    import win32process

    console = bundle / "JarvisConsole.exe"
    with tempfile.TemporaryDirectory(prefix="jarvis-frozen-") as temporary:
        folder = Path(temporary)
        cfg = folder / "config.json"
        config.save(config.load(cfg), cfg)
        env = dict(
            os.environ,
            JARVIS_CONFIG=str(cfg),
            JARVIS_ENDPOINT=str(folder / "run/control.json"),
            JARVIS_DRY_RUN="1",
        )
        os.environ["JARVIS_ENDPOINT"] = env["JARVIS_ENDPOINT"]

        def run(*arguments):
            result = subprocess.run(
                [str(console), *arguments],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            return result.stdout

        assert __version__ in run("--version")
        diagnostics = json.loads(run("doctor"))
        assert diagnostics["frozen"]
        assert diagnostics["voice_engine"]["model_ready"]
        assert diagnostics["voice_engine"]["accurate_model_ready"]
        assert all(item["ok"] for item in diagnostics["tools"].values())
        assert all(diagnostics["dependencies"].values())
        assert diagnostics["vosk_library"]
        print(
            "Frozen version, offline models, tools and dependency discovery passed",
            flush=True,
        )
        with (folder / "service.log").open("w+") as output:
            service = subprocess.Popen(
                [str(console), "--internal", "daemon"],
                env=env,
                stdout=output,
                stderr=output,
            )
            try:
                deadline = time.monotonic() + 15
                while (
                    not Path(env["JARVIS_ENDPOINT"]).exists()
                    and service.poll() is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                if not Path(env["JARVIS_ENDPOINT"]).exists():
                    output.seek(0)
                    raise AssertionError(output.read())
                status = request("status")["status"]
                assert status["muted"] and not status["microphone_open"]
                assert (
                    "Dry run"
                    in request("execute", phrase="turn the volume down")["message"]
                )
                assert json.loads(run("shutdown"))["ok"]
                assert service.wait(timeout=5) == 0
            finally:
                if service.poll() is None:
                    service.kill()
                    service.wait()
        print(
            "Frozen muted service, command dispatch and wait-for-exit shutdown passed",
            flush=True,
        )
        payload = {
            "config": config.load(cfg),
            "mode": "once",
            "grammar": {"phrases": []},
        }
        result = subprocess.run(
            [str(console), "--internal", "voice"],
            input=json.dumps(payload) + "\n",
            text=True,
            env=dict(env, JARVIS_OWNER_PID=str(os.getpid())),
            capture_output=True,
            timeout=15,
        )
        events = [json.loads(line) for line in result.stdout.splitlines()]
        assert any(
            event.get("event") == "error" and "consent" in event["message"]
            for event in events
        ), result.stderr
        print("Frozen voice worker refuses capture without consent", flush=True)
        ui = subprocess.Popen([str(bundle / "Jarvis.exe")], env=env)
        try:
            deadline = time.monotonic() + 15
            hwnd = 0
            while not hwnd and ui.poll() is None and time.monotonic() < deadline:
                candidate = win32gui.FindWindow("TkTopLevel", "Jarvis · Settings")
                if (
                    candidate
                    and win32process.GetWindowThreadProcessId(candidate)[1] == ui.pid
                ):
                    hwnd = candidate
                else:
                    time.sleep(0.05)
            assert hwnd, "Frozen settings window did not open"
            assert not Path(env["JARVIS_ENDPOINT"]).exists(), (
                "Opening Settings unexpectedly started the service"
            )
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            assert ui.wait(timeout=10) == 0
        finally:
            if ui.poll() is None:
                ui.kill()
                ui.wait()
        print(
            "Frozen native settings opens and closes without starting capture",
            flush=True,
        )
    print("FROZEN RELEASE CHECKS PASSED", flush=True)


if __name__ == "__main__":
    main()
