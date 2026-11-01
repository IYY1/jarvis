#!/usr/bin/env python3
"""Compare default Rust utterance handling with experimental Vosk finalization.

Sequential fresh workers, public local PCM only. The unpaced probe exercises
the real Engine's energy gate, ten-second bound, matcher and wake-to-command
path with Whisper disabled. Separate paced runs retain real Whisper/GTCRN and
prefetch, measuring worker RSS and command latency. Neither is a full acoustic
acceptance suite, live capture, Windows result or long-run soak.
"""
from __future__ import annotations

import argparse
import array
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from benchmark_whisper_on_demand import session
from murmur.commands import Parser
from murmur.config import validate
from murmur.native_worker import prepare


def engine_probe(binary, fixture, finalize, cycles, background_repeats):
    config = validate({"recognition": {"accurate": False, "noise_suppression": False},
                       "activation": {"wake_phrase": "go forward"}})
    grammar = Parser({"ten meters": "start recording", "go forward ten meters": "start recording"}).grammar(["firefox", "steam", "stremio"])
    with patch.dict(os.environ, MURMUR_VOICE_WORKER="rust"):
        _, payload, env = prepare(config, "wake_word", grammar)
    payload["native"]["vosk_finalize_utterances"] = finalize
    payload["native"]["vosk_fast_path"] = False
    with tempfile.TemporaryDirectory(prefix="jarvis-finalize-") as folder:
        reverse = Path(folder) / "reversed.raw"
        samples = array.array("h", fixture.read_bytes())
        samples.reverse()
        reverse.write_bytes(samples.tobytes())
        requests = []
        for _ in range(cycles):
            requests.extend([
                {"path": str(reverse), "session": "wake", "repeat": background_repeats, "trim_heap": True},
                {"path": str(fixture.resolve()), "session": "wake", "trim_heap": True},
                {"path": str(fixture.resolve()), "session": "command", "trim_heap": True},
            ])
        result = subprocess.run([str(binary.resolve()), "--engine"], env=env,
                                input=json.dumps(payload) + "\n" + "".join(json.dumps(r) + "\n" for r in requests),
                                text=True, capture_output=True, check=True, timeout=1200)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    if len(events) != 1 + cycles * 3 or events[0]["event"] != "ready":
        raise RuntimeError("Incomplete engine probe")
    rows = []
    expected = [[], [("wake", "go forward"), ("command", "ten meters")],
                [("command", "go forward ten meters")]]
    for index in range(cycles):
        observations = events[1 + index * 3:4 + index * 3]
        actual = [[(e["kind"], e["text"]) for e in row["events"]] for row in observations]
        if actual != expected:
            raise RuntimeError(f"Cycle {index + 1}: expected {expected}, got {actual}")
        if any(row["recognition_stats"]["vosk_finalize_utterances"] != finalize for row in observations):
            raise RuntimeError("Worker did not use the requested finalization policy")
        rows.append({"cycle": index + 1, "background": observations[0],
                     "wake_command": observations[1], "command": observations[2]})
    floors = [row["command"]["memory"]["rss_mib"] for row in rows]
    return {"cycles": rows, "ready": events[0], "commands_identical": True,
            "correct_commands": cycles * 2, "background_events": 0,
            "background_seconds_per_cycle": fixture.stat().st_size / 32000 * background_repeats,
            "first_floor_mib": floors[0], "last_floor_mib": floors[-1],
            "max_floor_mib": max(floors), "max_floor_rise_mib": max(floors) - floors[0],
            "cpu_seconds": sum(row["decode_cpu_seconds"] + row["settle_cpu_seconds"] for row in events[1:])}


