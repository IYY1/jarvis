#!/usr/bin/env python3
"""Compare idle offline Waybar clients. No controller, UI or audio is started."""
import argparse
import json
import os
from pathlib import Path
import platform
import select
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def memory(pid):
    values = {}
    for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key] = int(value.split()[0]) / 1024
    return {"rss_mib": values["Rss"], "pss_mib": values["Pss"]}


def ticks(pid):
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(stat[11]) + int(stat[12])


def sample(argv, env, seconds):
    start = time.perf_counter()
    process = subprocess.Popen([*argv, "waybar"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if not select.select([process.stdout], [], [], 10)[0]:
            raise RuntimeError("Waybar client did not produce its offline event")
        event = json.loads(process.stdout.readline())
        ready_ms = (time.perf_counter() - start) * 1000
        if event["class"] != "offline":
            raise RuntimeError(f"Unexpected event: {event}")
        # Sample the persistent client after its startup allocations have settled.
        time.sleep(.1)
        result = memory(process.pid)
        before = ticks(process.pid)
        time.sleep(seconds)
        result.update(ready_ms=ready_ms, idle_seconds=seconds, idle_cpu_ticks=ticks(process.pid) - before)
        return result
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
        process.stdout.close(); process.stderr.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--idle-seconds", type=float, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.pairs < 1 or not 0 < args.idle_seconds <= 30:
        parser.error("Choose at least one pair and 0–30 idle seconds")
    commands = {"python": [sys.executable, "-m", "jarvis"],
                "rust": [str(ROOT / "native/cli/target/release/jarvis")]}
    samples = {name: [] for name in commands}
    with tempfile.TemporaryDirectory(prefix="jarvis-cli-bench-") as folder:
        env = dict(os.environ, JARVIS_SOCKET=str(Path(folder) / "absent.sock"))
        for pair in range(args.pairs):
            for name in (["python", "rust"] if pair % 2 == 0 else ["rust", "python"]):
                samples[name].append(sample(commands[name], env, args.idle_seconds))
    result = {"workload": "Persistent Waybar client, service offline, one event then bounded reconnect backoff; no controller, audio, UI or model inference",
              "system": platform.platform(), "python": platform.python_version(), "pairs": args.pairs,
              "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"), "samples": samples,
              "medians": {name: {key: statistics.median(item[key] for item in values)
                                  for key in ["rss_mib", "pss_mib", "ready_ms", "idle_cpu_ticks"]}
                          for name, values in samples.items()}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["medians"], indent=2))


if __name__ == "__main__":
    main()
