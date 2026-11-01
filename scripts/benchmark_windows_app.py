#!/usr/bin/env python3
"""Whole-application memory and latency benchmark for an installed Windows Jarvis.

Runs on Windows against an installed (or extracted) Jarvis folder. The service
is started with an isolated config and endpoint, consent pre-granted and
MURMUR_DRY_RUN=1, so recognized commands are parsed but never dispatched.
MURMUR_TEST_CAPTURE_FILE replaces the microphone with a generated timeline:

  settle silence -> N x (public "go forward ten meters" fixture + gap) -> long idle

"go forward" is configured as the wake phrase and "ten meters" as a command
shortcut. Memory is sampled for the service process tree (service, voice
worker and any helpers) every --interval seconds using Windows private working
set (resident, non-shareable pages; DLL code shared with other processes is
excluded) and private commit. The settings window is a separate process and is
measured separately at the end. No device, action or network is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time

FIXTURE_SHA256 = "f15c60ec54059d8b66e410d0064945a0b0a04ea56e1ddca1958e493c0cf70e71"
RATE = 32000  # bytes per second of 16 kHz s16le mono


def timeline(fixture: bytes, settle: float, commands: int, gap: float, idle: float):
    silence = lambda seconds: bytes(int(seconds * RATE) // 2 * 2)
    pcm, ends = bytearray(silence(settle)), []
    for _ in range(commands):
        pcm += fixture
        ends.append(len(pcm) / RATE)
        pcm += silence(gap)
    pcm += silence(idle)
    return bytes(pcm), ends


def summarize(samples, start, end, key):
    values = [s[key] for s in samples if start <= s["t"] <= end and s[key] is not None]
    if not values:
        return None
    mib = [v / 1048576 for v in values]
    return {"median_mib": round(statistics.median(mib), 2), "max_mib": round(max(mib), 2),
            "min_mib": round(min(mib), 2), "samples": len(mib)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path, help="Installed Jarvis folder (contains JarvisConsole.exe)")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commands", type=int, default=10)
    parser.add_argument("--settle", type=float, default=40.0)
    parser.add_argument("--gap", type=float, default=12.0)
    parser.add_argument("--idle", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--ui-seconds", type=float, default=20.0)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from murmur import config
    from murmur.process_memory import snapshot

    fixture = args.fixture.read_bytes()
    if hashlib.sha256(fixture).hexdigest() != FIXTURE_SHA256:
        raise SystemExit("Unexpected fixture")
    fixture += bytes((-len(fixture)) % 960)
    work = Path(tempfile.mkdtemp(prefix="jarvis-bench-"))
    pcm, command_ends = timeline(fixture, args.settle, args.commands, args.gap, args.idle)
    (work / "capture.raw").write_bytes(pcm)
    cfg = config.validate({"activation": {"wake_phrase": "go forward", "mode": "wake_word"},
                           "commands": {"ten meters": "start recording"}})
    cfg["privacy"]["onboarded"] = True
    config.save(cfg, work / "config.json")
    env = dict(os.environ, MURMUR_CONFIG=str(work / "config.json"), MURMUR_ENDPOINT=str(work / "run/control.json"),
               MURMUR_DRY_RUN="1", MURMUR_TEST_CAPTURE_FILE=str(work / "capture.raw"))
    os.environ.update(MURMUR_ENDPOINT=env["MURMUR_ENDPOINT"], MURMUR_CONFIG=env["MURMUR_CONFIG"])
    from murmur.client import request, watch

    console = args.app / "JarvisConsole.exe"
    log = (work / "daemon.log").open("wb")
    started = time.monotonic()
    service = subprocess.Popen([str(console), "--internal", "daemon"], env=env, stdout=log, stderr=log,
                               stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    samples, events, marks = [], [], {}
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            t = time.monotonic() - started
            shot = snapshot([service.pid])
            samples.append({"t": t, "pws": shot["totals"]["private_working_set_bytes"],
                            "commit": shot["totals"]["commit_bytes"],
                            "processes": [(p["name"], p["private_working_set_bytes"]) for p in shot["processes"]]})
            stop.wait(args.interval)

    threading.Thread(target=sampler, daemon=True).start()
    deadline = started + 120
    while time.monotonic() < deadline:
        try:
            status = request("status", timeout=1)["status"]
            if "service_ready_s" not in marks:
                marks["service_ready_s"] = time.monotonic() - started
                if status["muted"]:
                    request("mute", muted=False, timeout=10)
            if status["microphone_open"] and status["state"] == "idle" and status.get("engine"):
                marks["usable_s"] = time.monotonic() - started
                break
            if status["state"] == "error":
                raise SystemExit("Service error: " + str(status["message"]))
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.05)
    else:
        raise SystemExit("Service did not become usable within 120 s")
    # The capture file starts with the worker, shortly before it reports ready.
    audio_origin = marks["usable_s"]

    def on_status(message):
        status = message.get("status", {})
        events.append({"t": time.monotonic() - started, "state": status.get("state"),
                       "message": status.get("message")})

    threading.Thread(target=watch, args=(on_status, stop), daemon=True).start()
    total = audio_origin + len(pcm) / RATE
    while time.monotonic() - started < total:
        if service.poll() is not None:
            raise SystemExit("Service exited during the run")
        time.sleep(0.5)
    end_idle = time.monotonic() - started
    # Settings window: a separate process while open.
    ui = subprocess.Popen([str(args.app / "Jarvis.exe")], env=env)
    ui_samples = []
    ui_start = time.monotonic()
    while time.monotonic() - ui_start < args.ui_seconds:
        shot = snapshot([ui.pid])
        ui_samples.append({"t": time.monotonic() - ui_start, "pws": shot["totals"]["private_working_set_bytes"],
                           "commit": shot["totals"]["commit_bytes"]})
        time.sleep(0.5)
    ui.terminate()
    ui.wait(timeout=10)
    stop.set()
    shutdown_start = time.monotonic()
    subprocess.run([str(console), "shutdown"], env=env, capture_output=True, timeout=60)
    try:
        service.wait(timeout=30)
        shutdown_s = round(time.monotonic() - shutdown_start, 3)
    except subprocess.TimeoutExpired:
        # Builds before 0.3.0 wait for open status subscriptions (this harness holds one).
        shutdown_s = None
        subprocess.run(["taskkill", "/PID", str(service.pid), "/T", "/F"], capture_output=True)
        service.wait(timeout=30)
    log.close()

    results = [e for e in events if str(e["message"]).startswith("Dry run")]
    # One dispatch per spoken command; collapse repeated publishes of the same result.
    dispatched = []
    for e in results:
        if not dispatched or e["t"] - dispatched[-1]["t"] > 2:
            dispatched.append(e)
    latencies = []
    for end in command_ends:
        spoken = audio_origin + end
        after = [e["t"] - spoken for e in dispatched if e["t"] >= spoken - 0.5]
        if after and after[0] < args.gap:
            latencies.append(round(after[0], 3))
    o = audio_origin
    windows = {
        "settled_idle": (o + args.settle - 15, o + args.settle - 1),
        "commands": (o + args.settle, o + command_ends[-1] + args.gap),
        "idle_after_commands_first_60s": (o + command_ends[-1] + args.gap, o + command_ends[-1] + args.gap + 60),
        "idle_end_last_30s": (end_idle - 30, end_idle),
    }
    report = {
        "label": args.label, "scope": __doc__.strip().splitlines()[0],
        "environment": {"platform": platform.platform(), "machine": platform.machine(),
                        "cpus": os.cpu_count(), "app": str(args.app),
                        "version": subprocess.run([str(console), "--version"], capture_output=True, text=True).stdout.strip()},
        "workload": {"commands": args.commands, "settle_s": args.settle, "gap_s": args.gap, "idle_s": args.idle,
                     "fixture_sha256": FIXTURE_SHA256, "interval_s": args.interval},
        "marks": {**marks, "shutdown_s": shutdown_s},
        "commands_dispatched": len(dispatched), "command_latency_s": latencies,
        "service_tree": {name: {"private_working_set": summarize(samples, a, b, "pws"),
                                "private_commit": summarize(samples, a, b, "commit")}
                         for name, (a, b) in windows.items()},
        "service_tree_peak": {"private_working_set_mib": round(max(s["pws"] or 0 for s in samples) / 1048576, 2),
                              "private_commit_mib": round(max(s["commit"] or 0 for s in samples) / 1048576, 2)},
        "settings_window": {"private_working_set": summarize(ui_samples, 5, args.ui_seconds, "pws"),
                            "private_commit": summarize(ui_samples, 5, args.ui_seconds, "commit")},
        "processes_at_settled_idle": next((s["processes"] for s in samples if s["t"] >= windows["settled_idle"][1]), []),
        "samples": [{k: s[k] for k in ("t", "pws", "commit")} for s in samples],
        "events": events,
    }
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("label", "marks", "commands_dispatched", "command_latency_s",
                                             "service_tree", "service_tree_peak", "settings_window")}, indent=2))


if __name__ == "__main__":
    main()
