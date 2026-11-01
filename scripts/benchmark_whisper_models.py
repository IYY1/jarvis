#!/usr/bin/env python3
"""Compare explicit local English Whisper profiles; never install/select a model.

All workers run sequentially. Isolated Rust transcription is checked against
sherpa-onnx with the same model and seven public-fixture perturbations. Separate
paced Rust voice workers retain Vosk/free decoding, Whisper prefetch and real
GTCRN; fast path and decoder finalization are disabled. No actual microphone,
desktop action, download, Windows result or full acoustic acceptance is involved.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from benchmark_transcriber import variants
from benchmark_whisper_on_demand import session

NAMES = ("small.en", "base.en", "tiny.en")


def files(folder, name):
    return {key: str((folder / f"{name}-{suffix}").resolve()) for key, suffix in
            (("encoder", "encoder.int8.onnx"), ("decoder", "decoder.int8.onnx"), ("tokens", "tokens.txt"))}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def reference(folder, name, fixture):
    import sherpa_onnx
    paths = files(folder, name)
    provider = "cpu:" + str(ROOT / "src/murmur/onnx-cpu.conf")
    recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
        paths["encoder"], paths["decoder"], paths["tokens"], language="en", num_threads=8, provider=provider)
    cases = []
    for label, pcm in variants(fixture.read_bytes()):
        samples = [value / 32768 for value in array.array("h", pcm)]
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, [0.0] * 4000 + samples + [0.0] * 4000)
        recognizer.decode_stream(stream)
        cases.append({"case": label, "text": stream.result.text.strip()})
    return {"sherpa_onnx": sherpa_onnx.__version__, "cases": cases}


def isolated(binary, folder, name, fixture, repeats, expected):
    with tempfile.TemporaryDirectory(prefix="jarvis-whisper-models-") as temporary:
        root = Path(temporary)
        inputs = []
        for label, pcm in variants(fixture.read_bytes()):
            path = root / f"{label}.raw"
            path.write_bytes(pcm)
            inputs.append((label, path))
        requests = "".join(json.dumps({"path": str(path)}) + "\n" for _ in range(repeats) for _, path in inputs)
        result = subprocess.run([str(binary.resolve()), "--batch", str(folder.resolve()), "--model-name", name],
                                input=requests, text=True, capture_output=True, check=True, timeout=180)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    if len(rows) != 1 + len(inputs) * repeats or rows[0].get("model") != name:
        raise RuntimeError("Incomplete/wrong-profile Rust output")
    references = {row["case"]: row["text"] for row in expected["cases"]}
    cases = [{"case": label, "repeat": repeat + 1, **row,
              "reference_text": references[label], "reference_identical": row["text"] == references[label]}
             for (repeat, label), row in zip(((repeat, label) for repeat in range(repeats) for label, _ in inputs), rows[1:], strict=True)]
    return {"ready": rows[0], "cases": cases,
            "reference_identical": all(row["reference_identical"] for row in cases),
            "median_decode_ms": statistics.median(row["seconds"] for row in cases) * 1000,
            "final_rss_mib": cases[-1]["memory"]["rss_mib"],
            "peak_rss_mib": max(row["memory"]["peak_rss_mib"] for row in rows)}


def summarize(runs):
    result = {}
    for name in NAMES:
        rows = [row for row in runs if row["name"] == name]
        if not rows:
            continue
        latency = [command["latency_ms"] for row in rows for command in row["paced"]["commands"]]
        result[name] = {"workers": len(rows), "paced_commands": len(latency),
                       "paced_commands_match": all(row["paced_commands_match"] for row in rows),
                       "reference_transcripts_identical": all(row["isolated"]["reference_identical"] for row in rows),
                       "isolated_retained_rss_mib": [row["isolated"]["final_rss_mib"] for row in rows],
                       "isolated_peak_rss_mib": [row["isolated"]["peak_rss_mib"] for row in rows],
                       "paced_worker_peak_rss_mib": [row["paced"]["peak_rss_mib"] for row in rows],
                       "paced_command_pss_mib": [round(m["pss_bytes"] / 2**20, 2) for row in rows for m in row["paced"]["memory_after_commands"] if m["pss_bytes"] is not None],
                       "median_latency_ms": statistics.median(latency),
                       "p95_latency_ms": sorted(latency)[math.ceil(.95 * len(latency)) - 1]}
    return result


def gates(summary, complete):
    baseline = summary.get("small.en")
    return {name: {
        "reference_transcripts_match": complete and row["reference_transcripts_identical"],
        "fixture_commands_match": complete and row["paced_commands_match"],
        "median_latency_nonregression": row["median_latency_ms"] <= baseline["median_latency_ms"] if complete and baseline else None,
        "p95_latency_nonregression": row["p95_latency_ms"] <= baseline["p95_latency_ms"] if complete and baseline else None,
        "whole_application_budget_acceptance": False,
        "full_recognition_acceptance": False,
        "enable_by_default": False,
    } for name, row in summary.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--small", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--tiny", type=Path, required=True)
    parser.add_argument("--voice-binary", type=Path, default=ROOT / "native/voice-worker/target/release/jarvis-voice")
    parser.add_argument("--whisper-binary", type=Path, default=ROOT / "native/whisper-recognizer/target/release/jarvis-whisper")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--commands", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-worker", choices=NAMES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    folders = {"small.en": args.small, "base.en": args.base, "tiny.en": args.tiny}
    if not 1 <= args.runs <= 3 or not 1 <= args.commands <= 10:
        parser.error("Use runs 1–3 and commands 1–10")
    if not 6400 <= args.fixture.stat().st_size <= 320000 or args.fixture.stat().st_size % 2:
        parser.error("Fixture must be 0.2–10 s of mono s16le 16 kHz PCM")
    for name, folder in folders.items():
        if not all(Path(path).is_file() for path in files(folder, name).values()):
            parser.error("Missing explicit local model files: " + name)
    if args.reference_worker:
        print(json.dumps(reference(folders[args.reference_worker], args.reference_worker, args.fixture)))
        return
    data = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(), "scope": __doc__, "fixture_sha256": digest(args.fixture),
            "voice_binary_sha256": digest(args.voice_binary), "whisper_binary_sha256": digest(args.whisper_binary),
            "model_files": {name: {key: {"sha256": digest(path), "bytes": Path(path).stat().st_size}
                                   for key, path in files(folder, name).items()} for name, folder in folders.items()},
            "references": {}, "runs": [], "complete": False, "production_default": "small.en",
            "full_recognition_acceptance": False, "enable_smaller_model_by_default": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n")
    for name in NAMES:
        print("Reference: " + name, flush=True)
        # A separate reference process releases all native allocations before
        # Rust resource measurement; the two inference engines never overlap.
        result = subprocess.run([sys.executable, __file__, *sys.argv[1:], "--reference-worker", name],
                                text=True, capture_output=True, check=True, timeout=180)
        data["references"][name] = json.loads(result.stdout)
        args.output.write_text(json.dumps(data, indent=2) + "\n")
    for repeat in range(args.runs):
        for name in NAMES:
            print(f"Measuring {name}, repeat {repeat + 1}", flush=True)
            row = {"name": name, "repeat": repeat + 1,
                   "isolated": isolated(args.whisper_binary, folders[name], name, args.fixture, 3, data["references"][name]),
                   "paced": session(args.voice_binary, args.fixture, 60, 4, 4, args.commands, 2, True,
                                    whisper_files=files(folders[name], name))}
            row["paced_commands_match"] = [c["text"] for c in row["paced"]["commands"]] == ["ten meters"] * args.commands
            data["runs"].append(row)
            data["complete"] = len(data["runs"]) == args.runs * 3
            data["summary"] = summarize(data["runs"])
            data["gates"] = gates(data["summary"], data["complete"])
            args.output.write_text(json.dumps(data, indent=2) + "\n")
            print(json.dumps({"name": name, "reference_identical": row["isolated"]["reference_identical"],
                              "paced_commands_match": row["paced_commands_match"],
                              "worker_peak_rss_mib": row["paced"]["peak_rss_mib"]}), flush=True)
    if not all(row["paced_commands_match"] and row["isolated"]["reference_identical"] for row in data["runs"]):
        raise SystemExit("Transcript or command mismatch: inspect raw results; smaller models remain experimental")


if __name__ == "__main__":
    main()
