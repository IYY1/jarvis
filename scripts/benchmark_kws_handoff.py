#!/usr/bin/env python3
"""Measure a separate Rust buffered handoff, using explicit public audio only.

The Rust parent, KWS child, unchanged voice --engine child, bounded audio ring,
retained PCM and native model state count together. Endpointing is an explicit
file boundary, not a live command endpoint. No capture or actions are started.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
from unittest.mock import patch

from benchmark_kws import ROOT, digest, silence
from benchmark_kws_gate import GATE
from murmur.commands import Parser
from murmur.config import validate
from murmur.native_worker import prepare


def payload(profile, tiny):
    config = validate({"recognition": {"accurate": profile != "vosk", "noise_suppression": False, "closest_match": True},
                       "activation": {"wake_phrase": "go forward"}})
    grammar = Parser({"ten meters": "start recording"}).grammar(["firefox", "steam", "stremio"])
    with patch.dict(os.environ, MURMUR_VOICE_WORKER="rust"):
        _, data, _ = prepare(config, "wake_word", grammar)
    # The entire command worker is on demand. Load its configured recognizer
    # immediately inside that child, including the real cold-loading peak.
    data["native"]["whisper_idle_seconds"] = None
    if profile == "tiny.en":
        data["native"]["whisper"] = {k: str((tiny / ("tiny.en-" + suffix)).resolve()) for k, suffix in
                                       (("encoder", "encoder.int8.onnx"), ("decoder", "decoder.int8.onnx"), ("tokens", "tokens.txt"))}
    if profile != "vosk" and data["native"]["whisper"] is None:
        raise ValueError("Supply the already-installed default Whisper model")
    return data


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", type=Path, default=ROOT / "native/kws-probe/target/release/jarvis-kws-handoff-probe")
    p.add_argument("--detector-binary", type=Path, default=ROOT / "native/kws-probe/target/release/jarvis-kws-probe")
    p.add_argument("--voice-binary", type=Path, default=ROOT / "native/voice-worker/target/release/jarvis-voice")
    for option in ("fixture", "model-dir", "library", "keywords-dir", "work-dir", "output", "tiny"):
        p.add_argument("--" + option, type=Path, required=True)
    p.add_argument("--repeat", type=int, default=3)
    args = p.parse_args()
    if not 1 <= args.repeat <= 3 or args.output.exists():
        p.error("Use 1–3 repeats and a new output path")
    if digest(args.fixture) != "f15c60ec54059d8b66e410d0064945a0b0a04ea56e1ddca1958e493c0cf70e71":
        p.error("Labelled controls require the pinned public goforward.raw")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    pcm = args.fixture.read_bytes()
    fixtures = {
        "natural": (silence(.6) + pcm + silence(1.5), ["ten meters"]),
        "late-loader": (silence(.6) + pcm + silence(1.5), ["ten meters"]),
        "command-without-wake": (silence(.6) + pcm[38400:] + silence(1.5), []),
        "wake-without-command": (silence(.6) + pcm[:38400] + silence(1.5), []),
    }
    detector_config = {"library": str(args.library.resolve()), "tokens": str((args.model_dir / "tokens.txt").resolve()),
                       "keywords": str((args.keywords_dir / "surrogate-keywords.txt").resolve()), "quiet_gate": GATE,
                       "score": 1.0, "threshold": .25}
    detector_config.update({k: str(next(args.model_dir.glob(k + "*.int8.onnx")).resolve()) for k in ("encoder", "decoder", "joiner")})
    settings = args.work_dir / "detector.json"
    settings.write_text(json.dumps(detector_config))
    profiles = {profile: payload(profile, args.tiny) for profile in ("vosk", "small.en", "tiny.en")}
    data = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "scope": __doc__,
            "platform": platform.platform(), "binary_sha256": digest(args.binary), "detector_sha256": digest(args.detector_binary),
            "voice_sha256": digest(args.voice_binary), "fixture_sha256": digest(args.fixture),
            "configuration": {"quiet_gate": GATE, "threshold": .25, "preroll_ms": 2000, "max_buffer_ms": 10000,
                              "sampler_interval_ms": 20, "allocator_trim_requested": False,
                              "existing_worker_allocator_policy_unchanged": True, "sequential_fresh_workers": True,
                              "surrogate_command_cut_input_seconds": 1.8},
            "detector_files": {k: {"path": v, "sha256": digest(v)} for k, v in detector_config.items() if isinstance(v, str)},
            "command_files": {profile: {k: {"path": v, "sha256": digest(v)} for k, v in (cfg["native"]["whisper"] or {}).items()}
                              for profile, cfg in profiles.items()},
            "payloads": profiles, "runs": [], "complete": False,
            "full_recognition_acceptance": False, "whole_application_acceptance": False, "windows_acceptance": False,
            "production_defaults_changed": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(data, indent=2) + "\n")
    save()
    for repeat in range(1, args.repeat + 1):
        for profile, voice_payload in profiles.items():
            # Primary natural fixture gets three independent cold worker sets.
            # Additional controls are focused one-pass checks, not new speakers.
            selected = fixtures.items() if repeat == 1 else [("natural", fixtures["natural"])]
            for name, (audio, expected) in selected:
                print(f"handoff {profile}, {name}, repeat={repeat}", flush=True)
                folder = args.work_dir / f"{profile}-{name}-{repeat}"
                folder.mkdir(exist_ok=True)
                path, voice_path, config_path = (folder / n for n in ("input.raw", "voice.json", "config.json"))
                path.write_bytes(audio)
                voice_path.write_text(json.dumps(voice_payload))
                config = {"detector_binary": str(args.detector_binary.resolve()), "detector_config": str(settings.resolve()),
                          "voice_binary": str(args.voice_binary.resolve()), "voice_payload": str(voice_path.resolve()),
                          "work_dir": str(folder.resolve()), "preroll_ms": 2000, "max_buffer_ms": 10000,
                          "payload_delay_ms": 3000 if name == "late-loader" else 0}
                config_path.write_text(json.dumps(config))
                process = subprocess.run([str(args.binary.resolve()), str(config_path.resolve()), str(path.resolve())],
                                         text=True, capture_output=True, timeout=150, check=True)
                events = [json.loads(line) for line in process.stdout.splitlines()]
                if events[0]["event"] != "ready" or events[-1]["event"] != "complete":
                    raise RuntimeError("Incomplete handoff protocol")
                result = events[-1]
                actual = [e["text"] for e in result["command_result"].get("events", []) if e["kind"] == "command"] if result["command_result"] else []
                triggered = result["trigger"] is not None
                expected_trigger = name != "command-without-wake"
                buffer_identical = None
                if result["buffer_path"]:
                    saved = Path(result["buffer_path"])
                    start = round(result["buffer_start_seconds"] * 32000)
                    buffer_identical = saved.read_bytes() == audio[start:]
                    result["buffer_sha256"] = digest(saved)
                    if not buffer_identical:
                        raise RuntimeError("Buffered audio changed or dropped input")
                row = {"profile": profile, "name": name, "repeat": repeat, "input_sha256": digest(path),
                       "expected_commands": expected, "actual_commands": actual,
                       "passed": actual == expected and triggered == expected_trigger,
                       "buffer_identical": buffer_identical, "events": events,
                       "latency_from_fixture_speech_end_ms": 1000 * (result["result_elapsed_seconds"] - (.6 + len(pcm) / 32000))
                            if name in ("natural", "late-loader") else None}
                data["runs"].append(row)
                save()
                print(json.dumps({"profile": profile, "name": name, "passed": row["passed"],
                                  "peak_rss_mib": result["memory"]["peak_combined_rss_mib"]}), flush=True)
    data["complete"] = True
    data["summary"] = {}
    for profile in profiles:
        rows = [r for r in data["runs"] if r["profile"] == profile]
        natural = [r for r in rows if r["name"] == "natural"]
        data["summary"][profile] = {
            "cases": len(rows), "passed_cases": sum(r["passed"] for r in rows),
            "natural_peak_combined_rss_mib": [r["events"][-1]["memory"]["peak_combined_rss_mib"] for r in natural],
            "natural_peak_combined_pss_mib": [r["events"][-1]["memory"]["peak_combined_pss_mib"] for r in natural],
            "natural_hwm_sum_upper_bound_mib": [r["events"][-1]["memory"]["sum_process_hwm_upper_bound_mib"] for r in natural],
            "natural_latency_from_fixture_speech_end_ms": [r["latency_from_fixture_speech_end_ms"] for r in natural],
            "natural_command_load_seconds": [r["events"][-1]["command_ready"]["event"]["load_seconds"] for r in natural],
            "observed_under_400_mib": all(r["events"][-1]["memory"]["sum_process_hwm_upper_bound_mib"] <= 400 for r in natural),
            "full_application_budget_pass": False,
        }
    data["gates"] = {"available_fixture_cases_pass": all(r["passed"] for r in data["runs"]),
                     "buffered_audio_byte_identical": all(r["buffer_identical"] is not False for r in data["runs"]),
                     "enable_by_default": False}
    save()
    print(json.dumps({"summary": data["summary"], "gates": data["gates"]}, indent=2))
    return 0 if data["gates"]["available_fixture_cases_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
