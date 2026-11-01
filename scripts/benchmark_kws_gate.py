#!/usr/bin/env python3
"""Sequential file-only comparison against the verified, saved ungated probe.

RSS/PSS include the energy ring and native libraries. No allocator trimming,
capture, downloads, actions or production defaults are changed. Repetitions
reuse public recordings and cannot establish a population false-alarm rate.
"""
from __future__ import annotations

import argparse
import array
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import tarfile

from benchmark_kws import ROOT, cases, digest, probe, reverse, silence

GATE = {"rms": .001, "preroll_ms": 300, "hangover_ms": 1200}


def verify_baseline(folder):
    snapshot = json.loads((folder / "snapshot.json").read_text())
    binary = folder / "jarvis-kws-probe"
    archive = folder / "source.tar.gz"
    if digest(binary) != snapshot["binary_sha256"] or digest(archive) != snapshot["source_archive_sha256"]:
        raise ValueError("Saved baseline hash mismatch")
    import hashlib
    with tarfile.open(archive) as source:
        for name, expected in snapshot["files"].items():
            entries = [m for m in source.getmembers() if m.isfile() and m.name.endswith(name)]
            if len(entries) != 1 or hashlib.sha256(source.extractfile(entries[0]).read()).hexdigest() != expected:
                raise ValueError("Baseline source archive mismatch: " + name)
    return snapshot


def gate_cases(fixture, model, folder, silence_seconds):
    rows = cases(fixture, model, folder)
    pcm = fixture.read_bytes()
    def add(name, audio, expected, paced=True):
        path = folder / (name + ".raw")
        path.write_bytes(audio)
        rows.append(dict(name=name, group="surrogate", path=str(path.resolve()),
                         sha256=digest(path), seconds=len(audio) / 32000, expected=expected, paced=paced))
    soft = array.array("h", (round(s * .1) for s in array.array("h", pcm[:21120]))).tobytes()
    add("soft-onset", silence(3) + soft + pcm[21120:] + silence(1.5), ["GO_FORWARD"])
    add("long-leading-quiet", silence(12) + pcm + silence(1.5), ["GO_FORWARD"], False)
    add("active-speech", reverse(pcm) * 6, [])
    # Replace the short silence row, keeping exactly the same audio in both arms.
    rows = [r for r in rows if r["name"] != "silence"]
    add("silence", silence(silence_seconds), [])
    for row in rows:
        row["paced"] = row["name"] in {
            "natural", "pause-inside-name", "low-volume", "soft-onset", "quiet-recovery", "active-speech", "silence"}
    return rows


def compare(baseline, gated):
    rows = []
    for before, after in zip(baseline["cases"], gated["cases"], strict=True):
        if before["name"] != after["name"]:
            raise ValueError("Reordered comparison cases")
        identical = before["actual"] == after["actual"]
        rows.append({"name": before["name"], "labels_identical": identical,
                     "trigger_deltas_seconds": [b["audio_seconds"] - a["audio_seconds"]
                         for a, b in zip(before["hits"], after["hits"], strict=True)] if identical else None})
    return rows


def summarize(runs):
    result = {}
    for arm in ("baseline", "ungated-current", "gated"):
        selected = [r for r in runs if r["arm"] == arm]
        if not selected:
            continue
        all_cases = [c for r in selected for c in r["cases"]]
        paced = [c for r in selected if r["paced"] for c in r["cases"]]
        def cpu(name):
            return [100 * c["measurement"]["cpu_seconds"] / c["measurement"]["audio_seconds"]
                    for c in paced if c["name"] == name]
        result[arm] = {
            "fresh_workers": len(selected), "cases": len(all_cases),
            "passed_cases": sum(c["passed"] for c in all_cases),
            "expected_wakes": sum(len(c["expected"]) for c in all_cases),
            "correct_wakes": sum((Counter(c["expected"]) & Counter(c["actual"])).total() for c in all_cases),
            "missed_wakes": sum((Counter(c["expected"]) - Counter(c["actual"])).total() for c in all_cases),
            "extra_triggers": sum((Counter(c["actual"]) - Counter(c["expected"])).total() for c in all_cases),
            "paced_ready_rss_mib": [r["ready"]["memory"]["rss_mib"] for r in selected if r["paced"]],
            "paced_peak_rss_mib": [max(c["measurement"]["memory"]["peak_rss_mib"] for c in r["cases"])
                                    for r in selected if r["paced"]],
            "paced_final_pss_mib": [r["cases"][-1]["measurement"]["memory"]["pss_mib"] for r in selected if r["paced"]],
            "silence_cpu_percent_one_core": cpu("silence"),
            "active_speech_cpu_percent_one_core": cpu("active-speech"),
        }
    return result


