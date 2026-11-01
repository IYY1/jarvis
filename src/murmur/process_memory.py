"""On-request process-tree memory accounting, without inference or optional packages.

Linux reports RSS/PSS. Windows reports private working set and private commit.
Unavailable counters stay null, including totals, rather than understating usage.
Only explicit roots and their descendants are included; a separately launched
settings window must be supplied as another root by the benchmark caller.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys


def linux_processes(proc: Path = Path("/proc")) -> dict:
    processes = {}
    for path in proc.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            raw = (path / "stat").read_text()
            name, fields = raw.split("(", 1)[1].rsplit(") ", 1)
            fields = fields.split()
            if fields[0] == "Z":
                continue
            processes[int(path.name)] = {"ppid": int(fields[1]), "name": name,
                                         "started": fields[19]}
        except (OSError, ValueError, IndexError):
            continue  # a process may exit during enumeration
    return processes


def linux_memory(pid: int, proc: Path = Path("/proc")) -> dict:
    values = {}
    for line in (proc / str(pid) / "smaps_rollup").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("Rss", "Pss"):
            values[key.lower() + "_bytes"] = int(value.split()[0]) * 1024
    return {key: values.get(key) for key in ("rss_bytes", "pss_bytes")}


def descendants(roots, processes) -> list[int]:
    children = {}
    for pid, info in processes.items():
        children.setdefault(info["ppid"], []).append(pid)
    included, pending = set(), list(roots)
    while pending:
        pid = pending.pop()
        if pid in included:
            continue
        included.add(pid)
        pending.extend(children.get(pid, ()))
    return sorted(included)


def snapshot(roots=None) -> dict:
    roots = sorted(set(roots if roots is not None else [os.getpid()]))
    if not roots or any(type(pid) is not int or pid <= 0 for pid in roots):
        raise ValueError("Supply positive process IDs")
    windows = sys.platform == "win32"
    keys = ("private_working_set_bytes", "commit_bytes") if windows else ("rss_bytes", "pss_bytes")
    result = {"schema_version": 1, "platform": sys.platform, "roots": roots,
              "scope": "explicit roots and descendants; add separately launched settings as a root",
              "processes": [], "totals": {key: None for key in keys}, "complete": False}
    try:
        if windows:
            from .windows.process_memory import processes as inventory, memory as measure
        elif sys.platform == "linux":
            inventory, measure = linux_processes, linux_memory
        else:
            result["error"] = "Process memory accounting supports Linux and Windows"
            return result
        processes = inventory()
    except OSError as exc:
        result["error"] = str(exc)
        return result
    for pid in descendants(roots, processes):
        row = {"pid": pid, **{k: processes.get(pid, {}).get(k) for k in ("ppid", "name")},
               **dict.fromkeys(keys)}
        try:
            if pid not in processes:
                raise ProcessLookupError("Process exited or could not be inspected")
            row.update(measure(pid))
        except OSError as exc:
            row["error"] = str(exc)
        result["processes"].append(row)
    for key in keys:
        values = [row.get(key) for row in result["processes"]]
        if values and all(value is not None for value in values):
            result["totals"][key] = sum(values)
    result["complete"] = all(value is not None for value in result["totals"].values())
    return result


def service_snapshot() -> dict:
    """Read the running service's snapshot; never start it for diagnostics."""
    from .client import request
    try:
        response = request("status", timeout=2)
        if response.get("ok") and isinstance(response.get("memory"), dict):
            return response["memory"]
        return {"complete": False, "error": "Service does not provide process memory; restart the updated service"}
    except (OSError, ValueError) as exc:
        return {"complete": False, "error": str(exc)}
