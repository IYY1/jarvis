#!/usr/bin/env python3
"""Three fresh 24-hour worker runs with post-warm-up RSS-floor slope gates.

Uses the load-cycle benchmark with paced supplied audio, no devices or actions.
The room-noise workaround remains explicit: this tests memory, not wake safety.
Writes each completed run before starting the next; failures are retained too.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from benchmark_load_cycles import session

MAX_FLOOR_GAP_SECONDS = 300


def slope(points):
    center = sum(t for t, _ in points) / len(points)
    mean = sum(rss for _, rss in points) / len(points)
    variance = sum((t - center) ** 2 for t, _ in points)
    if not variance:
        raise ValueError("RSS-floor timestamps must span time")
    return sum((t - center) * (rss - mean) for t, rss in points) / variance * 3600


def analyze(run, warmup_seconds=3600, windows=3, tolerance=1, minimum_seconds=86400):
    finite = lambda value: type(value) in (int, float) and math.isfinite(value)
    if (type(windows) is not int or not 2 <= windows <= 10
            or not all(finite(v) for v in (warmup_seconds, tolerance, minimum_seconds))
            or not 0 <= warmup_seconds < minimum_seconds or tolerance < 0):
        return {"status": "invalid", "reason": "Invalid analysis limits"}
    rows = run.get("commands", [])
    if (not run.get("commands_identical") or not rows
            or any(row.get("text") != "ten meters" for row in rows)):
        return {"status": "invalid", "reason": "Missing or mismatched fixture commands"}
    elapsed = run.get("elapsed_seconds", 0)
    if not finite(elapsed) or elapsed < 0:
        return {"status": "invalid", "reason": "Invalid run duration"}
    if elapsed < minimum_seconds:
        return {"status": "insufficient", "reason": "Run shorter than the requested duration"}
    if any(not finite(row.get("wake_elapsed_seconds"))
           or not finite(row.get("rss_at_wake_mib"))
           or not 0 <= row["wake_elapsed_seconds"] <= elapsed
           or row["rss_at_wake_mib"] < 0 for row in rows):
        return {"status": "invalid", "reason": "Missing, invalid or non-finite memory or timestamps"}
    if any(b["wake_elapsed_seconds"] <= a["wake_elapsed_seconds"] for a, b in zip(rows, rows[1:])):
        return {"status": "invalid", "reason": "Wake timestamps must strictly increase"}
    points = [(row["wake_elapsed_seconds"], row["rss_at_wake_mib"]) for row in rows
              if row.get("wake_elapsed_seconds", -1) >= warmup_seconds]
    if len(points) < windows * 3 or points[-1][0] <= warmup_seconds:
        return {"status": "insufficient", "reason": "Need at least three post-warm-up wake floors per window"}
    # Fit the declared run, not only the period with available observations.
    # Require floors throughout it; a short cluster cannot stand in for a day.
    times = [warmup_seconds, *(t for t, _ in points), elapsed]
    maximum_gap = max(b - a for a, b in zip(times, times[1:]))
    if maximum_gap > MAX_FLOOR_GAP_SECONDS:
        return {"status": "insufficient", "reason": "More than five minutes without a post-warm-up floor",
                "largest_floor_gap_seconds": maximum_gap}
    width = (elapsed - warmup_seconds) / windows
    fits = []
    for index in range(windows):
        lower, upper = warmup_seconds + index * width, warmup_seconds + (index + 1) * width
        window = [(t, rss) for t, rss in points if lower <= t and (t < upper or index == windows - 1)]
        if len(window) < 3:
            return {"status": "insufficient", "reason": "Fewer than three floors in a time window"}
        fits.append({"start_seconds": lower, "end_seconds": upper, "samples": len(window),
                     "slope_mib_per_hour": slope(window)})
    exceeds = [fit["slope_mib_per_hour"] > tolerance for fit in fits]
    status = "growth" if all(exceeds) else "stable" if not any(exceeds) else "inconclusive"
    return {"status": status, "warmup_seconds": warmup_seconds,
            "tolerance_mib_per_hour": tolerance, "maximum_floor_gap_seconds": MAX_FLOOR_GAP_SECONDS,
            "largest_floor_gap_seconds": maximum_gap, "windows": fits}


def verdict(analyses):
    if len(analyses) < 3:
        return "insufficient"
    states = {run["status"] for run in analyses}
    return "pass" if states == {"stable"} else "fail" if states == {"growth"} else "inconclusive"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup-hours", type=float, default=1)
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--tolerance", type=float, default=1, help="MiB/hour per window")
    parser.add_argument("--idle", type=float, default=20)
    parser.add_argument("--speech", type=float, default=35)
    parser.add_argument("--room-noise", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    values = (args.hours, args.warmup_hours, args.tolerance, args.idle, args.speech)
    if any(not math.isfinite(value) for value in values):
        parser.error("All durations and tolerance must be finite")
    if not 1 <= args.hours <= 168 or not 0 <= args.warmup_hours < args.hours:
        parser.error("Use 1–168 hours and a shorter nonnegative warm-up")
    if not 3 <= args.runs <= 10 or not 2 <= args.windows <= 10 or args.tolerance < 0:
        parser.error("Use 3–10 runs, 2–10 windows and a nonnegative tolerance")
    if not 1 <= args.idle <= 86400 or args.speech <= args.idle + 5:
        parser.error("Speech must outlast the valid idle limit by more than five seconds")
    pcm = args.fixture.read_bytes()
    if not args.binary.is_file() or not 6400 <= len(pcm) <= 320000 or len(pcm) % 2:
        parser.error("Supply a worker binary and 0.2–10 seconds of s16le 16 kHz mono PCM")
    cycles = math.ceil(args.hours * 3600 / (len(pcm) / 32000 + args.speech + 5)) + 1
    result = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
              "platform": platform.platform(), "fixture_sha256": hashlib.sha256(pcm).hexdigest(),
              "requested_hours_per_run": args.hours, "required_runs": args.runs,
              "scope": "voice-worker RSS floors only; excludes controller, UI and capture; not a recognition acceptance test",
              "runs": [], "verdict": "incomplete", "full_duration_worker_soak_pass": False}
    for index in range(args.runs):
        try:
            run = session(args.binary, args.fixture, args.idle, cycles, args.speech, args.room_noise)
            analysis = analyze(run, args.warmup_hours * 3600, args.windows, args.tolerance, args.hours * 3600)
            result["runs"].append({"index": index, "analysis": analysis, "measurement": run})
        except (OSError, RuntimeError, ValueError) as exc:
            result["runs"].append({"index": index, "analysis": {"status": "invalid", "reason": str(exc)}})
        result["verdict"] = (verdict([run["analysis"] for run in result["runs"]])
                             if len(result["runs"]) == args.runs else "incomplete")
        result["full_duration_worker_soak_pass"] = args.hours >= 24 and result["verdict"] == "pass"
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"run": index, "analysis": result["runs"][-1]["analysis"], "verdict": result["verdict"]}), flush=True)
        if result["runs"][-1]["analysis"]["status"] == "invalid":
            break
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
