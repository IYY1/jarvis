#!/usr/bin/env python3
"""Measure on-demand Whisper: waiting/peak RSS and end-of-speech-to-command latency.

Fresh sequential workers with the local Vosk/Whisper models and a paced fake
pw-record that plays a deliberately supplied public fixture as a one-breath
"go forward ten meters" (surrogate wake name and command). No microphone,
desktop actions or downloads. Latency runs from the fixture's last voiced
frame to the worker's command event, so it includes Vosk's endpoint wait.
"""
from __future__ import annotations
import argparse
import array
import copy
import hashlib
import json
import math
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
from murmur.commands import Parser
from murmur.config import DEFAULTS
from murmur.native_worker import prepare
from murmur.process_memory import linux_memory

CAPTURE = """#!/usr/bin/env python3
import os, time
from pathlib import Path
audio = Path(os.environ['JARVIS_BENCH_PCM']).read_bytes()
audio += bytes((-len(audio)) % 960)
log = open(os.environ['JARVIS_BENCH_LOG'], 'a', buffering=1)
def feed(frames):
    for start in range(0, len(frames), 960):
        os.write(1, frames[start:start + 960])
        time.sleep(.03)
feed(bytes(960) * round(float(os.environ['JARVIS_BENCH_LEAD']) / .03))
for _ in range(int(os.environ['JARVIS_BENCH_REPEATS'])):
    log.write(f"start {time.monotonic()}\\n")
    feed(audio)
    log.write(f"end {time.monotonic()}\\n")
    feed(bytes(960) * round(float(os.environ['JARVIS_BENCH_GAP']) / .03))
while True:
    feed(bytes(960))
"""


