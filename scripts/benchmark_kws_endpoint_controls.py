#!/usr/bin/env python3
"""Focused diagnosis of the recorded quiet-endpoint low-volume command miss.

Reuses explicit, already-generated cycle fixtures/configurations; new evidence
only. A file endpoint isolates command-decoder capability, and RMS 0.001 is a
separate experimental endpoint candidate. No production defaults change.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from benchmark_kws import ROOT, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "native/kws-probe/target/release/jarvis-kws-handoff-probe")
    parser.add_argument("--cycle-work-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--cycle-result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new evidence path")
    before = json.loads(args.cycle_result.read_text())
    if not before["complete"] or before["gates"]["all_fixture_controls_pass"]:
        parser.error("This diagnostic requires the recorded failed endpoint matrix")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(), "scope": __doc__,
              "binary_sha256": digest(args.binary), "prior_matrix_sha256": digest(args.cycle_result),
              "prior_low_volume_command_pass": False, "runs": [], "complete": False,
              "production_defaults_changed": False, "full_acoustic_acceptance": False,
              "windows_acceptance": False, "whole_application_acceptance": False}
    tests = [("low-volume-command", "file", ["ten meters"], False)]
    tests += [(name, "rms-0.001", expected, timeout) for name, expected, timeout in [
        ("low-volume-command", ["ten meters"], False), ("natural", ["ten meters"], False),
        ("pause-inside-command", ["ten meters"], False), ("wake-without-command", [], True),
        ("command-without-wake", [], False), ("long-command-timeout", [], True)]]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    save()
    for name, arm, expected, timeout in tests:
        source = args.cycle_work_dir / ("tiny.en-gated-" + name)
        config = json.loads((source / "config.json").read_text())
        fixture = source / "input.raw"
        folder = args.work_dir / (name + "-" + arm)
        folder.mkdir(exist_ok=True)
        config.update(cycles=1, work_dir=str(folder.resolve()))
        config["quiet_endpoint"] = None if arm == "file" else dict(config["quiet_endpoint"], rms=.001)
        config["detector_binary"] = str(Path(config["detector_binary"]).resolve())
        settings = folder / "config.json"
        settings.write_text(json.dumps(config))
        print(f"endpoint diagnostic: {name}, {arm}", flush=True)
        run = subprocess.run([str(args.binary.resolve()), str(settings.resolve()), str(fixture.resolve())],
                             capture_output=True, text=True, timeout=120, check=True)
        events = [json.loads(line) for line in run.stdout.splitlines()]
        row = events[-1]
        if row["event"] != "complete":
            raise RuntimeError("Incomplete focused probe")
        actual = [e["text"] for e in row["command_result"].get("events", []) if e["kind"] == "command"] if row["command_result"] else []
        identical = None
        if row["buffer_path"]:
            buffered = Path(row["buffer_path"])
            start = round(row["buffer_start_seconds"] * 32000)
            end = round(row["endpoint_input_seconds"] * 32000)
            identical = buffered.read_bytes() == fixture.read_bytes()[start:end]
            if not identical:
                raise RuntimeError("Buffered audio changed")
        measured = {"name": name, "arm": arm, "expected": expected, "actual": actual,
                    "passed": actual == expected and row["timed_out"] == timeout, "buffer_identical": identical,
                    "input_sha256": digest(fixture), "configuration": config, "events": events}
        result["runs"].append(measured)
        save()
        print(json.dumps({"name": name, "arm": arm, "actual": actual, "passed": measured["passed"]}), flush=True)
    result["complete"] = True
    result["focused_cases_pass"] = all(r["passed"] for r in result["runs"])
    result["enable_by_default"] = False
    save()
    return 0 if result["focused_cases_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
