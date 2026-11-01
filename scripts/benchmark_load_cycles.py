#!/usr/bin/env python3
"""Measure voice worker RSS and latency over Whisper load/release cycles with speech between them.

Fresh sequential workers with the local Vosk/Whisper/GTCRN models and a paced
fake pw-record. Each cycle plays a deliberately supplied public fixture as a
one-breath wake plus command (surrogate wake name), then the fixture
time-reversed without pause: speech-like audio that is never a wake phrase,
followed by 3 seconds of quiet room noise. On-demand Whisper stays loaded through part of that audio and is released
during the rest, as in live use. A resident arm keeps Whisper loaded. Latency
runs from the fixture's last voiced frame to the command event. No microphone,
desktop actions or downloads.
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
from benchmark_whisper_on_demand import rss_mib, trailing_silence, whisper_threads
from murmur.commands import Parser
from murmur.config import DEFAULTS
from murmur.native_worker import prepare

CAPTURE = """#!/usr/bin/env python3
import array, os, random, time
from pathlib import Path
audio = Path(os.environ['JARVIS_BENCH_PCM']).read_bytes()
audio += bytes((-len(audio)) % 960)
reverse = array.array('h', audio)
reverse.reverse()
reverse = reverse.tobytes()
log = open(os.environ['JARVIS_BENCH_LOG'], 'a', buffering=1)
def feed(frames):
    for start in range(0, len(frames), 960):
        os.write(1, frames[start:start + 960])
        time.sleep(.03)
silence = lambda seconds: bytes(960) * round(seconds / .03)
# Keep the historical room-noise workload for comparable memory measurements.
# It lets Vosk end the chatter itself; --no-room-noise instead exercises the
# worker's quiet-boundary reset and must not need this workaround.
random.seed(1)
noise = array.array('h', (random.randint(-680, 680) for _ in range(16000 * 3))).tobytes()
if os.environ['JARVIS_BENCH_NOISE'] != '1':
    noise = silence(3)
feed(silence(4))
for _ in range(int(os.environ['JARVIS_BENCH_CYCLES'])):
    log.write(f"start {time.monotonic()}\\n")
    feed(audio)
    log.write(f"end {time.monotonic()}\\n")
    feed(silence(1.5))
    speech = b''
    while len(speech) < 32000 * float(os.environ['JARVIS_BENCH_SPEECH']):
        speech += reverse
    feed(speech)
    feed(noise)
    feed(silence(.5))
while True:
    feed(bytes(960))
