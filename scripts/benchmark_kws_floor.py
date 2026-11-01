#!/usr/bin/env python3
"""Check the waiting-floor trend beyond initial persistent-KWS warm-up.

Explicit existing experimental configuration and public PCM only; bounded
cycles with fresh command workers and a persistent file-replay detector.
This short run does not establish leak freedom or replace the 24-hour soak.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from benchmark_kws import ROOT, digest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", type=Path, default=ROOT / "native/kws-probe/target/release/jarvis-kws-handoff-probe")
    for name in ("config", "fixture", "work-dir", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--cycles", type=int, default=10)
    args = p.parse_args()
    if not 4 <= args.cycles <= 32 or args.output.exists():
        p.error("Use 4–32 cycles and a new result path")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    config = json.loads(args.config.read_text())
    config.update(cycles=args.cycles, work_dir=str(args.work_dir.resolve()))
    settings = args.work_dir / "config.json"
    settings.write_text(json.dumps(config))
    measured = subprocess.run([str(args.binary.resolve()), str(settings.resolve()), str(args.fixture.resolve())],
                              capture_output=True, text=True, timeout=30+args.cycles*35, check=True)
    events = [json.loads(line) for line in measured.stdout.splitlines()]
    if events[-1]["event"] != "complete" or len(events[-1]["cycles"]) != args.cycles:
        raise RuntimeError("Incomplete cycle data")
    rows = events[-1]["cycles"]
    passed = True
    for row in rows:
        actual = [e["text"] for e in row["command_result"].get("events", []) if e["kind"] == "command"] if row["command_result"] else []
        row["actual_commands"] = actual
        start = round(row["buffer_start_seconds"] * 32000)
        end = round(row["endpoint_input_seconds"] * 32000)
        row["buffer_identical"] = Path(row["buffer_path"]).read_bytes() == args.fixture.read_bytes()[start:end]
        passed = passed and actual == ["ten meters"] and row["buffer_identical"] and row["waiting_after_release"]["processes"] == 2
    rss = [r["waiting_after_release"]["rss_mib"] for r in rows]
    pss = [r["waiting_after_release"]["pss_mib"] for r in rows]
    data = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(), "scope": __doc__,
            "binary_sha256": digest(args.binary), "detector_sha256": digest(config["detector_binary"]),
            "voice_sha256": digest(config["voice_binary"]), "fixture_sha256": digest(args.fixture),
            "configuration": config, "events": events, "complete": True,
            "summary": {"cycles": len(rows), "all_commands_and_buffers_match": passed,
                "waiting_rss_mib": rss, "waiting_pss_mib": pss,
                "initial_waiting_rss_mib": events[0]["waiting"]["rss_mib"],
                "first_to_last_waiting_rss_growth_mib": rss[-1]-rss[0],
                "last_five_waiting_rss_growth_mib": rss[-1]-rss[max(0,len(rss)-5)],
                "peak_combined_rss_mib": max(r["memory"]["peak_combined_rss_mib"] for r in rows),
                "peak_combined_pss_mib": max(r["memory"]["peak_combined_pss_mib"] for r in rows)},
            "whole_application_acceptance": False, "full_acoustic_acceptance": False,
            "windows_acceptance": False, "full_duration_soak_acceptance": False, "enable_by_default": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2)+"\n")
    print(json.dumps(data["summary"], indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
