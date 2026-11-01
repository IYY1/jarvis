#!/usr/bin/env python3
"""Compare Rust Whisper intermediate tensors with an optional ONNX Runtime.

This diagnostic requires numpy and onnxruntime in its Python environment, not
in the application runtime. It records numerical differences, checks decoder
argmax under identical encoder tensors/token history, and does not assert
bitwise or floating-point equivalence. Use benchmark_rust_voice.py separately
for end-to-end transcript parity and fresh-process resource measurements.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from murmur.models import accurate_model_dir
from benchmark_transcriber import variants


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--model", type=Path, default=accurate_model_dir())
    parser.add_argument("--model-name", choices=("tiny.en", "base.en", "small.en"), default="small.en")
    parser.add_argument("--case", action="append", choices=("clean", "quiet", "noise", "long", "silence", "short", "slower"))
    parser.add_argument("--binary", type=Path, default=ROOT / "native/whisper-recognizer/target/release/jarvis-whisper")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 6400 <= args.fixture.stat().st_size <= 320000 or args.fixture.stat().st_size % 2:
        parser.error("fixture must be 0.2–10 seconds of complete s16le samples")
    import numpy as np
    import onnxruntime as ort
    ort.disable_telemetry_events()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 8
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    encoder = ort.InferenceSession(str(args.model / f"{args.model_name}-encoder.int8.onnx"), options,
                                  providers=["CPUExecutionProvider"])
    decoder = ort.InferenceSession(str(args.model / f"{args.model_name}-decoder.int8.onnx"), options,
                                  providers=["CPUExecutionProvider"])

    def difference(actual, reference):
        delta = actual.astype(np.float64) - reference
        return {"max_abs": float(abs(delta).max()),
                "relative_rms": float(np.linalg.norm(delta) / max(np.linalg.norm(reference.astype(np.float64)), 1e-30))}

    audio = args.fixture.read_bytes()
    def checksum(path):
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    report = {"onnxruntime_reference": ort.__version__, "scope": "offline tensor diagnostic; excludes resource comparisons",
              "fixture_sha256": hashlib.sha256(audio).hexdigest(), "model_name": args.model_name,
              "model_sha256": {name: checksum(args.model / name)
                               for name in (f"{args.model_name}-encoder.int8.onnx", f"{args.model_name}-decoder.int8.onnx")},
              "cases": []}
    with tempfile.TemporaryDirectory(prefix="jarvis-whisper-tensors-") as directory:
        root = Path(directory)
        for name, pcm in variants(audio):
            if name not in (args.case or ("clean", "long")):
                continue
            fixture = root / f"{name}.raw"
            fixture.write_bytes(pcm)
            trace = root / name
            subprocess.run([str(args.binary.resolve()), "--model-name", args.model_name, "--diagnose", str(args.model.resolve()), str(fixture), str(trace)],
                           check=True, stdout=subprocess.DEVNULL, timeout=30)
            metadata = json.loads((trace / "trace.json").read_text())
            mel = np.fromfile(trace / "mel.f32", dtype="<f4").reshape(metadata["mel_shape"])
            cross = [np.fromfile(trace / f"cross-{letter}.f32", dtype="<f4").reshape(metadata["cross_shape"])
                     for letter in ("k", "v")]
            expected_cross = encoder.run(None, {"mel": mel})
            comparison = {"case": name, "text": metadata["text"], "feature_frames": metadata["feature_frames"],
                          "cross_k": difference(cross[0], expected_cross[0]),
                          "cross_v": difference(cross[1], expected_cross[1]), "decoder_steps": []}
            layers, _, _, state = metadata["cross_shape"]
            self_k = np.zeros((layers, 1, 448, state), dtype=np.float32)
            self_v = self_k.copy()
            tokens, offset = [50257, 50362], 0
            for step, predicted in enumerate(metadata["tokens"]):
                logits, self_k, self_v = decoder.run(None, {
                    "tokens": np.array([tokens], dtype=np.int64),
                    "in_n_layer_self_k_cache": self_k, "in_n_layer_self_v_cache": self_v,
                    "n_layer_cross_k": cross[0], "n_layer_cross_v": cross[1],
                    "offset": np.array([offset], dtype=np.int64)})
                expected = logits[0, -1]
                actual = np.fromfile(trace / f"step-{step:03}.f32", dtype="<f4")
                comparison["decoder_steps"].append({"rust_token": predicted, "reference_token": int(expected.argmax()),
                                                    **difference(actual, expected)})
                offset += len(tokens)
                tokens = [predicted]
            report["cases"].append(comparison)
    report["decoder_argmax_identical"] = all(s["rust_token"] == s["reference_token"]
                                            for c in report["cases"] for s in c["decoder_steps"])
    report["numerically_identical"] = False
    report["interpretation"] = "Matching argmax on this fixture does not prove tensor equivalence or general recognition accuracy."
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"decoder_argmax_identical": report["decoder_argmax_identical"], "cases": len(report["cases"])}))
    if not report["decoder_argmax_identical"]:
        raise SystemExit("Decoder argmax differs; inspect the saved diagnostic before claiming parity")


if __name__ == "__main__":
    main()
