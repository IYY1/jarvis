#!/usr/bin/env python3
"""Attribute Vosk history to one decoder at a time with the Rust offline probe.

All three production decoders stay resident. One consumes repeated fixture PCM
without real-time pacing or an energy gate, with a ten-second utterance bound.
Whisper, capture and actions never run. Finalization is an experiment, not a
recognition-quality result or a change to the normal worker reset policy.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from murmur.commands import Parser
from murmur.config import validate
from murmur.native_worker import prepare


def measure(binary, fixture, decoder, policy, cycles, seconds, reverse):
    config = validate({"recognition": {"accurate": False}, "activation": {"wake_phrase": "go forward"}})
    grammar = Parser({"ten meters": "start recording", "go forward ten meters": "start recording"}).grammar(["firefox", "steam", "stremio"])
    with patch.dict(os.environ, MURMUR_VOICE_WORKER="rust"):
        _, payload, env = prepare(config, "once", grammar)
    request = {"path": str(fixture.resolve()), "decoder": decoder, "policy": policy, "cycles": cycles,
               "seconds_per_cycle": seconds, "reverse": reverse}
    result = subprocess.run([str(binary.resolve()), "--decoder-history"],
                            input=json.dumps(payload) + "\n" + json.dumps(request) + "\n",
                            text=True, capture_output=True, env=env, timeout=1800, check=True)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    if len(events) != cycles + 1 or events[0]["event"] != "ready" or any(e["event"] != "cycle" for e in events[1:]):
        raise RuntimeError("Decoder history probe returned incomplete results")
    rows = events[1:]
    return {"decoder": decoder, "policy": policy, "reverse_fixture": reverse, "ready": events[0], "cycles": rows,
            "first_floor_mib": rows[0]["memory"]["rss_mib"], "last_floor_mib": rows[-1]["memory"]["rss_mib"],
            "peak_rss_mib": max(r["memory"]["peak_rss_mib"] for r in rows),
            "max_floor_mib": max(r["memory"]["rss_mib"] for r in rows),
            "cpu_seconds": rows[-1]["cpu_seconds"], "elapsed_seconds": rows[-1]["elapsed_seconds"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", type=Path, default=ROOT / "native/voice-worker/target/release/jarvis-voice")
    p.add_argument("--fixture", type=Path, required=True)
    p.add_argument("--cycles", type=int, default=32)
    p.add_argument("--seconds", type=float, default=42)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--decoder", choices=("wake", "command", "free"), action="append")
    p.add_argument("--policy", choices=("reset", "finalize"), action="append")
    p.add_argument("--reverse", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if not 3 <= args.cycles <= 100 or not 1 <= args.seconds <= 60 or not 1 <= args.repeat <= 3:
        p.error("Use cycles 3–100, seconds 1–60, repeat 1–3")
    if not 6400 <= args.fixture.stat().st_size <= 320000 or args.fixture.stat().st_size % 2:
        p.error("Fixture must be 0.2–10 seconds of s16le mono 16 kHz PCM")
    with args.fixture.open("rb") as stream:
        fixture_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    with args.binary.open("rb") as stream:
        binary_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    expected_runs = args.repeat * len(args.decoder or ("wake", "command", "free")) * len(args.policy or ("reset", "finalize"))
    result = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "platform": platform.platform(), "scope": __doc__, "fixture_sha256": fixture_hash,
              "binary_sha256": binary_hash, "seconds_per_cycle": args.seconds, "runs": [],
              "cycles_per_run": args.cycles, "expected_runs": expected_runs, "complete": False,
              "production_policy_changed": False, "full_recognition_acceptance": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repeat in range(args.repeat):
        for decoder in args.decoder or ("wake", "command", "free"):
            for policy in args.policy or ("reset", "finalize"):
                print(f"Measuring {decoder} / {policy}, repeat {repeat + 1}", flush=True)
                run = measure(args.binary, args.fixture, decoder, policy, args.cycles, args.seconds, args.reverse)
                run["repeat"] = repeat + 1
                result["runs"].append(run)
                result["complete"] = len(result["runs"]) == expected_runs
                args.output.write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps({k: v for k, v in run.items() if k not in ("ready", "cycles")}), flush=True)


if __name__ == "__main__":
    main()
