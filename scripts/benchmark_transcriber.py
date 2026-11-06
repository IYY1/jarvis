#!/usr/bin/env python3
"""Compare retained Whisper memory in fresh processes, without a microphone.

Use a public 16 kHz mono s16le PCM fixture (see docs/speech-research.md):
  .venv/bin/python scripts/benchmark_transcriber.py --fixture /tmp/goforward.raw --baseline
  .venv/bin/python scripts/benchmark_transcriber.py --fixture /tmp/goforward.raw

Both runs use the same model, threads and decoding. Compare the per-case text
as well as latency and RSS. These fixture perturbations test allocator changes;
they are not an accent or real-room recognition benchmark.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
from pathlib import Path
import platform
import random
import resource
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def variants(audio: bytes) -> list[tuple[str, bytes]]:
    samples = array.array("h", audio)
    rng = random.Random(42)
    return [
        ("clean", audio),
        ("quiet", array.array("h", (int(x * .2) for x in samples)).tobytes()),
        ("noise", array.array("h", (max(-32768, min(32767, x + rng.randint(-200, 200)))
                                    for x in samples)).tobytes()),
        ("long", ((audio + bytes(16000)) * 2)[:320000]),
        ("silence", bytes(64000)),
        ("short", audio[:32000]),
        ("slower", array.array("h", (samples[min(len(samples) - 1, int(i * .85))]
                                     for i in range(min(160000, int(len(samples) / .85))))).tobytes()),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--baseline", action="store_true", help="Use the original ORT CPU arena and no allocator trim")
    parser.add_argument("--denoise", action="store_true", help="Enable the installed GTCRN denoiser")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--threads", type=int, help="Override the normal thread count for comparison")
    parser.add_argument("--settle-seconds", type=float, default=.2,
                        help="Measure CPU after each decode, including worker spin (default: 0.2 seconds)")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("repeats must be 1–20")
    if args.threads is not None and not 1 <= args.threads <= 32:
        parser.error("threads must be 1–32")
    if not 0 <= args.settle_seconds <= 2:
        parser.error("settle-seconds must be 0–2")
    if not 6400 <= args.fixture.stat().st_size <= 320000:
        parser.error("fixture must contain 0.2–10 seconds of 16 kHz mono s16le PCM")
    audio = args.fixture.read_bytes()
    if len(audio) % 2:
        parser.error("fixture must contain complete 16-bit samples")
    import psutil
    import sherpa_onnx
    from jarvis.transcriber import Transcriber, default_threads
    process = psutil.Process()
    start = time.perf_counter()
    engine = Transcriber(memory_efficient=not args.baseline, threads=args.threads, denoise=args.denoise)
    if args.denoise and engine._denoiser is None:
        parser.error("GTCRN model missing; install it before running the denoiser benchmark")
    result = {"baseline_allocator": args.baseline, "denoise": args.denoise, "model": engine.name,
              "sherpa_onnx": sherpa_onnx.__version__, "threads": args.threads or default_threads(),
              "python": platform.python_version(), "platform": platform.platform(),
              "fixture_sha256": hashlib.sha256(audio).hexdigest(),
              "load_seconds": time.perf_counter() - start,
              "loaded_rss_mib": process.memory_info().rss / 1048576, "cases": []}
    cases = variants(audio)
    for repeat in range(args.repeats):
        for name, pcm in cases:
            start = time.perf_counter()
            cpu = time.process_time()
            text = engine.transcribe(pcm)
            seconds = time.perf_counter() - start
            decode_cpu = time.process_time() - cpu
            cpu = time.process_time()
            time.sleep(args.settle_seconds)
            settle_cpu = time.process_time() - cpu
            result["cases"].append({"case": name, "repeat": repeat, "text": text,
                                    "seconds": seconds, "decode_cpu_seconds": decode_cpu,
                                    "settle_cpu_seconds": settle_cpu,
                                    "rss_mib": process.memory_info().rss / 1048576})
    result["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    result["median_decode_ms"] = statistics.median(case["seconds"] for case in result["cases"]) * 1000
    result["final_rss_mib"] = result["cases"][-1]["rss_mib"]
    result["total_cpu_seconds"] = sum(case["decode_cpu_seconds"] + case["settle_cpu_seconds"]
                                      for case in result["cases"])
    result["settle_seconds_per_case"] = args.settle_seconds
    result["scope"] = "isolated transcriber; excludes Vosk, microphone, service and UI; no audio saved"
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
