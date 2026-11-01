#!/usr/bin/env python3
"""Pair the default Rust voice path with its opt-in Vosk fast-path experiment.

Real Vosk/Whisper/GTCRN, supplied public fixture and fake capture only. Includes
an exact-agreement arm and a forced fallback arm with the free decoder disabled.
The latter checks retained Whisper behavior and the cost of losing prefetch;
it is not an accent/noise corpus. All workers run sequentially.
"""
from __future__ import annotations
import argparse
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from benchmark_whisper_on_demand import session
from murmur.commands import Parser
from murmur.config import validate
from murmur.native_worker import prepare


def counters(binary, fixture, fast, free_decoder, commands):
    config = validate({"recognition": {"accurate": True, "noise_suppression": True, "closest_match": free_decoder},
                       "activation": {"wake_phrase": "go forward"}})
    grammar = Parser({"ten meters": "start recording"}).grammar(["firefox", "steam", "stremio"])
    with patch.dict(os.environ, MURMUR_VOICE_WORKER="rust"):
        _, payload, env = prepare(config, "wake_word", grammar)
    if payload["native"]["whisper"] is None:
        raise RuntimeError("Install the local Whisper model before benchmarking")
    payload["native"]["vosk_fast_path"] = fast
    payload["native"]["whisper_idle_seconds"] = 60
    requests = [json.dumps({"path": str(fixture.resolve()), "session": "wake"}) for _ in range(commands)]
    result = subprocess.run([str(binary.resolve()), "--engine"], env=env,
                            input=json.dumps(payload) + "\n" + "\n".join(requests) + "\n",
                            text=True, capture_output=True, check=True, timeout=120)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    if len(events) != commands + 1:
        raise RuntimeError("Incomplete model-load counter result")
    actual = [e["text"] for row in events[1:] for e in row["events"] if e["kind"] == "command"]
    if actual != ["ten meters"] * commands:
        raise RuntimeError(f"Counter fixture mismatch: {actual}")
    stats = events[-1]["recognition_stats"]
    expected = {"vosk_fast_path": fast, "vosk_finalize_utterances": False,
                "fast_commands": commands if fast and free_decoder else 0,
                "whisper_transcriptions": 0 if fast and free_decoder else commands,
                "whisper_loads": 0 if fast and free_decoder else 1}
    if stats != expected:
        raise RuntimeError(f"Unexpected model-load behavior: {stats}; expected {expected}")
    return {"final_stats": events[-1]["recognition_stats"], "results": events}


def summarize(runs):
    result = {}
    for name in ("default_exact", "fast_exact", "default_fallback", "fast_fallback"):
        rows = [r for r in runs if r["name"] == name and "paced" in r]
        if not rows:
            continue
        latency = [c["latency_ms"] for r in rows for c in r["paced"]["commands"]]
        pss = [m["pss_bytes"] / 1024**2 for r in rows for m in r["paced"]["memory_after_commands"] if m["pss_bytes"] is not None]
        result[name] = {"workers": len(rows), "commands": len(latency),
                        "median_latency_ms": statistics.median(latency),
                        "p95_latency_ms": sorted(latency)[math.ceil(.95 * len(latency)) - 1],
                        "peak_rss_mib": [r["paced"]["peak_rss_mib"] for r in rows],
                        "command_pss_mib_range": [round(min(pss), 2), round(max(pss), 2)] if pss else None,
                        "cold_latencies_ms": [r["paced"]["commands"][0]["latency_ms"] for r in rows],
                        "whisper_loads": [r["counters"]["final_stats"]["whisper_loads"] for r in rows]}
    return result


def gates(summary, complete):
    def latency_gate(kind):
        baseline, fast = summary.get("default_" + kind), summary.get("fast_" + kind)
        return fast["p95_latency_ms"] <= baseline["p95_latency_ms"] if complete and baseline and fast else None
    return {"paired_fixture_commands_match": complete,
            "exact_latency_nonregression": latency_gate("exact"),
            "fallback_latency_nonregression": latency_gate("fallback"),
            "full_recognition_acceptance": False, "enable_by_default": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", type=Path, default=ROOT / "native/voice-worker/target/release/jarvis-voice")
    p.add_argument("--fixture", type=Path, required=True)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--commands", type=int, default=3)
    p.add_argument("--counters-only", action="store_true")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if not 1 <= args.runs <= 5 or not 1 <= args.commands <= 10:
        p.error("Use runs 1–5 and commands 1–10")
    if not 6400 <= args.fixture.stat().st_size <= 320000 or args.fixture.stat().st_size % 2:
        p.error("Fixture must be 0.2–10 seconds of s16le mono 16 kHz PCM")
    with args.fixture.open("rb") as stream:
        fixture_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    with args.binary.open("rb") as stream:
        binary_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    result = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "platform": platform.platform(), "scope": __doc__, "fixture_sha256": fixture_hash,
              "binary_sha256": binary_hash, "runs": [], "production_default_enabled": False,
              "full_recognition_acceptance": False, "counters_only": args.counters_only}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repeat in range(args.runs):
        for free in (True, False):
            for fast in (False, True):
                name = ("fast" if fast else "default") + ("_exact" if free else "_fallback")
                print(f"Measuring {name}, repeat {repeat + 1}", flush=True)
                count = counters(args.binary, args.fixture, fast, free, args.commands)
                run = {"name": name, "repeat": repeat + 1, "counters": count}
                if not args.counters_only:
                    paced = session(args.binary, args.fixture, 60, 4, 4, args.commands, 2, True,
                                    fast_path=fast, free_decoder=free)
                    if [c["text"] for c in paced["commands"]] != ["ten meters"] * args.commands:
                        raise RuntimeError("Paced fixture commands differ")
                    latencies = [c["latency_ms"] for c in paced["commands"]]
                    paced["median_latency_ms"] = statistics.median(latencies)
                    paced["p95_latency_ms"] = sorted(latencies)[math.ceil(len(latencies) * .95) - 1]
                    run["paced"] = paced
                result["runs"].append(run)
                result["complete"] = len(result["runs"]) == args.runs * 4
                result["summary"] = summarize(result["runs"])
                result["gates"] = gates(result["summary"], result["complete"])
                args.output.write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps({"name": name, "stats": count["final_stats"],
                                  "peak_rss_mib": run.get("paced", {}).get("peak_rss_mib")}), flush=True)


if __name__ == "__main__":
    main()