def trailing_silence(pcm: bytes) -> float:
    """Seconds after the last 30 ms frame at or above the worker's default gate."""
    samples = array.array("h", pcm[:len(pcm) // 2 * 2])
    voiced = [i for i in range(0, len(samples), 480)
              if math.sqrt(sum(s * s for s in samples[i:i + 480]) / len(samples[i:i + 480])) / 32768 >= .005]
    return (len(samples) - (voiced[-1] + 480)) / 16000 if voiced else 0.0


def whisper_threads(pid: int) -> int:
    count = 0
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            count += (task / "comm").read_text().startswith("whisper-")
        except OSError:
            pass
    return count


def rss_mib(pid: int) -> float:
    return int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576


def session(binary: Path, fixture: Path, idle: float | None, lead: float, gap: float, repeats: int,
            tail: float, denoise: bool, fast_path: bool = False, free_decoder: bool = True,
            finalize_utterances: bool = False, whisper_files: dict | None = None) -> dict:
    config = copy.deepcopy(DEFAULTS)
    config["privacy"]["onboarded"] = True
    config["recognition"].update(accurate=True, noise_suppression=denoise, closest_match=free_decoder)
    config["activation"]["wake_phrase"] = "go forward"
    grammar = Parser({"ten meters": "start recording"}).grammar(["firefox", "steam", "stremio"])
    with patch.dict(os.environ, MURMUR_VOICE_WORKER="rust"):
        _argv, payload, env = prepare(config, "wake_word", grammar)
    payload["native"]["vosk_fast_path"] = fast_path
    payload["native"]["vosk_finalize_utterances"] = finalize_utterances
    if whisper_files is not None:
        if set(whisper_files) != {"encoder", "decoder", "tokens"} or not all(Path(p).is_file() for p in whisper_files.values()):
            raise ValueError("Supply existing encoder, decoder and tokens files")
        payload["native"]["whisper"] = whisper_files
    if payload["native"]["whisper"] is None or (denoise and payload["native"]["denoiser"] is None):
        raise SystemExit("Install the local Whisper (and GTCRN for --denoise) models first")
    if idle is None:
        payload["native"].pop("whisper_idle_seconds", None)
    else:
        payload["native"]["whisper_idle_seconds"] = idle
    pcm = fixture.read_bytes()
    with tempfile.TemporaryDirectory(prefix="jarvis-on-demand-") as folder:
        root = Path(folder)
        capture = root / "pw-record"
        capture.write_text(CAPTURE)
        capture.chmod(0o700)
        log = root / "speech.log"
        env.update(PATH=f"{root}:{env.get('PATH', '')}", JARVIS_BENCH_PCM=str(fixture.resolve()),
                   JARVIS_BENCH_LOG=str(log), JARVIS_BENCH_LEAD=str(lead), JARVIS_BENCH_GAP=str(gap),
                   JARVIS_BENCH_REPEATS=str(repeats))
        start = time.monotonic()
        process = subprocess.Popen([str(binary.resolve())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   env=env, bufsize=0)
        samples, commands, ready = [], [], None
        command_memory = []
        try:
            process.stdin.write((json.dumps(payload) + "\n").encode())
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while len(commands) < repeats or time.monotonic() < commands[-1][0] + tail:
                    if time.monotonic() - start > 60 + lead + repeats * (gap + 8):
                        raise RuntimeError("The worker did not recognize every fixture command")
                    if process.poll() is not None:
                        raise RuntimeError("The worker exited during the benchmark")
                    samples.append((time.monotonic(), rss_mib(process.pid), whisper_threads(process.pid)))
                    if not selector.select(.02):
                        continue
                    message = json.loads(process.stdout.readline())
                    if message["event"] == "error":
                        raise RuntimeError(message["message"])
                    if message["event"] == "ready" and ready is None:
                        ready = time.monotonic()
                    if message["event"] == "command":
                        commands.append((time.monotonic(), message["text"], message.get("recognition_pass")))
                        command_memory.append(linux_memory(process.pid))
            status = Path(f"/proc/{process.pid}/status").read_text()
            peak = next(int(line.split()[1]) / 1024 for line in status.splitlines() if line.startswith("VmHWM:"))
            stat = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
            cpu = (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()
        marks = [line.split() for line in log.read_text().splitlines()]
    starts = [float(t) for kind, t in marks if kind == "start"]
    ends = [float(t) - trailing_silence(pcm) for kind, t in marks if kind == "end"]

    def before(moment):
        return max((s for s in samples if s[0] <= moment), key=lambda s: s[0])

    results = []
    for (received, text, recognition_pass), began, ended in zip(commands, starts, ends, strict=True):
        results.append({"text": text, "latency_ms": round((received - ended) * 1000, 1),
                        "whisper_loaded_at_speech_start": before(began)[2] > 0,
                        "recognition_pass": recognition_pass,
                        "rss_at_speech_start_mib": round(before(began)[1], 2)})
    waiting = [rss for t, rss, _ in samples if ready + .5 <= t < starts[0]]
    unloaded = [rss for t, rss, threads in samples if t > commands[0][0] and threads == 0]
    first_load = next((t for t, _, threads in samples if threads > 0), float("inf"))
    released = [rss for t, rss, threads in samples if t > max(commands[0][0], first_load) and threads == 0]
    return {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "experimental_vosk_fast_path": fast_path, "free_decoder": free_decoder,
            "experimental_vosk_finalize_utterances": finalize_utterances,
            "experimental_whisper_files": whisper_files,
            "whisper_idle_seconds": idle, "gap_seconds": gap, "denoise": denoise,
            "ready_seconds": round(ready - start, 3),
            "waiting_rss_mib": round(statistics.median(waiting), 2),
            "released_rss_mib": round(statistics.median(released), 2) if released else None,
            "unloaded_after_commands_rss_mib": round(statistics.median(unloaded), 2) if unloaded else None,
            "max_sampled_rss_mib": round(max(rss for _, rss, _ in samples), 2),
            "peak_rss_mib": round(peak, 2), "cpu_seconds": round(cpu, 2), "commands": results,
            "memory_after_commands": command_memory, "max_observed_whisper_threads": max(s[2] for s in samples)}


def summary(run: dict) -> dict:
    def latency(cold):
        values = [c["latency_ms"] for c in run["commands"] if c["whisper_loaded_at_speech_start"] != cold]
        return round(statistics.median(values), 1) if values else None
    return {k: run[k] for k in ("ready_seconds", "waiting_rss_mib", "released_rss_mib", "peak_rss_mib", "cpu_seconds")} | {
        "cold_median_latency_ms": latency(True), "warm_median_latency_ms": latency(False),
        "commands": len(run["commands"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True, help="Saved worker that keeps Whisper loaded")
    parser.add_argument("--after", type=Path, default=ROOT / "native/voice-worker/target/release/jarvis-voice")
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--denoise", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be 1–10")
    audio = args.fixture.read_bytes()
    if not 6400 <= len(audio) <= 320000 or len(audio) % 2:
        parser.error("fixture must be 0.2–10 seconds of s16le PCM")
    # Sequential, never concurrent: inference workloads would compete for CPU.
    runs = {
        "before_resident": session(args.before, args.fixture, None, 4, 6, args.repeats, 1, args.denoise),
        # Idle 3 s with 6 s gaps: every command starts with Whisper released.
        "after_cold": session(args.after, args.fixture, 3, 4, 6, args.repeats, 5, args.denoise),
        # Idle 20 s with 3.5 s gaps: the first command loads, the rest reuse it.
        "after_warm": session(args.after, args.fixture, 20, 4, 3.5, args.repeats + 1, 1, args.denoise),
    }
    texts = {c["text"] for run in runs.values() for c in run["commands"]}
    result = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "platform": platform.platform(),
              "fixture_sha256": hashlib.sha256(audio).hexdigest(),
              "fixture_trailing_silence_seconds": round(trailing_silence(audio), 3),
              "commands_identical": texts == {"ten meters"},
              "scope": "complete Rust voice worker with real Vosk, Whisper small.en and optional GTCRN, paced fake capture of a public one-breath fixture; excludes the capture child, service, UI and real microphone; no actions",
              "summary": {name: summary(run) for name, run in runs.items()}, "runs": runs}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(json.dumps({"commands_identical": result["commands_identical"], "summary": result["summary"]}, indent=2))
    if not result["commands_identical"]:
        raise SystemExit("Worker command results differ")


if __name__ == "__main__":
    main()
