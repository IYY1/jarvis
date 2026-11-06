#!/usr/bin/env python3
"""Compare Python and Rust with identical local models and public PCM variants.

Fresh, sequential processes. No microphone, desktop actions or model downloads.
The JSON includes every transcript; use only a deliberately supplied fixture.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import selectors
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from benchmark_transcriber import variants
from jarvis.commands import Parser
from jarvis.config import DEFAULTS
from jarvis.native_worker import prepare


def response(process):
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"Rust worker exited ({process.poll()})")
    result = json.loads(line)
    if result.get("event") == "error":
        raise RuntimeError(result["message"])
    return result


def rust_run(fixture: bytes, repeats: int, denoise: bool, threads: int, binary: Path | None = None):
    config = copy.deepcopy(DEFAULTS)
    config["recognition"]["noise_suppression"] = denoise
    with patch.dict(os.environ, JARVIS_VOICE_WORKER="rust"):
        argv, payload, env = prepare(config, "once", Parser().grammar([]))
    payload["native"]["threads"] = threads
    if binary is not None:
        argv = [str(binary.resolve())]
    process = subprocess.Popen(argv + ["--transcribe"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               text=True, env=env)
    try:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        ready = response(process)
        cases = []
        with tempfile.TemporaryDirectory(prefix="jarvis-rust-fixtures-") as folder:
            for index, (name, pcm) in enumerate(variants(fixture)):
                path = Path(folder) / f"{index}.raw"
                path.write_bytes(pcm)
            for repeat in range(repeats):
                for index, (name, _pcm) in enumerate(variants(fixture)):
                    process.stdin.write(json.dumps({"path": str(Path(folder) / f"{index}.raw"), "settle_seconds": .2}) + "\n")
                    process.stdin.flush()
                    result = response(process)
                    cases.append({"case": name, "repeat": repeat, "text": result["text"],
                                  "seconds": result["seconds"], "decode_cpu_seconds": result["decode_cpu_seconds"],
                                  "settle_cpu_seconds": result["settle_cpu_seconds"], "rss_mib": result["memory"]["rss_mib"]})
                    peak = result["memory"]["peak_rss_mib"]
        process.stdin.close()
        if process.wait(timeout=10):
            raise RuntimeError("Rust benchmark did not exit successfully")
        with Path(argv[0]).open("rb") as source:
            binary_sha256 = hashlib.file_digest(source, "sha256").hexdigest()
        return {"worker": "rust", "denoise": denoise, "threads": threads, "whisper": ready["whisper"],
                "binary_sha256": binary_sha256,
                "load_seconds": ready["load_seconds"], "loaded_rss_mib": ready["memory"]["rss_mib"],
                "load_peak_rss_mib": ready["memory"]["peak_rss_mib"],
                "final_rss_mib": cases[-1]["rss_mib"], "peak_rss_mib": peak,
                "median_decode_ms": statistics.median(c["seconds"] for c in cases) * 1000,
                "total_cpu_seconds": sum(c["decode_cpu_seconds"] + c["settle_cpu_seconds"] for c in cases), "cases": cases}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def worker_run(worker: str, fixture: Path, repeats: int, denoise: bool, binary: Path | None = None):
    """Exercise the real worker loop with a paced fake pw-record process."""
    import psutil
    config = copy.deepcopy(DEFAULTS)
    config["privacy"]["onboarded"] = True
    config["recognition"]["noise_suppression"] = denoise
    config["activation"]["wake_phrase"] = "go forward"
    grammar = Parser({"ten meters": "start recording"}).grammar(["firefox", "steam", "stremio"])
    with patch.dict(os.environ, JARVIS_VOICE_WORKER=worker):
        argv, payload, env = prepare(config, "wake_word", grammar)
    if binary is not None:
        if worker != "rust":
            raise ValueError("A native binary can only replace the Rust worker")
        argv = [str(binary.resolve())]
    with tempfile.TemporaryDirectory(prefix="jarvis-fake-capture-") as folder:
        root = Path(folder)
        capture = root / "pw-record"
        capture.write_text("""#!/usr/bin/env python3
import os, time
from pathlib import Path
audio = Path(os.environ['JARVIS_BENCH_PCM']).read_bytes()
audio += bytes((-len(audio)) % 960)
silence = bytes(960)
def feed(pcm):
    for start in range(0, len(pcm), 960):
        os.write(1, pcm[start:start + 960])
        time.sleep(.03)
feed(silence * 20)
for _ in range(int(os.environ['JARVIS_BENCH_REPEATS'])):
    feed(audio)
    feed(silence * 90)
