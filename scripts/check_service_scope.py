#!/usr/bin/env python3
"""Explicit transient user-service lifecycle test, with no UI/audio/capture.

Starts a temporary sleeping fixture in its own scope through the same launch
helper as Jarvis. Stops only the temporary parent service and verifies the
launched fixture survived; then terminates that fixture and cleans the unit.
No persistent service files or enablement are written.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    unit = "jarvis-scope-check-" + uuid.uuid4().hex[:12]
    child_pid = None
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="jarvis-scope-") as temp:
        base = Path(temp)
        child = base / "child.py"
        marker = base / "child.pid"
        child.write_text("import os,time\nfrom pathlib import Path\nPath(" + repr(str(marker)) + ").write_text(str(os.getpid()))\ntime.sleep(60)\n")
        parent = base / "parent.py"
        parent.write_text("import subprocess,sys\nfrom jarvis.desktop_process import desktop_argv\nsubprocess.run(desktop_argv([sys.executable," + repr(str(child)) + "]))\n")
        try:
            subprocess.run(["systemd-run", "--user", "--quiet", "--collect", "--expand-environment=no",
                            "--unit=" + unit, "--property=Type=exec", "--property=KillMode=mixed",
                            "--setenv=JARVIS_MANAGED_SERVICE=1", "--setenv=PYTHONPATH=" + str(root / "src"),
                            "--", sys.executable, str(parent)], check=True, timeout=5)
            until = time.monotonic() + 8
            while not marker.exists():
                if time.monotonic() > until:
                    raise RuntimeError("Scoped child did not start; inspect the transient unit journal")
                time.sleep(.05)
            child_pid = int(marker.read_text())
            before = Path(f"/proc/{child_pid}/cgroup").read_text()
            subprocess.run(["systemctl", "--user", "stop", unit], check=True, timeout=8)
            os.kill(child_pid, 0)
            result = {"ok": True, "launched_fixture_survives_parent_service_stop": True,
                      "separate_scope": ".scope" in before and unit not in before,
                      "persistent_service_files_written": False, "microphone_or_screen_access": False}
            if not result["separate_scope"]:
                raise RuntimeError("Child did not migrate to an independent scope")
        finally:
            if marker.exists() and child_pid is None:
                child_pid = int(marker.read_text())
            if child_pid:
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            subprocess.run(["systemctl", "--user", "stop", unit], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=8)
    print(json.dumps(result, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
