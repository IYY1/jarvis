#!/usr/bin/env python3
"""Summarize scripts/benchmark_windows_app.py reports into a before/after table.

Usage: summarize_windows_benchmark.py REPORT.json... [--output summary.json]
Reports are grouped by the label prefix before the last "-" (e.g. baseline-2).
Medians across runs are reported; per-run values are kept in the JSON output.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics


METRICS = {
    "Cold start to service ready (s)": lambda r: r["marks"]["service_ready_s"],
    "Cold start to listening (s)": lambda r: r["marks"]["usable_s"],
    "Settled idle, private working set (MiB)": lambda r: r["service_tree"]["settled_idle"]["private_working_set"]["median_mib"],
    "Settled idle, private commit (MiB)": lambda r: r["service_tree"]["settled_idle"]["private_commit"]["median_mib"],
    "During commands, median private WS (MiB)": lambda r: r["service_tree"]["commands"]["private_working_set"]["median_mib"],
    "Peak private working set (MiB)": lambda r: r["service_tree_peak"]["private_working_set_mib"],
    "Peak private commit (MiB)": lambda r: r["service_tree_peak"]["private_commit_mib"],
    "First 60 s idle after commands, private WS (MiB)": lambda r: r["service_tree"]["idle_after_commands_first_60s"]["private_working_set"]["median_mib"],
    "After 5 min idle, private working set (MiB)": lambda r: r["service_tree"]["idle_end_last_30s"]["private_working_set"]["median_mib"],
    "After 5 min idle, private commit (MiB)": lambda r: r["service_tree"]["idle_end_last_30s"]["private_commit"]["median_mib"],
    "Settings window, private working set (MiB)": lambda r: r["settings_window"]["private_working_set"]["median_mib"],
    "Command latency, median (s)": lambda r: statistics.median(r["command_latency_s"]),
    "Command latency, max (s)": lambda r: max(r["command_latency_s"]),
    "Commands dispatched": lambda r: r["commands_dispatched"],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    groups: dict[str, list[dict]] = {}
    for path in args.reports:
        report = json.loads(path.read_text())
        groups.setdefault(report["label"].rsplit("-", 1)[0], []).append(report)
    names = sorted(groups)
    summary = {}
    for metric, read in METRICS.items():
        row = {}
        for name in names:
            values = [read(r) for r in groups[name]]
            row[name] = {"median": round(statistics.median(values), 3), "runs": values}
        summary[metric] = row
    header = "| Metric | " + " | ".join(f"{n} (n={len(groups[n])})" for n in names)
    if len(names) == 2:
        header += " | Change |"
    print(header + ("" if len(names) == 2 else " |"))
    print("|" + " --- |" * (len(names) + 1 + (len(names) == 2)))
    for metric, row in summary.items():
        cells = [f"{row[n]['median']:g}" for n in names]
        line = f"| {metric} | " + " | ".join(cells)
        if len(names) == 2:
            before, after = row[names[0]]["median"], row[names[1]]["median"]
            change = f"{after - before:+.2f} ({(after - before) / before * 100:+.0f}%)" if before else "n/a"
            line += f" | {change}"
        print(line + " |")
    if args.output:
        args.output.write_text(json.dumps({"groups": {n: len(groups[n]) for n in names}, "metrics": summary}, indent=2))


if __name__ == "__main__":
    main()
