#!/usr/bin/env python3
"""Sample explicit Jarvis process trees without starting audio or replay.

Start the desired scenario yourself, then supply its controller PID (or let
the local status API supply it). Add --extra-pid for a separately launched
settings window. Peak means the largest simultaneous total, not the sum of
each process's independent high-water mark. Units in the artifact are bytes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from murmur.process_memory import snapshot

BUDGET_MIB = {"waiting-replay-off": 150, "waiting-replay-on": 300, "command": 400}


def summarize(samples):
    keys = samples[0]["memory"]["totals"]
    result = {}
    for key in keys:
        values = [sample["memory"]["totals"][key] for sample in samples]
        result[key] = ({"peak": max(values), "median": statistics.median(values)}
                       if all(value is not None for value in values) else None)
    return result


def measure(roots, seconds, interval, sample=snapshot):
    started = time.monotonic()
    samples = []
    while True:
        samples.append({"elapsed_seconds": round(time.monotonic() - started, 3),
                        "memory": sample(roots)})
        remaining = seconds - (time.monotonic() - started)
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, help="Controller/service PID; otherwise query local status")
    parser.add_argument("--extra-pid", type=int, action="append", default=[])
    parser.add_argument("--scenario", choices=BUDGET_MIB, required=True)
    parser.add_argument("--settings", choices=["closed", "included"], required=True,
                        help="Declare settings-window coverage; include its root when launched separately")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--interval", type=float, default=.25)
    parser.add_argument("--note", default="", help="Describe workload and capture helper; never include private audio")
    parser.add_argument("--check-budget", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or not .1 <= args.seconds <= 86400:
        parser.error("seconds must be finite and between 0.1 and 86400")
    if not math.isfinite(args.interval) or not .05 <= args.interval <= 60:
        parser.error("interval must be finite and between 0.05 and 60")
    if args.pid is None:
        from murmur.client import request
        response = request("status", timeout=2)
        if not response.get("ok") or type(response.get("pid")) is not int:
            parser.error("Service does not supply its PID; update/restart it or pass --pid")
        args.pid = response["pid"]
    roots = sorted(set([args.pid, *args.extra_pid]))
    if any(pid <= 0 for pid in roots):
        parser.error("PIDs must be positive")
    samples = measure(roots, args.seconds, args.interval)
    summary = summarize(samples)
    metric = "private_working_set_bytes" if sys.platform == "win32" else "rss_bytes"
    complete = all(s["memory"]["complete"] for s in samples)
    within = complete and summary.get(metric) is not None and summary[metric]["peak"] <= BUDGET_MIB[args.scenario] * 1048576
    result = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
              "platform": platform.platform(), "python": platform.python_version(),
              "scenario": args.scenario, "settings": args.settings, "note": args.note,
              "roots": roots, "requested_seconds": args.seconds, "interval_seconds": args.interval,
              "scope": "explicit roots and descendants; caller must include every Jarvis process",
              "peak_is_sampled": True, "complete": complete, "summary_bytes": summary,
              "budget": {"metric": metric, "limit_mib": BUDGET_MIB[args.scenario],
                         "within_target": within, "enforced": args.check_budget}, "samples": samples}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ("scenario", "complete", "summary_bytes", "budget")}, indent=2))
    return 0 if complete and (within or not args.check_budget) else 1


if __name__ == "__main__":
    raise SystemExit(main())