while True: feed(silence)
""")
        capture.chmod(0o700)
        env.update(PATH=str(root) + ":" + env.get("PATH", ""), JARVIS_BENCH_PCM=str(fixture.resolve()),
                   JARVIS_BENCH_REPEATS=str(repeats))
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, bufsize=0)
        measured = psutil.Process(process.pid)
        start = time.monotonic()
        commands, command_rss = [], []
        loaded, load_peak, peak = None, None, 0
        try:
            process.stdin.write((json.dumps(payload) + "\n").encode())
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while len(commands) < repeats:
                    if time.monotonic() - start > 15 + repeats * 8:
                        raise RuntimeError(f"{worker} did not recognize the expected fixture commands")
                    if process.poll() is not None:
                        raise RuntimeError(f"{worker} exited before the benchmark completed")
                    peak = max(peak, measured.memory_info().rss / 1048576)
                    if not selector.select(.02):
                        continue
                    message = json.loads(process.stdout.readline())
                    if message["event"] == "error":
                        raise RuntimeError(message["message"])
                    if message["event"] == "ready":
                        loaded = measured.memory_info().rss / 1048576
                        load_seconds = time.monotonic() - start
                        status = Path(f"/proc/{process.pid}/status").read_text()
                        load_peak = next(float(l.split()[1]) / 1024 for l in status.splitlines() if l.startswith("VmHWM:"))
                    if message["event"] == "command":
                        commands.append(message["text"])
                        command_rss.append(measured.memory_info().rss / 1048576)
            time.sleep(.2)
            cpu = measured.cpu_times()
            final = measured.memory_info().rss / 1048576
            status = Path(f"/proc/{process.pid}/status").read_text()
            peak = next(float(l.split()[1]) / 1024 for l in status.splitlines() if l.startswith("VmHWM:"))
            return {"worker": worker, "denoise": denoise, "commands": commands, "load_seconds": load_seconds,
                    "binary_sha256": hashlib.sha256(Path(argv[0]).read_bytes()).hexdigest() if worker == "rust" else None,
                    "loaded_rss_mib": loaded, "load_peak_rss_mib": load_peak,
                    "final_rss_mib": final, "peak_rss_mib": peak,
                    "command_rss_mib": command_rss, "cpu_seconds": cpu.user + cpu.system,
                    "elapsed_seconds": time.monotonic() - start}
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait()
            process.stdin.close(); process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--denoise", action="store_true")
    parser.add_argument("--full-worker", action="store_true", help="Compare Vosk+Whisper and the capture loop with fake paced input")
    parser.add_argument("--before-rust", type=Path, help="Compare a saved older Rust worker with the current build")
    parser.add_argument("--after-rust", type=Path, help="Measure a native build without installing it in the running checkout")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20 or not 1 <= args.threads <= 32:
        parser.error("repeats must be 1–20 and threads 1–32")
    audio = args.fixture.read_bytes()
    if not 6400 <= len(audio) <= 320000 or len(audio) % 2:
        parser.error("fixture must be 0.2–10 seconds of s16le PCM")
    if args.full_worker:
        if args.threads != 8:
            parser.error("full-worker uses the configured default thread count")
        before = worker_run("rust" if args.before_rust else "python", args.fixture, args.repeats, args.denoise, args.before_rust)
        after = worker_run("rust", args.fixture, args.repeats, args.denoise, args.after_rust)
        paired = before["commands"] == after["commands"] == ["ten meters"] * args.repeats
        result = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "platform": platform.platform(),
                  "fixture_sha256": hashlib.sha256(audio).hexdigest(), "commands_identical": paired,
                  "scope": "complete voice worker Vosk+Whisper with fake paced capture; excludes capture child, service, UI and real microphone; surrogate wake go forward and allowlisted ten meters command; no actions",
                  "comparison": "before/after Rust" if args.before_rust else "Python/Rust",
                  ("before" if args.before_rust else "python"): before,
                  ("after" if args.before_rust else "rust"): after}
        rendered = json.dumps(result, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered)
        print(rendered, end="")
        if not paired:
            raise SystemExit("Worker command results differ")
        return
    if args.before_rust:
        before = rust_run(audio, args.repeats, args.denoise, args.threads, args.before_rust)
        after = rust_run(audio, args.repeats, args.denoise, args.threads, args.after_rust)
        paired = all((a["case"], a["repeat"], a["text"]) == (b["case"], b["repeat"], b["text"])
                     for a, b in zip(before["cases"], after["cases"], strict=True))
        result = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "platform": platform.platform(),
                  "fixture_sha256": hashlib.sha256(audio).hexdigest(), "transcripts_identical": paired,
                  "scope": "before/after Rust transcriber in fresh sequential processes; same weights, threads and public fixture variants; excludes Vosk, capture, service and UI",
                  "before": before, "after": after}
        if args.output:
            args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"transcripts_identical": paired,
                          "before": {k: before[k] for k in ("final_rss_mib", "median_decode_ms", "total_cpu_seconds")},
                          "after": {k: after[k] for k in ("final_rss_mib", "median_decode_ms", "total_cpu_seconds")}}, indent=2))
        if not paired:
            raise SystemExit("Rust transcript differences detected")
        return
    command = [sys.executable, str(ROOT / "scripts/benchmark_transcriber.py"), "--fixture", str(args.fixture),
               "--repeats", str(args.repeats), "--threads", str(args.threads)]
    if args.denoise:
        command.append("--denoise")
    python = json.loads(subprocess.check_output(command, text=True))
    rust = rust_run(audio, args.repeats, args.denoise, args.threads, args.after_rust)
    paired = all((a["case"], a["repeat"], a["text"]) == (b["case"], b["repeat"], b["text"])
                 for a, b in zip(python["cases"], rust["cases"], strict=True))
    result = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "platform": platform.platform(),
              "fixture_sha256": hashlib.sha256(audio).hexdigest(), "transcripts_identical": paired,
              "scope": "isolated Whisper plus optional GTCRN; excludes Vosk, capture, service and UI; public fixture variants only",
              "python": python, "rust": rust}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
        print(json.dumps({"output": str(args.output), "transcripts_identical": paired,
                          "python": {k: python[k] for k in ("loaded_rss_mib", "final_rss_mib", "peak_rss_mib", "median_decode_ms")},
                          "rust": {k: rust[k] for k in ("loaded_rss_mib", "final_rss_mib", "peak_rss_mib", "median_decode_ms")}}, indent=2))
    else:
        print(rendered, end="")
    if not paired:
        raise SystemExit("Transcript differences detected; inspect the result before enabling Rust")


if __name__ == "__main__":
    main()
