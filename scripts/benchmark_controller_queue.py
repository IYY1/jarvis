#!/usr/bin/env python3
"""Measure registry memory while 16 fixture actions wait in the Rust controller.

Uses private test services, synthetic desktop entries and a fake volume tool.
No microphone, display, installed service or real application is controlled.
Pass --binary to compare an older controller. RSS/PSS are controller-only;
fixture processes and the Python harness are excluded.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.benchmark_controller import memory
from tests.test_rust_controller import Harness


def sample(binary, applications):
    harness = Harness(dry_run=False, controller_argv=[str(binary)])
    try:
        folder = harness.root / "data/applications"
        folder.mkdir(parents=True)
        for n in range(applications):
            (folder / f"fixture-{n}.desktop").write_text(
                f"[Desktop Entry]\nType=Application\nName=Fixture {n}\nExec=/fixture/app-{n}\n")
        assert harness.rpc("apps")["ok"]
        barrier = harness.root / "blocked"
        marker = harness.root / "entered"
        barrier.touch()
        tool = harness.bin / "wpctl"
        tool.write_text("#!/usr/bin/env python3\nimport time\nfrom pathlib import Path\n"
                        f"Path({str(marker)!r}).touch()\n"
                        f"while Path({str(barrier)!r}).exists(): time.sleep(.01)\n")
        idle = memory(harness.process.pid)
        with ThreadPoolExecutor(max_workers=16) as pool:
            pending = [pool.submit(harness.rpc, "execute", phrase="volume up") for _ in range(16)]
            try:
                harness.wait(marker.exists)
                # Allow all socket requests to enter the bounded action queue.
                time.sleep(.2)
                assert not any(future.done() for future in pending)
                assert harness.rpc("status")["ok"]
                queued = memory(harness.process.pid)
            finally:
                barrier.unlink(missing_ok=True)
            assert all(future.result()["ok"] for future in pending)
        return {"idle": idle, "queued": queued,
                "queue_pss_delta_mib": queued["pss_mib"] - idle["pss_mib"]}
    finally:
        harness.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "src/jarvis/bin/jarvis-controller")
    parser.add_argument("--applications", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.binary.is_file() or not 1 <= args.applications <= 4096 or not 1 <= args.repeats <= 10:
        parser.error("Supply a built controller, 1–4096 applications and 1–10 repeats")
    samples = [sample(args.binary.resolve(), args.applications) for _ in range(args.repeats)]
    result = {"scope": __doc__, "platform": platform.platform(), "binary": str(args.binary.resolve()),
              "applications": args.applications, "queued_actions": 16, "samples": samples,
              "median_idle_pss_mib": statistics.median(s["idle"]["pss_mib"] for s in samples),
              "median_queued_pss_mib": statistics.median(s["queued"]["pss_mib"] for s in samples),
              "median_queue_pss_delta_mib": statistics.median(s["queue_pss_delta_mib"] for s in samples)}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
