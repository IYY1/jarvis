#!/usr/bin/env python3
"""Measure persistent-detector cycles with a matched optional quiet endpoint.

Public file replay only; sequential fresh parent/detector sets and a fresh
existing command worker on each trigger. Includes waiting floors, timeout,
overflow, soft commands and audio retention. No actions or real capture.
"""
from __future__ import annotations

import argparse
import array
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import statistics
import subprocess

from benchmark_kws import ROOT, digest, silence
from benchmark_kws_gate import GATE
from benchmark_kws_handoff import payload

ENDPOINT = {"rms": .005, "min_speech_ms": 240, "quiet_ms": 600, "timeout_ms": 5000}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", type=Path, default=ROOT / "native/kws-probe/target/release/jarvis-kws-handoff-probe")
    p.add_argument("--detector-binary", type=Path, default=ROOT / "native/kws-probe/target/release/jarvis-kws-probe")
    p.add_argument("--voice-binary", type=Path, default=ROOT / "native/voice-worker/target/release/jarvis-voice")
    for name in ("fixture", "model-dir", "library", "keywords-dir", "tiny", "work-dir", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--cycles", type=int, default=3)
    args = p.parse_args()
    if not 2 <= args.cycles <= 10 or args.output.exists():
        p.error("Use 2–10 cycles and a new output path")
    if digest(args.fixture) != "f15c60ec54059d8b66e410d0064945a0b0a04ea56e1ddca1958e493c0cf70e71":
        p.error("Requires the pinned public recording")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    pcm = args.fixture.read_bytes()
    natural = silence(.6) + pcm + silence(1.5)
    config = {"library": str(args.library.resolve()), "tokens": str((args.model_dir / "tokens.txt").resolve()),
              "keywords": str((args.keywords_dir / "surrogate-keywords.txt").resolve()), "score": 1., "threshold": .25}
    config.update({k: str(next(args.model_dir.glob(k + "*.int8.onnx")).resolve()) for k in ("encoder", "decoder", "joiner")})
    data = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "scope": __doc__,
            "platform": platform.platform(), "binary_sha256": digest(args.binary),
            "detector_sha256": digest(args.detector_binary), "voice_sha256": digest(args.voice_binary),
            "fixture_sha256": digest(args.fixture), "configuration": {"endpoint": ENDPOINT, "cycles": args.cycles,
                "quiet_gate": GATE, "settle_ms": 1000, "preroll_ms": 2000, "max_buffer_ms": 10000,
                "allocator_trim_requested": False, "file_cache_evicted": False, "sequential": True},
            "model_and_library": {k: {"path": v, "sha256": digest(v)} for k, v in config.items() if isinstance(v, str)},
            "runs": [], "complete": False, "production_defaults_changed": False,
            "whole_application_acceptance": False, "full_acoustic_acceptance": False,
            "windows_acceptance": False, "full_duration_soak_acceptance": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(data, indent=2) + "\n")
    def run(profile, arm, name, audio, expected, cycles=1, timeout=False, error=False, **overrides):
        print(f"cycles {profile}, {arm}, {name}, count={cycles}", flush=True)
        folder = args.work_dir / f"{profile}-{arm}-{name}"
        folder.mkdir(exist_ok=True)
        fixture, voice, detector, settings = (folder / n for n in ("input.raw", "voice.json", "detector.json", "config.json"))
        fixture.write_bytes(audio)
        voice.write_text(json.dumps(payload(profile, args.tiny)))
        detector_cfg = dict(config)
        if arm == "gated":
            detector_cfg["quiet_gate"] = GATE
        detector.write_text(json.dumps(detector_cfg))
        cfg = {"detector_binary": str(args.detector_binary.resolve()), "detector_config": str(detector.resolve()),
               "voice_binary": str(args.voice_binary.resolve()), "voice_payload": str(voice.resolve()),
               "work_dir": str(folder.resolve()), "cycles": cycles, "settle_ms": 1000,
               "quiet_endpoint": ENDPOINT, **overrides}
        settings.write_text(json.dumps(cfg))
        process = subprocess.run([str(args.binary.resolve()), str(settings.resolve()), str(fixture.resolve())],
                                 text=True, capture_output=True, timeout=180)
        events = [json.loads(line) for line in process.stdout.splitlines()]
        if error:
            passed = process.returncode != 0 and "Command audio buffer overflow" in process.stderr and "panicked" not in process.stderr
            data["runs"].append({"profile": profile, "arm": arm, "name": name, "expected_error": "buffer overflow",
                                 "exit_code": process.returncode, "stderr": process.stderr, "events": events, "passed": passed})
            save()
            return
        if process.returncode != 0:
            raise RuntimeError(process.stderr)
        if not events or events[-1]["event"] != "complete":
            raise RuntimeError("Incomplete cycle protocol")
        rows = events[-1].get("cycles", [events[-1]])
        if len(rows) != cycles:
            raise RuntimeError("Missing cycle results")
        for row in rows:
            row["actual_commands"] = [e["text"] for e in row["command_result"].get("events", []) if e["kind"] == "command"] if row["command_result"] else []
            row["expected_commands"] = expected
            row["passed"] = row["actual_commands"] == expected and row["timed_out"] == timeout
            if row["buffer_path"]:
                buffered = Path(row["buffer_path"])
                start = round(row["buffer_start_seconds"] * 32000)
                end = round(row["endpoint_input_seconds"] * 32000)
                row["buffer_identical"] = buffered.read_bytes() == audio[start:end]
                row["buffer_sha256"] = digest(buffered)
                if not row["buffer_identical"]:
                    raise RuntimeError("Missing or changed buffered input")
            row["result_after_last_speech_ms"] = 1000 * (row["result_elapsed_seconds"] - row["last_speech_input_seconds"]) if row["last_speech_input_seconds"] and row["command_result"] else None
            if row["waiting_after_release"]["processes"] != 2:
                raise RuntimeError("Command process survived release")
        report = {"profile": profile, "arm": arm, "name": name, "input_sha256": digest(fixture),
                  "settings": cfg, "cycles": rows, "passed": all(r["passed"] for r in rows),
                  "floor_growth_first_to_last_mib": rows[-1]["waiting_after_release"]["rss_mib"] - rows[0]["waiting_after_release"]["rss_mib"],
                  "initial_waiting": events[0]["waiting"], "final_waiting": rows[-1]["waiting_after_release"], "events": events}
        data["runs"].append(report)
        save()
        print(json.dumps({"name": name, "passed": report["passed"], "commands": [r["actual_commands"] for r in rows],
                          "final_rss_mib": report["final_waiting"]["rss_mib"]}), flush=True)
    save()
    for profile in ("vosk", "small.en", "tiny.en"):
        for arm in ("ungated", "gated"):
            run(profile, arm, "natural", natural, ["ten meters"], cycles=args.cycles)
    run("tiny.en", "gated", "file-endpoint-compatibility", natural, ["ten meters"], quiet_endpoint=None)
    run("tiny.en", "gated", "late-loader", natural, ["ten meters"], payload_delay_ms=3000)
    run("tiny.en", "gated", "pause-inside-command", silence(.6)+pcm[:55040]+silence(.3)+pcm[55040:]+silence(1.5), ["ten meters"])
    run("tiny.en", "gated", "command-without-wake", silence(.6)+pcm[38400:]+silence(1.5), [])
    run("tiny.en", "gated", "wake-without-command", silence(.6)+pcm[:38400]+silence(5.5), [], timeout=True)
    strong = pcm[17*960:38*960]
    run("tiny.en", "gated", "long-command-timeout", silence(.6)+pcm+strong*10+silence(1.5), [], timeout=True)
    low = array.array("h", (round(s*.1) for s in array.array("h", pcm))).tobytes()
    # Deliberately retain this quality gate even if a wake passes but the new
    # endpoint cannot collect enough quiet command speech.
    run("tiny.en", "gated", "low-volume-command", silence(.6)+low+silence(5.5), ["ten meters"])
    run("tiny.en", "gated", "buffer-overflow", natural, [], error=True, max_buffer_ms=2500)
    data["summary"] = {}
    for profile in ("vosk", "small.en", "tiny.en"):
        data["summary"][profile] = {}
        for arm in ("ungated", "gated"):
            report = next(r for r in data["runs"] if r["name"] == "natural" and r["profile"] == profile and r["arm"] == arm)
            rows = report["cycles"]
            data["summary"][profile][arm] = {
                "commands": sum(len(r["actual_commands"]) for r in rows), "cycles": len(rows), "passed": report["passed"],
                "peak_rss_mib": max(r["memory"]["peak_combined_rss_mib"] for r in rows),
                "peak_pss_mib": max(r["memory"]["peak_combined_pss_mib"] for r in rows),
                "hwm_sum_upper_bound_mib": max(r["memory"]["sum_process_hwm_upper_bound_mib"] for r in rows),
                "waiting_rss_mib": [r["waiting_after_release"]["rss_mib"] for r in rows],
                "waiting_pss_mib": [r["waiting_after_release"]["pss_mib"] for r in rows],
                "floor_growth_first_to_last_mib": report["floor_growth_first_to_last_mib"],
                "result_after_last_speech_ms": [r["result_after_last_speech_ms"] for r in rows],
                "median_result_after_last_speech_ms": statistics.median(r["result_after_last_speech_ms"] for r in rows),
            }
    data["complete"] = True
    data["gates"] = {"natural_cycle_commands_match": all(r["passed"] for r in data["runs"] if r["name"] == "natural"),
                     "all_fixture_controls_pass": all(r["passed"] for r in data["runs"]),
                     "returned_to_detector_only_processes": all(c["waiting_after_release"]["processes"] == 2 for r in data["runs"] for c in r.get("cycles", [])),
                     "buffered_slices_byte_identical": all(c.get("buffer_identical", True) for r in data["runs"] for c in r.get("cycles", [])),
                     "enable_by_default": False}
    save()
    print(json.dumps({"summary": data["summary"], "gates": data["gates"]}, indent=2))
    return 0 if data["gates"]["all_fixture_controls_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
