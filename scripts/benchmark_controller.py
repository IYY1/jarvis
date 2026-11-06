#!/usr/bin/env python3
"""Compare fresh Python/Rust controllers with voice/replay off and private IPC.

No audio, recording, desktop actions or changes to the installed service. App
discovery reads the normal desktop entries. Report controller-only RSS/PSS,
idle CPU seconds and local status-request latency; model memory is excluded.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jarvis.config import DEFAULTS


def request(path, op):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(3)
        connection.connect(str(path))
        connection.sendall((json.dumps({"op": op}) + "\n").encode())
        with connection.makefile("rb") as response:
            result = json.loads(response.readline())
        if not result.get("ok"):
            raise RuntimeError(result)
        return result


def cpu_seconds(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def memory(pid):
    result = {}
    for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
        if line.startswith(("Rss:", "Pss:", "Private_Clean:", "Private_Dirty:")):
            result[line.split(":", 1)[0].lower() + "_mib"] = int(line.split()[1]) / 1024
    status = Path(f"/proc/{pid}/status").read_text().splitlines()
    result["peak_rss_mib"] = next(int(line.split()[1]) / 1024 for line in status if line.startswith("VmHWM:"))
    return result


def sample(engine, binary, seconds, requests):
    with tempfile.TemporaryDirectory(prefix="jarvis-controller-benchmark-") as directory:
        root = Path(directory)
        cfg = copy.deepcopy(DEFAULTS)
        cfg["recording"]["output_dir"] = str(root / "unused-video")
        config = root / "config.json"; config.write_text(json.dumps(cfg))
        path = root / "ipc/control.sock"
        env = dict(os.environ, JARVIS_CONFIG=str(config), JARVIS_SOCKET=str(path), JARVIS_CONTROLLER="python", JARVIS_DRY_RUN="1")
        argv = [sys.executable, "-m", "jarvis", "daemon"] if engine == "python" else [str(binary)]
        with (root / "stderr").open("w+") as errors:
            started = time.perf_counter()
            process = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                while True:
                    try:
                        response = request(path, "status")
                        break
                    except (OSError, ValueError):
                        if process.poll() is not None or time.perf_counter() - started > 10:
                            errors.seek(0)
                            raise RuntimeError(errors.read())
                        time.sleep(.002)
                startup_ms = (time.perf_counter() - started) * 1000
                assert response["status"]["muted"] and not response["status"]["microphone_open"]
                assert response["status"]["replay"]["state"] == "off"
                latencies = []
                for index in range(10 + requests):
                    before = time.perf_counter()
                    request(path, "status")
                    if index >= 10:
                        latencies.append((time.perf_counter() - before) * 1000)
                used = memory(process.pid)
                before = cpu_seconds(process.pid)
                time.sleep(seconds)
                idle_cpu = cpu_seconds(process.pid) - before
                maps = Path(f"/proc/{process.pid}/maps").read_text().lower()
                return {"engine": engine, "startup_to_status_ms": startup_ms,
                        "median_status_ms": statistics.median(latencies), "requests": requests,
                        "idle_seconds": seconds, "idle_cpu_seconds": idle_cpu,
                        "maps_contain_libpython": "libpython" in maps, **used}
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "src/jarvis/bin/jarvis-controller")
    parser.add_argument("--seconds", type=float, default=3)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.binary.is_file() or not 1 <= args.repeats <= 10 or not 1 <= args.seconds <= 30 or not 1 <= args.requests <= 1000:
        parser.error("Supply a built binary, 1–10 repeats, 1–30 seconds and 1–1000 requests")
    samples = []
    for repeat in range(args.repeats):
        # Alternate order so each implementation runs first in some pairs.
        for engine in (("python", "rust") if repeat % 2 == 0 else ("rust", "python")):
            samples.append({"repeat": repeat, **sample(engine, args.binary, args.seconds, args.requests)})
    medians = {engine: {key: statistics.median(s[key] for s in samples if s["engine"] == engine)
                        for key in ("rss_mib", "pss_mib", "peak_rss_mib", "startup_to_status_ms", "median_status_ms", "idle_cpu_seconds")}
               for engine in ("python", "rust")}
    result = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "platform": platform.platform(),
              "python": platform.python_version(), "rust_binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
              "scope": "fresh sequential controllers; private default config and IPC; microphone, voice models and replay off; normal read-only app discovery; excludes UI, workers and encoders",
              "medians": medians, "samples": samples}
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(medians, indent=2))


if __name__ == "__main__":
    main()
