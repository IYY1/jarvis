#!/usr/bin/env python3
"""Measure the whole service tree. Live microphone use is an explicit flag.

No audio/transcripts are saved. All action dispatch is disabled during this test.
psutil is a measurement-only dependency, not a service dependency.
"""
from __future__ import annotations
import argparse
import copy
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.config import DEFAULTS


def main():
    import psutil
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["paused", "ptt", "wake", "ui"], default="paused")
    p.add_argument("--seconds", type=float, default=30)
    p.add_argument("--live-microphone", action="store_true")
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    if args.seconds < 5 or args.seconds > 600:
        p.error("Use 5–600 seconds")
    if args.mode == "wake" and not args.live_microphone:
        p.error("Wake mode opens your input; explicitly pass --live-microphone")
    with tempfile.TemporaryDirectory(prefix="jarvis-benchmark-") as temp:
        base = Path(temp)
        cfg = copy.deepcopy(DEFAULTS)
        cfg["privacy"]["onboarded"] = args.mode != "paused"
        cfg["activation"]["mode"] = "wake_word" if args.mode == "wake" else "push_to_talk"
        (base / "config.json").write_text(json.dumps(cfg))
        env = dict(os.environ, JARVIS_CONFIG=str(base / "config.json"), JARVIS_SOCKET=str(base / "ipc/control.sock"), JARVIS_DRY_RUN="1")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        process = subprocess.Popen([sys.executable, "-m", "jarvis", "daemon"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        window = None
        old_socket = os.environ.get("JARVIS_SOCKET")
        os.environ["JARVIS_SOCKET"] = env["JARVIS_SOCKET"]
        from jarvis.client import request
        try:
            until = time.monotonic() + 10
            while True:
                try:
                    status = request("status")["status"]
                    if args.mode != "wake" or status["state"] in ("idle", "error"):
                        break
                except (OSError, KeyError):
                    pass
                if process.poll() is not None or time.monotonic() > until:
                    # read() on a live process's pipe can otherwise wait forever
                    # precisely when this startup deadline is meant to bound it.
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    raise RuntimeError("Service failed to start: " + process.stderr.read(1000).decode(errors="replace"))
                time.sleep(.1)
            if status["state"] == "error":
                raise RuntimeError(status["message"])
            if args.mode == "ui":
                window = subprocess.Popen([sys.executable, "-m", "jarvis", "ui"], env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(2)
                if window.poll() is not None:
                    raise RuntimeError("UI did not remain open. Select a graphical/Broadway display.")
            roots = [psutil.Process(process.pid)] + ([psutil.Process(window.pid)] if window else [])
            # One CPU second per wall second is 100% of ONE logical CPU.
            def sample():
                procs = {}
                for root in roots:
                    try:
                        for item in [root, *root.children(recursive=True)]:
                            procs[item.pid] = item
                    except psutil.Error:
                        pass
                cpu, rss, count = 0.0, 0, 0
                for item in procs.values():
                    try:
                        times = item.cpu_times()
                        cpu += times.user + times.system
                        rss += item.memory_info().rss
                        count += 1
                    except psutil.Error:
                        pass
                return cpu, rss, count
            initial, _, _ = sample()
            start = time.monotonic()
            peak = count_peak = 0
            rss_samples = []
            while time.monotonic() - start < args.seconds:
                if process.poll() is not None or (window and window.poll() is not None):
                    raise RuntimeError("A measured process exited during the sample; no valid result")
                cpu, rss, count = sample()
                peak, count_peak = max(peak, rss), max(count_peak, count)
                rss_samples.append(rss)
                time.sleep(.25)  # the external meter polls; the service does not
            wall = time.monotonic() - start
            cpu, _, _ = sample()
            final_status = request("status")["status"]
            if final_status["state"] == "error" or process.poll() is not None or (window and window.poll() is not None):
                raise RuntimeError("A measured component failed; sample is invalid: " + final_status["message"])
            result = {"mode": args.mode, "duration_seconds": wall,
                      "cpu_seconds": max(0, cpu - initial),
                      "cpu_percent_one_logical_core": max(0, cpu - initial) / wall * 100,
                      "peak_tree_rss_mib": peak / 1048576,
                      "mean_tree_rss_mib": sum(rss_samples) / len(rss_samples) / 1048576,
                      "peak_process_count": count_peak,
                      "status": final_status["state"],
                      "python": platform.python_version(), "kernel": platform.release(),
                      "scope": "Whole service, speech worker and pw-record tree; UI included only in ui mode. Sum RSS double-counts shared pages. Warm-up excluded. Actions disabled. No audio saved.",
                      "live_microphone": args.mode == "wake", "engine": final_status.get("engine"),
                      "voice_gpu_compute": "none; CPU-only recognition"}
            print(json.dumps(result, indent=2))
            if args.output:
                args.output.write_text(json.dumps(result, indent=2) + "\n")
        finally:
            if window:
                window.terminate()
                try:
                    window.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    window.kill()
                    window.wait()
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stderr.close()
            if old_socket is None:
                os.environ.pop("JARVIS_SOCKET", None)
            else:
                os.environ["JARVIS_SOCKET"] = old_socket


if __name__ == "__main__":
    main()
