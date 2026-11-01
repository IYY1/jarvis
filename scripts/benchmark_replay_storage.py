#!/usr/bin/env python3
"""Compare fresh RAM/disk segment stores using one supplied one-second video.

No capture, voice models or desktop service. Segments have distinct byte objects
in RAM, as they do after pipe delivery. This measures the Windows buffer classes
on the host OS, including the Rust helper; it is not a Windows/gameplay result.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from murmur.process_memory import snapshot
from murmur.replay_store import DiskReplayBuffer, binary
from murmur.windows.capture import ReplayBuffer


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def worker(args):
    with tempfile.TemporaryDirectory(prefix="replay-measure-", dir=args.work_dir) as folder:
        store = (DiskReplayBuffer(args.history, args.max_mb * 1024**2, str(Path(folder) / "buffer"))
                 if args.mode == "disk" else ReplayBuffer(args.history, args.max_mb * 1024**2))
        try:
            samples = [{"phase": "empty", "memory": snapshot()}]
            data = args.fixture.read_bytes()
            started = time.monotonic()
            for index in range(args.segments):
                # Force a fresh encoded block; bytes(existing_bytes) shares it.
                store.append(bytes(bytearray(data)), index)
                if index % 10 == 9:
                    samples.append({"phase": "fill", "segment": index + 1, "memory": snapshot()})
            samples.append({"phase": "retained", "memory": snapshot()})
            keys = samples[0]["memory"]["totals"]
            return {"mode": args.mode, "append_seconds": round(time.monotonic() - started, 4),
                    "segment_bytes": len(data), "written_payload_bytes": len(data) * args.segments if args.mode == "disk" else 0,
                    "retained_payload_bytes": min(args.history, args.max_mb * 1024**2 // len(data)) * len(data),
                    "samples": samples,
                    "sampled_peak_bytes": {k: max(s["memory"]["totals"][k] for s in samples)
                                           if all(s["memory"]["totals"][k] is not None for s in samples) else None for k in keys}}
        finally:
            store.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixture", type=Path, required=True, help="Generated one-second independent Matroska video")
    p.add_argument("--history", type=int, default=120)
    p.add_argument("--segments", type=int, default=180)
    p.add_argument("--max-mb", type=int, default=256)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--work-dir", type=Path, default=ROOT / "build/replay-storage")
    p.add_argument("--output", type=Path)
    p.add_argument("--mode", choices=("ram", "disk"), help=argparse.SUPPRESS)
    args = p.parse_args()
    if not 1 <= args.history <= 600 or not args.history <= args.segments <= 3600 or not 64 <= args.max_mb <= 16384:
        p.error("Use history 1–600, segments history–3600 and max-mb 64–16384")
    if not 1 <= args.repeats <= 10:
        p.error("repeats must be 1–10")
    if not 0 < args.fixture.stat().st_size <= min(64, args.max_mb) * 1024**2:
        p.error("Fixture must be nonempty and fit the segment/buffer limit")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    if args.mode:
        print(json.dumps(worker(args)))
        return
    if args.output is None:
        p.error("--output is required")
    runs = []
    for repeat in range(args.repeats):
        for mode in ("ram", "disk"):
            command = [sys.executable, str(Path(__file__).resolve()), "--fixture", str(args.fixture.resolve()),
                       "--history", str(args.history), "--segments", str(args.segments), "--max-mb", str(args.max_mb),
                       "--work-dir", str(args.work_dir.resolve()), "--mode", mode]
            result = json.loads(subprocess.check_output(command, text=True, timeout=300))
            result["repeat"] = repeat + 1
            runs.append(result)
    result = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "platform": platform.platform(), "scope": __doc__,
              "history_seconds": args.history, "segments_appended": args.segments, "max_mib": args.max_mb,
              "fixture_sha256": digest(args.fixture), "helper_sha256": digest(binary()),
              "runs": runs, "full_application_acceptance": False,
              "limitations": ["Store-only Linux-host comparison; excludes encoder, named-pipe readers, voice, controller and settings.",
                              "Accelerated fill/rollover; no sustained disk-throughput or SSD-wear measurement.",
                              "Samples every ten appends; brief peaks can be missed. File cache is outside process RSS/PSS and is reclaimable.",
                              "No native Windows hardware, memory-pressure or live GSR disk-buffer result."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps([{ "mode": r["mode"], "repeat": r["repeat"], "peak": r["sampled_peak_bytes"],
                      "retained": r["samples"][-1]["memory"]["totals"]} for r in runs], indent=2))


if __name__ == "__main__":
    main()