def summarize(runs):
    result = {}
    for name in ("default", "finalize"):
        rows = [row for row in runs if row["name"] == name]
        paced = [row["paced"] for row in rows if "paced" in row]
        latency = [command["latency_ms"] for row in paced for command in row["commands"]]
        result[name] = {
            "workers": len(rows),
            "probe_correct_commands": sum(row["probe"]["correct_commands"] for row in rows),
            "probe_max_floor_rise_mib": [row["probe"]["max_floor_rise_mib"] for row in rows],
            "probe_final_floor_mib": [row["probe"]["last_floor_mib"] for row in rows],
            "paced_commands": len(latency),
            "median_latency_ms": statistics.median(latency) if latency else None,
            "p95_latency_ms": sorted(latency)[math.ceil(.95 * len(latency)) - 1] if latency else None,
            "paced_peak_rss_mib": [row["peak_rss_mib"] for row in paced],
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "native/voice-worker/target/release/jarvis-voice")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cycles", type=int, default=32)
    parser.add_argument("--background-repeats", type=int, default=12)
    parser.add_argument("--commands", type=int, default=3)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume a saved prefix with identical inputs and parameters")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (1 <= args.runs <= 3 and 3 <= args.cycles <= 100 and 1 <= args.commands <= 10
            and 1 <= args.background_repeats <= 20):
        parser.error("Use runs 1–3, cycles 3–100, commands 1–10 and background repetitions 1–20")
    size = args.fixture.stat().st_size
    if not 6400 <= size <= 320000 or size % 2 or size * args.background_repeats > 32000 * 60:
        parser.error("Use 0.2–10 s of s16le mono 16 kHz PCM, repeating to at most 60 s")
    parameters = {"runs": args.runs, "cycles": args.cycles, "commands": args.commands,
                  "background_repeats": args.background_repeats, "probe_only": args.probe_only}
    data = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(), "scope": __doc__,
            "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
            "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
            "parameters": parameters, "runs": [], "complete": False, "production_default_enabled": False,
            "full_recognition_acceptance": False, "probe_only": args.probe_only}
    schedule = [(repeat + 1, name) for repeat in range(args.runs) for name in ("default", "finalize")]
    if args.resume:
        previous = json.loads(args.output.read_text())
        keys = ("schema_version", "parameters", "binary_sha256", "fixture_sha256", "platform", "scope")
        if any(previous.get(key) != data[key] for key in keys):
            parser.error("Resume needs identical parameters, binary, fixture, platform and scope")
        completed = [(row["repeat"], row["name"]) for row in previous["runs"]]
        if completed != schedule[:len(completed)] or len(completed) > len(schedule):
            parser.error("Saved runs must be a completed prefix of this comparison")
        data = previous
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n")
    for repeat in range(args.runs):
        for finalize in (False, True):
            name = "finalize" if finalize else "default"
            if (repeat + 1, name) in [(row["repeat"], row["name"]) for row in data["runs"]]:
                continue
            print(f"Measuring {name}, repeat {repeat + 1}", flush=True)
            row = {"name": name, "repeat": repeat + 1,
                   "probe": engine_probe(args.binary, args.fixture, finalize, args.cycles, args.background_repeats)}
            if not args.probe_only:
                row["paced"] = session(args.binary, args.fixture, 60, 4, 4, args.commands, 2, True,
                                       finalize_utterances=finalize)
                if [c["text"] for c in row["paced"]["commands"]] != ["ten meters"] * args.commands:
                    raise RuntimeError("Paced command mismatch")
            data["runs"].append(row)
            data["complete"] = len(data["runs"]) == args.runs * 2
            data["summary"] = summarize(data["runs"])
            baseline, candidate = data["summary"]["default"], data["summary"]["finalize"]
            comparable = data["complete"] and not args.probe_only
            data["gates"] = {
                "fixture_commands_match": data["complete"],
                "median_latency_nonregression": candidate["median_latency_ms"] <= baseline["median_latency_ms"] if comparable else None,
                "p95_latency_nonregression": candidate["p95_latency_ms"] <= baseline["p95_latency_ms"] if comparable else None,
                "full_recognition_acceptance": False, "enable_by_default": False,
            }
            args.output.write_text(json.dumps(data, indent=2) + "\n")
            print(json.dumps({"name": name, "probe_floor_rise_mib": row["probe"]["max_floor_rise_mib"],
                              "paced_peak_rss_mib": row.get("paced", {}).get("peak_rss_mib")}), flush=True)


if __name__ == "__main__":
    main()