def finalize(data):
    """Derive gates from recorded measurements, without running workers again."""
    data["summary"] = summarize(data["runs"])
    deltas = [d for row in data["comparisons"] for c in row["gated"]
              for d in (c["trigger_deltas_seconds"] or [])]
    silence_cpu = data["summary"]["gated"]["silence_cpu_percent_one_core"]
    data["trigger_delta_seconds_range"] = [min(deltas), max(deltas)] if deltas else None
    data["gates"] = {
        "fixture_labels_pass": all(r["all_cases_pass"] for r in data["runs"]),
        "ungated_trigger_chunks_unchanged": all(c["labels_identical"] and all(d == 0 for d in c["trigger_deltas_seconds"])
                                                for row in data["comparisons"] for c in row.get("ungated", [])),
        "standby_cpu_at_most_0.1_percent": all(v <= .1 for v in silence_cpu) if silence_cpu else None,
        "all_fixture_trigger_times_nonregressing": all(d <= 1e-9 for d in deltas) if deltas else None,
        "prior_full_threshold_sweep_pass": False,
        "enable_by_default": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", type=Path, default=ROOT / "native/kws-probe/target/release/jarvis-kws-probe")
    p.add_argument("--baseline-dir", type=Path, required=True)
    for option in ("fixture", "model-dir", "library", "keywords-dir", "work-dir", "output"):
        p.add_argument("--" + option, type=Path, required=True)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--silence-seconds", type=int, default=30)
    p.add_argument("--unpaced-only", action="store_true")
    args = p.parse_args()
    if sys.platform != "linux" or not 1 <= args.repeat <= 3 or not 10 <= args.silence_seconds <= 90:
        p.error("Linux; 1–3 repeats; 10–90 seconds of silence")
    if args.output.exists():
        p.error("Use a new output path to preserve earlier evidence")
    snapshot = verify_baseline(args.baseline_dir)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    rows = gate_cases(args.fixture, args.model_dir, args.work_dir, args.silence_seconds)
    config = {"library": str(args.library.resolve()), "tokens": str((args.model_dir / "tokens.txt").resolve()),
              "score": 1.0, "threshold": .25}
    config.update({k: str(next(args.model_dir.glob(k + "*.int8.onnx")).resolve()) for k in ("encoder", "decoder", "joiner")})
    data = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "scope": __doc__,
            "platform": platform.platform(), "baseline_snapshot": snapshot, "binary_sha256": digest(args.binary),
            "configuration": {"threshold": .25, "quiet_gate": GATE, "sequential_workers": True, "allocator_trim": False},
            "model_and_library": {k: {"path": v, "sha256": digest(v)} for k, v in config.items() if isinstance(v, str)},
            "keywords": {g: digest(args.keywords_dir / (g + "-keywords.txt")) for g in ("surrogate", "published", "names")},
            "inputs": rows, "runs": [], "comparisons": [], "complete": False,
            "prior_threshold_failures": {"artifact": "kws-2026-10-26.json", "sha256": digest(ROOT / "docs/benchmarks/kws-2026-10-26.json"),
                                         "threshold_0.5_misses": 9, "threshold_0.75_misses": 12, "full_sweep_pass": False},
            "production_defaults_changed": False, "full_acoustic_acceptance": False,
            "windows_acceptance": False, "whole_application_acceptance": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        data["summary"] = summarize(data["runs"])
        args.output.write_text(json.dumps(data, indent=2) + "\n")
    def run(arm, selected, paced, repeat, group="surrogate"):
        cfg = dict(config, keywords=str((args.keywords_dir / (group + "-keywords.txt")).resolve()))
        if arm == "gated":
            cfg["quiet_gate"] = GATE
        settings = args.work_dir / f"{group}-{arm}.json"
        settings.write_text(json.dumps(cfg))
        binary = args.baseline_dir / "jarvis-kws-probe" if arm == "baseline" else args.binary
        print(f"{arm}, {group}, paced={paced}, repeat={repeat}", flush=True)
        measured = {"arm": arm, "group": group, "paced": paced, "repeat": repeat, **probe(binary, settings, selected, paced)}
        data["runs"].append(measured)
        save()
        return measured
    for group in ("surrogate", "published", "names"):
        selected = [r for r in rows if r["group"] == group]
        baseline = run("baseline", selected, False, 1, group)
        current = run("ungated-current", selected, False, 1, group)
        gated = run("gated", selected, False, 1, group)
        data["comparisons"].append({"paced": False, "group": group, "ungated": compare(baseline, current), "gated": compare(baseline, gated)})
        save()
    if not args.unpaced_only:
        selected = [r for r in rows if r["paced"]]
        for repeat in range(1, args.repeat + 1):
            baseline = run("baseline", selected, True, repeat)
            gated = run("gated", selected, True, repeat)
            data["comparisons"].append({"paced": True, "repeat": repeat, "gated": compare(baseline, gated)})
            save()
    data["complete"] = True
    finalize(data)
    save()
    print(json.dumps({"summary": data["summary"], "gates": data["gates"]}, indent=2))
    return 0 if data["gates"]["fixture_labels_pass"] and data["gates"]["ungated_trigger_chunks_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