"""


def session(binary: Path, fixture: Path, idle: float | None, cycles: int, speech: float, noise: bool,
            fast_path: bool = False, finalize_utterances: bool = False) -> dict:
    config = copy.deepcopy(DEFAULTS)
    config["privacy"]["onboarded"] = True
    config["recognition"].update(accurate=True, noise_suppression=True)
    config["activation"]["wake_phrase"] = "go forward"
    grammar = Parser({"ten meters": "start recording"}).grammar(["firefox", "steam", "stremio"])
    with patch.dict(os.environ, MURMUR_VOICE_WORKER="rust"):
        _argv, payload, env = prepare(config, "wake_word", grammar)
    payload["native"]["vosk_fast_path"] = fast_path
    payload["native"]["vosk_finalize_utterances"] = finalize_utterances
    if payload["native"]["whisper"] is None or payload["native"]["denoiser"] is None:
        raise SystemExit("Install the local Whisper and GTCRN models first")
    if idle is None:
        payload["native"].pop("whisper_idle_seconds", None)
    else:
        payload["native"]["whisper_idle_seconds"] = idle
    pcm = fixture.read_bytes()
    with tempfile.TemporaryDirectory(prefix="jarvis-load-cycles-") as folder:
        root = Path(folder)
        capture = root / "pw-record"
        capture.write_text(CAPTURE)
        capture.chmod(0o700)
        log = root / "speech.log"
        env.update(PATH=f"{root}:{env.get('PATH', '')}", JARVIS_BENCH_PCM=str(fixture.resolve()),
                   JARVIS_BENCH_LOG=str(log), JARVIS_BENCH_CYCLES=str(cycles), JARVIS_BENCH_SPEECH=str(speech),
                   JARVIS_BENCH_NOISE="1" if noise else "0")
        start = time.monotonic()
        process = subprocess.Popen([str(binary.resolve())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   env=env, bufsize=0)
        samples, commands = [], []
        limit = 60 + cycles * (speech + 12)
        try:
            process.stdin.write((json.dumps(payload) + "\n").encode())
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while len(commands) < cycles or time.monotonic() < commands[-1][0] + speech:
                    if time.monotonic() - start > limit:
                        raise RuntimeError(f"Only {len(commands)} of {cycles} commands were recognized")
                    if process.poll() is not None:
                        raise RuntimeError("The worker exited during the benchmark")
                    samples.append((time.monotonic(), rss_mib(process.pid), whisper_threads(process.pid)))
                    if not selector.select(.25):
                        continue
                    message = json.loads(process.stdout.readline())
                    if message["event"] == "error":
                        raise RuntimeError(message["message"])
                    if message["event"] == "command":
                        commands.append((time.monotonic(), message["text"]))
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
    rows = []
    for (received, text), began, ended in zip(commands, starts, ends, strict=True):
        rows.append({"text": text, "latency_ms": round((received - ended) * 1000, 1),
                     "wake_elapsed_seconds": round(began - start, 3),
                     "whisper_loaded_at_speech_start": before(began)[2] > 0,
                     "rss_at_wake_mib": round(before(began)[1], 2)})
    floors = [row["rss_at_wake_mib"] for row in rows]
    latencies = [row["latency_ms"] for row in rows]
    return {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "experimental_vosk_fast_path": fast_path,
            "experimental_vosk_finalize_utterances": finalize_utterances,
            "whisper_idle_seconds": idle, "cycles": cycles, "speech_seconds_per_cycle": speech,
            "room_noise_after_speech": noise,
            "elapsed_seconds": round(samples[-1][0] - start, 3),
            "commands_identical": {row["text"] for row in rows} == {"ten meters"},
            "rss_at_wake_first_mib": floors[0], "rss_at_wake_last_mib": floors[-1],
            "rss_at_wake_growth_per_cycle_mib": round((floors[-1] - floors[1]) / (len(floors) - 2), 2),
            "max_rss_mib": round(max(rss for _, rss, _ in samples), 2), "peak_rss_mib": round(peak, 2),
            "median_latency_ms": round(statistics.median(latencies), 1),
            "max_latency_ms": max(latencies), "cpu_seconds": round(cpu, 2), "commands": rows,
            "rss_samples": [[round(t - start, 1), round(rss, 2), threads] for t, rss, threads in samples[::8]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--arm", action="append", default=[], metavar="NAME=PATH",
                        help="On-demand worker to measure (repeatable)")
    parser.add_argument("--resident", action="append", default=[], metavar="NAME=PATH",
                        help="Worker to measure with Whisper kept loaded (repeatable)")
    parser.add_argument("--cycles", type=int, default=15)
    parser.add_argument("--idle", type=float, default=20, help="Whisper idle limit for on-demand arms")
    parser.add_argument("--speech", type=float, default=35, help="Seconds of reversed speech per cycle")
    parser.add_argument("--room-noise", action=argparse.BooleanOptionalAction, default=True,
                        help="Quiet noise after the chatter so Vosk ends its utterance (default on)")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--vosk-fast-path", action="store_true", help="Exercise the experimental Rust fast path (off by default)")
    parser.add_argument("--finalize-vosk-utterances", action="store_true", help="Exercise experimental decoder finalization (off by default)")
    args = parser.parse_args()
    if not 3 <= args.cycles <= 100:
        parser.error("cycles must be 3–100")
    if args.speech <= args.idle + 5:
        parser.error("speech must outlast the idle limit so Whisper is released each cycle")
    audio = args.fixture.read_bytes()
    if not 6400 <= len(audio) <= 320000 or len(audio) % 2:
        parser.error("fixture must be 0.2–10 seconds of s16le PCM")
    arms = [(spec, args.idle) for spec in args.arm] + [(spec, None) for spec in args.resident]
    if not arms:
        parser.error("give at least one --arm or --resident")
    runs = {}
    # Sequential, never concurrent: inference workloads would compete for CPU.
    for spec, idle in arms:
        name, _, path = spec.partition("=")
        runs[name] = session(Path(path), args.fixture, idle, args.cycles, args.speech, args.room_noise,
                             args.vosk_fast_path, args.finalize_vosk_utterances)
    result = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "platform": platform.platform(),
              "fixture_sha256": hashlib.sha256(audio).hexdigest(),
              "fixture_trailing_silence_seconds": round(trailing_silence(audio), 3),
              "scope": "complete Rust voice worker with real Vosk, Whisper small.en and GTCRN; paced fake capture of a public fixture (wake+command, then time-reversed speech-like audio each cycle); excludes the capture child, service, UI and real microphone; no actions",
              "runs": runs}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(json.dumps({name: {k: v for k, v in run.items() if k not in ("rss_samples", "commands", "binary_sha256")}
                      for name, run in runs.items()}, indent=2))
    print(json.dumps({name: [c["rss_at_wake_mib"] for c in run["commands"]] for name, run in runs.items()}))
    if not all(run["commands_identical"] for run in runs.values()):
        raise SystemExit("Worker command results differ from the fixture command")


if __name__ == "__main__":
    main()
