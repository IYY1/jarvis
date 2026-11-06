"""Run explicitly on Windows/Wine with a private test display and local tools."""

import os
import sys
import tempfile
import subprocess
import time
import json
import unittest
from pathlib import Path


def main():
    ROOT = Path(__file__).resolve().parents[1]
    import argparse

    parser = argparse.ArgumentParser(
        description="Offline Windows contracts: no live microphone or desktop capture"
    )
    parser.add_argument("--tools", type=Path, required=True)
    options = parser.parse_args()
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
    from jarvis import config
    from jarvis.windows import system
    from jarvis.windows.pipes import SegmentReader
    from jarvis.windows.capture import ReplayBuffer
    from jarvis.windows.displays import OutputDesc, OutputDesc1, AdapterDesc1
    import ctypes

    print(
        "ctypes sizes",
        *[ctypes.sizeof(c) for c in (OutputDesc, OutputDesc1, AdapterDesc1)],
        flush=True,
    )
    assert [ctypes.sizeof(c) for c in (OutputDesc, OutputDesc1, AdapterDesc1)] == [
        96,
        152,
        312,
    ]
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_windows")
    assert unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()
    with tempfile.TemporaryDirectory(prefix="jarvis-contracts-") as temporary:
        folder = Path(temporary)
        system.private_directory(folder)
        lock = system.ServiceLock(folder / "lock")
        try:
            try:
                system.ServiceLock(folder / "lock")
                raise AssertionError("double lock accepted")
            except RuntimeError:
                pass
        finally:
            lock.close()
        import win32security

        acl = win32security.GetFileSecurity(
            str(folder), win32security.DACL_SECURITY_INFORMATION
        ).GetSecurityDescriptorDacl()
        assert all(
            win32security.ConvertSidToStringSid(acl.GetAce(i)[2])
            in (win32security.ConvertSidToStringSid(system.user_sid()), "S-1-5-18")
            for i in range(acl.GetAceCount())
        )
        print("private ACL and single-service lock passed", flush=True)
        prefix = r"\\.\pipe\Jarvis-test-" + str(os.getpid()) + "-"
        history = ReplayBuffer(10)
        errors = []
        reader = SegmentReader(prefix, history.append, errors.append)
        try:
            proc = subprocess.run(
                [
                    str(options.tools / "ffmpeg.exe"),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=30",
                    "-t",
                    "4",
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-threads",
                    "2",
                    "-g",
                    "30",
                    "-bf",
                    "0",
                    "-force_key_frames",
                    "expr:gte(t,n_forced*1)",
                    "-f",
                    "segment",
                    "-segment_time",
                    "1",
                    "-reset_timestamps",
                    "1",
                    "-segment_wrap",
                    "3",
                    "-segment_format",
                    "matroska",
                    prefix + "%03d",
                ],
                capture_output=True,
                timeout=20,
            )
            assert proc.returncode == 0, (errors, proc.stderr.decode(errors="replace"))
            deadline = time.monotonic() + 3
            while history.next < 4 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not errors, errors
            assert history.next == 4, history.next
            print(
                "private RAM replay pipes",
                history.next,
                "segments",
                history.size,
                "bytes",
                flush=True,
            )
        finally:
            reader.close()
        endpoint = folder / "run/control.json"
        cfg = folder / "config.json"
        config.save(config.load(cfg), cfg)
        env = dict(
            os.environ,
            JARVIS_ENDPOINT=str(endpoint),
            JARVIS_CONFIG=str(cfg),
            JARVIS_DRY_RUN="1",
            PYTHONPATH=str(ROOT / "src"),
        )
        with (folder / "daemon.log").open("w+") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "jarvis", "--internal", "daemon"],
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 10
                while (
                    not endpoint.exists()
                    and time.monotonic() < deadline
                    and proc.poll() is None
                ):
                    time.sleep(0.05)
                if not endpoint.exists():
                    log.seek(0)
                    raise AssertionError(log.read())
                os.environ["JARVIS_ENDPOINT"] = str(endpoint)
                from jarvis.client import request

                status = request("status")["status"]
                assert not status["microphone_open"]
                assert status["muted"]
                assert request("execute", phrase="open firefox")["ok"]
                import socket

                port = json.loads(endpoint.read_text())["port"]
                with socket.create_connection(("127.0.0.1", port), 2) as client:
                    client.sendall(b'{"op":"status","auth":"wrong"}\n')
                    assert not json.loads(client.recv(65536))["ok"]
                assert request("shutdown")["ok"]
                assert proc.wait(timeout=8) == 0
                assert not endpoint.exists()
                print(
                    "daemon authenticated IPC, dry-run dispatch, muted startup, tray and clean shutdown passed",
                    flush=True,
                )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
    print("ALL WINDOWS CONTRACTS PASSED", flush=True)


if __name__ == "__main__":
    main()
