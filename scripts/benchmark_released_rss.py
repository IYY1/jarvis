#!/usr/bin/env python3
"""Measure voice worker RSS after on-demand Whisper is released while speech continues.

Fresh sequential workers with the local Vosk/Whisper/GTCRN models and a paced
fake pw-record. It plays a deliberately supplied public fixture once as a wake
plus command (surrogate wake name), so Whisper loads, then plays the fixture
time-reversed without pause as speech-like audio that is never a wake phrase.
Whisper stays loaded through the idle limit and is released while that audio
continues. No microphone, desktop actions or downloads.
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
sys.path.insert(0, str(ROOT / "scripts"))
from benchmark_whisper_on_demand import rss_mib, whisper_threads
from jarvis.commands import Parser
from jarvis.config import DEFAULTS
from jarvis.native_worker import prepare

CAPTURE = """#!/usr/bin/env python3
import array, os, time
from pathlib import Path
audio = Path(os.environ['JARVIS_BENCH_PCM']).read_bytes()
audio += bytes((-len(audio)) % 960)
reverse = array.array('h', audio)
reverse.reverse()
reverse = reverse.tobytes()
def feed(frames):
    for start in range(0, len(frames), 960):
        os.write(1, frames[start:start + 960])
        time.sleep(.03)
feed(bytes(960) * 133)
feed(audio)
while True:
    feed(reverse)
"""


def session(binary: Path, fixture: Path, idle: float, seconds: float) -> dict:
    config = copy.deepcopy(DEFAULTS)
    config["privacy"]["onboarded"] = True
    config["recognition"].update(accurate=True, noise_suppression=True)
    config["activation"]["wake_phrase"] = "go forward"
    grammar = Parser({"ten meters": "start recording"}).grammar(["firefox", "steam", "stremio"])
    with patch.dict(os.environ, JARVIS_VOICE_WORKER="rust"):
        _argv, payload, env = prepare(config, "wake_word", grammar)
    if payload["native"]["whisper"] is None or payload["native"]["denoiser"] is None:
        raise SystemExit("Install the local Whisper and GTCRN models first")
    payload["native"]["whisper_idle_seconds"] = idle
    with tempfile.TemporaryDirectory(prefix="jarvis-released-rss-") as folder:
        capture = Path(folder) / "pw-record"
        capture.write_text(CAPTURE)
        capture.chmod(0o700)
        env.update(PATH=f"{folder}:{env.get('PATH', '')}", JARVIS_BENCH_PCM=str(fixture.resolve()))
        start = time.monotonic()
        process = subprocess.Popen([str(binary.resolve())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   env=env, bufsize=0)
        samples, commands = [], []
        try:
            process.stdin.write((json.dumps(payload) + "\n").encode())
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while time.monotonic() - start < seconds:
                    if process.poll() is not None:
                        raise RuntimeError("The worker exited during the benchmark")
                    samples.append((time.monotonic() - start, rss_mib(process.pid), whisper_threads(process.pid)))
                    if not selector.select(.5):
                        continue
                    message = json.loads(process.stdout.readline())
                    if message["event"] == "error":
                        raise RuntimeError(message["message"])
                    if message["event"] == "command":
                        commands.append(message["text"])
            status = Path(f"/proc/{process.pid}/status").read_text()
            peak = next(int(line.split()[1]) / 1024 for line in status.splitlines() if line.startswith("VmHWM:"))
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
    loaded = [s for s in samples if s[2] > 0]
    if commands != ["ten meters"] or not loaded or samples[-1][2]:
        raise RuntimeError(f"Expected one command and a release; got {commands}")
    released_at = next(t for t, _, threads in samples if t > loaded[-1][0] and threads == 0)
    after = [(t - released_at, rss) for t, rss, threads in samples if t >= released_at]

    def at(offset):
        return round(min(after, key=lambda s: abs(s[0] - offset))[1], 2)
    return {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "commands": commands,
            "loaded_rss_median_mib": round(statistics.median(rss for _, rss, _ in loaded), 2),
            "released_after_seconds": round(released_at, 1),
            "rss_after_release_mib": {f"{offset}s": at(offset) for offset in (5, 30, 60, 90, 120, 150)
                                      if offset <= after[-1][0]},
            "max_rss_after_release_mib": round(max(rss for _, rss in after), 2),
            "peak_rss_mib": round(peak, 2),
            "rss_samples": [[round(t, 1), round(rss, 2), threads] for t, rss, threads in samples[::4]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True, help="Saved worker without periodic trims")
    parser.add_argument("--after", type=Path, default=ROOT / "native/voice-worker/target/release/jarvis-voice")
    parser.add_argument("--idle", type=float, default=150, help="Whisper idle limit while speech plays")
    parser.add_argument("--seconds", type=float, default=330)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.seconds < args.idle + 60:
        parser.error("seconds must leave at least 60 seconds after the release")
    audio = args.fixture.read_bytes()
    if not 6400 <= len(audio) <= 320000 or len(audio) % 2:
        parser.error("fixture must be 0.2–10 seconds of s16le PCM")
    # Sequential, never concurrent: inference workloads would compete for CPU.
    runs = {"before": session(args.before, args.fixture, args.idle, args.seconds),
            "after": session(args.after, args.fixture, args.idle, args.seconds)}
    result = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "platform": platform.platform(),
              "fixture_sha256": hashlib.sha256(audio).hexdigest(), "whisper_idle_seconds": args.idle,
              "scope": "complete Rust voice worker with real Vosk, Whisper small.en and GTCRN; paced fake capture of a public fixture (once forward as wake+command, then time-reversed without pause); excludes the capture child, service, UI and real microphone; no actions",
              "runs": runs}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(json.dumps({name: {k: v for k, v in run.items() if k not in ("rss_samples", "binary_sha256")}
                      for name, run in runs.items()}, indent=2))


if __name__ == "__main__":
    main()
