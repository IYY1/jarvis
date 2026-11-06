#!/usr/bin/env python3
"""Verify unchanged mel/encoder/logit bytes against a saved Rust executable.

Uses only deliberately supplied public PCM and local models. Timing from this
diagnostic includes tensor file writes; use benchmark_rust_voice.py for speed.
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
from benchmark_transcriber import variants
from jarvis.models import accurate_model_dir


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, default=ROOT / "native/whisper-recognizer/target/release/jarvis-whisper")
    parser.add_argument("--model", type=Path, default=accurate_model_dir())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 6400 <= args.fixture.stat().st_size <= 320000 or args.fixture.stat().st_size % 2:
        parser.error("fixture must be 0.2–10 seconds of complete s16le samples")
    report = {"scope": "byte equality of saved mel, encoder and sampled decoder-logit tensors; excludes performance measurement",
              "fixture_sha256": digest(args.fixture), "before_binary_sha256": digest(args.before),
              "after_binary_sha256": digest(args.after), "cases": []}
    for name, audio in variants(args.fixture.read_bytes()):
        with tempfile.TemporaryDirectory(prefix="jarvis-whisper-equivalence-") as directory:
            root = Path(directory)
            fixture = root / "input.raw"
            fixture.write_bytes(audio)
            outputs = []
            for side, binary in (("before", args.before), ("after", args.after)):
                trace = root / side
                subprocess.run([str(binary.resolve()), "--diagnose", str(args.model.resolve()), str(fixture), str(trace)],
                               check=True, stdout=subprocess.DEVNULL, timeout=30)
                metadata = json.loads((trace / "trace.json").read_text())
                tensors = {p.name: digest(p) for p in trace.glob("*.f32")}
                outputs.append(({key: metadata[key] for key in ("text", "tokens", "feature_frames", "mel_shape", "cross_shape")}, tensors))
            equal = outputs[0] == outputs[1]
            report["cases"].append({"case": name, "text": outputs[1][0]["text"], "tensor_count": len(outputs[1][1]),
                                    "tensors_sha256": outputs[1][1], "identical": equal})
    report["all_identical"] = all(c["identical"] for c in report["cases"])
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"all_identical": report["all_identical"], "cases": len(report["cases"]),
                      "tensor_pairs": sum(c["tensor_count"] for c in report["cases"])}))
    if not report["all_identical"]:
        raise SystemExit("Rust intermediate tensors changed; inspect the saved report")


if __name__ == "__main__":
    main()
