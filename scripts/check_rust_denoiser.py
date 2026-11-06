#!/usr/bin/env python3
"""Numerical and memory comparison of the Rust GTCRN engine and native reference.

Only an explicitly supplied public s16le fixture is read. No microphone,
screen, actions, model downloads or production configuration changes.
"""
from __future__ import annotations
import argparse
import array
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from benchmark_transcriber import variants
from jarvis.models import denoiser_path


def memory():
    values = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    return {"rss_mib": float(values["VmRSS"].split()[0]) / 1024,
            "peak_rss_mib": float(values["VmHWM"].split()[0]) / 1024}


def reference(model):
    import sherpa_onnx
    start = time.perf_counter()
    denoiser = sherpa_onnx.OfflineSpeechDenoiser(sherpa_onnx.OfflineSpeechDenoiserConfig(
        model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
            gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(model=str(model)), num_threads=8)))
    print(json.dumps({"event": "ready", "load_seconds": time.perf_counter() - start, **memory()}), flush=True)
    for line in sys.stdin:
        source, output = line.rstrip("\n").split("\t")
        samples = [v / 32768 for v in array.array("h", Path(source).read_bytes())]
        start = time.perf_counter()
        result = denoiser(samples, 16000)
        elapsed = time.perf_counter() - start
        with open(output, "xb") as stream:
            array.array("f", result.samples).tofile(stream)
        print(json.dumps({"event": "result", "seconds": elapsed, "output_samples": len(result.samples), **memory()}), flush=True)


def measure(argv, requests, env=None):
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    try:
        def receive():
            line = process.stdout.readline()
            if not line:
                raise RuntimeError(f"Denoiser exited: {process.poll()}")
            return json.loads(line)
        ready = receive()
        cases = []
        for name, repeat, source, output in requests:
            process.stdin.write(f"{source}\t{output}\n"); process.stdin.flush()
            cases.append({"case": name, "repeat": repeat, **receive()})
        process.stdin.close()
        if process.wait(timeout=10):
            raise RuntimeError("Denoiser failed")
        return {"loaded_rss_mib": ready["rss_mib"], "load_seconds": ready["load_seconds"],
                "final_rss_mib": cases[-1]["rss_mib"], "peak_rss_mib": max(c["peak_rss_mib"] for c in cases),
                "median_denoise_ms": statistics.median(c["seconds"] for c in cases) * 1000, "cases": cases}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(); process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--model", type=Path, default=denoiser_path())
    parser.add_argument("--binary", type=Path, default=ROOT / "native/gtcrn-denoiser/target/release/jarvis-denoise")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reference-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.reference_worker:
        reference(args.model); return
    if not args.fixture or not 6400 <= args.fixture.stat().st_size <= 320000 or not 1 <= args.repeats <= 20:
        parser.error("Supply a 0.2–10 second s16le fixture and 1–20 repeats")
    audio = args.fixture.read_bytes()
    if len(audio) % 2:
        parser.error("Incomplete 16-bit sample")
    libraries = subprocess.check_output(["ldd", str(args.binary)], text=True)
    if any(name in libraries.lower() for name in ("onnxruntime", "sherpa", "libpython", "libstdc++")):
        raise RuntimeError("Standalone Rust denoiser unexpectedly links a non-Rust inference engine")
    with tempfile.TemporaryDirectory(prefix="jarvis-denoiser-parity-") as directory:
        root = Path(directory)
        sources = []
        for index, (name, pcm) in enumerate(variants(audio)):
            path = root / f"{index}.s16le"; path.write_bytes(pcm); sources.append((name, path))
        requests = [(name, repeat, source) for repeat in range(args.repeats) for name, source in sources]
        native = measure([sys.executable, str(Path(__file__).resolve()), "--reference-worker", "--model", str(args.model)],
                         [(name, repeat, source, root / f"reference-{i}.f32") for i, (name, repeat, source) in enumerate(requests)])
        rust = measure([str(args.binary), "--batch", str(args.model)],
                       [(name, repeat, source, root / f"rust-{i}.f32") for i, (name, repeat, source) in enumerate(requests)])
        import numpy as np
        comparisons = []
        repeat_stable = True
        for index, (name, repeat, _source) in enumerate(requests):
            expected = np.fromfile(root / f"reference-{index}.f32", dtype="<f4").astype(np.float64)
            actual = np.fromfile(root / f"rust-{index}.f32", dtype="<f4").astype(np.float64)
            if expected.shape != actual.shape:
                raise RuntimeError(f"Output lengths differ for {name}")
            error = expected - actual
            maximum = float(np.max(np.abs(error)))
            power = float(np.sum(expected * expected)); error_power = float(np.sum(error * error))
            relative = math.sqrt(error_power / power) if power else 0.0 if not error_power else math.inf
            passed = maximum <= 1e-4 and relative <= 1e-3
            if repeat:
                first = np.fromfile(root / f"rust-{index % len(sources)}.f32", dtype="<f4")
                repeat_stable = repeat_stable and np.array_equal(actual, first)
            comparisons.append({"case": name, "repeat": repeat, "samples": len(actual), "max_absolute_error": maximum,
                                "relative_rms_error": relative, "passed": passed})
    result = {"date": "2026-10-22", "platform": platform.platform(), "scope": "standalone GTCRN; Rust FFT/tract inference/overlap-add versus Python binding to native sherpa-onnx/ONNX Runtime; excludes Whisper, Vosk and microphone",
              "fixture_sha256": hashlib.sha256(audio).hexdigest(), "model_sha256": hashlib.sha256(args.model.read_bytes()).hexdigest(),
              "standalone_dependencies": libraries.splitlines(), "all_waveforms_within_tolerance": all(c["passed"] for c in comparisons),
              "repeated_outputs_identical": repeat_stable, "reference": native, "rust": rust, "comparisons": comparisons}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
        print(json.dumps({"output": str(args.output), "all_waveforms_within_tolerance": result["all_waveforms_within_tolerance"],
                          "repeated_outputs_identical": repeat_stable,
                          "reference": {k: native[k] for k in ("final_rss_mib", "peak_rss_mib", "median_denoise_ms")},
                          "rust": {k: rust[k] for k in ("final_rss_mib", "peak_rss_mib", "median_denoise_ms")}}, indent=2))
    else:
        print(rendered, end="")
    if not result["all_waveforms_within_tolerance"] or not repeat_stable:
        raise SystemExit("Rust denoiser failed numerical parity")


if __name__ == "__main__":
    main()
