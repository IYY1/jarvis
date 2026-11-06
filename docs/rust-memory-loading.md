# Rust model loading memory — 2026-10-25

Three alternating fresh-process startup pairs measured **529.26 → 344.08 MiB
peak RSS**, a reduction of **185.18 MiB (35.0%)**. Median startup time was
1.592 → 1.571 seconds. These are startup peaks with a warm filesystem cache,
the same small.en model and eight threads; they are not idle-memory savings.
See the [startup samples](benchmarks/rust-streaming-load-2026-10-25.json).

The baseline is the completed local Jarvis 0.2.0 refactor, including its
uncommitted changes. Saved source and executables are under
`build/memory-optimization/before/`. This pass preserves that work.

## Implementation

- The Rust ONNX loader streams model/graph/initializer envelopes. The embedding
  is converted from file bytes directly into its existing lossless binary16
  representation using a 16 KiB buffer. It no longer needs serialized and
  expanded float32 copies of the entire table at once. Every value must still
  round-trip exactly; nonexact or nonfinite values retain float32 storage.
- Other raw weights become validated ranges in the same unchanged ONNX file.
  Tract's external-data reader loads those ranges for the graph being compiled.
  The encoder compiles first, then each decoder block. The protobuf graph keeps
  weight metadata instead of another complete set of serialized weight bytes.
  No derived model file or persistent weight cache is created.
- The encoder returns its existing per-layer cross-attention tensors directly.
  This removes concatenation and subsequent split/copies. Diagnostic files keep
  their original concatenated layout. Attention arithmetic, weights, precision,
  padding, token selection and command policy remain unchanged.

For the longest supported input, the former concatenated cross-cache pair alone
held 24 × 1,025 × 768 × 4 bytes, about 72.1 MiB, before the decoder's copies.
Removing that allocation does not imply the same reduction in process peak RSS:
other inference allocations can dominate the maximum.

## Controlled inference measurements

Fresh sequential old/new processes used the same model files, eight threads and
seven public fixture variants repeated three times. The complete worker uses Vosk,
Whisper and GTCRN with three paced surrogate wake/command utterances, excluding
the capture child, service, UI and recorder. Its results are three identical
`ten meters` commands, with no desktop action dispatch.

| Scope, RSS MiB | Before | After |
| --- | ---: | ---: |
| Whisper + GTCRN startup peak | 526.36 | 342.56 |
| Whisper peak including 21 transcriptions | 526.86 | 438.05 |
| Whisper + GTCRN peak including 21 transcriptions | 526.36 | 438.13 |
| Complete worker startup peak | 526.74 | 460.78 |
| Complete worker peak including commands | 571.81 | 556.90 |
| Complete worker retained after commands | 480.97 | 480.59 |

The complete worker saves about 66.0 MiB at startup. Its retained RAM is
essentially unchanged, and its peak during commands improves much less than the
isolated Whisper startup peak. These changes remove temporary copies rather
than resident model weights. RSS is process memory, not a whole-machine RAM
measurement.

| Median decode, milliseconds | Before Rust | After Rust |
| --- | ---: | ---: |
| Whisper | 293.5 | 285.4 |
| Whisper + GTCRN | 352.6 | 381.0 |

A separate fresh Python/Rust comparison measured 327.7 →
295.2 ms without denoising and 409.8 →
359.8 ms with denoising. Rust denoising varied between
360 and 381 ms across these runs. The results support Python-comparable latency
on this fixture; they do not establish a general speedup or zero timing regression.
All 21 paired transcripts matched in each isolated comparison.

Raw comparisons: [Whisper](benchmarks/rust-streaming-whisper-2026-10-25.json),
[GTCRN](benchmarks/rust-streaming-denoise-2026-10-25.json),
[complete worker](benchmarks/rust-streaming-worker-2026-10-25.json),
[Python](benchmarks/rust-streaming-python-2026-10-25.json) and
[Python + GTCRN](benchmarks/rust-streaming-python-denoise-2026-10-25.json).

## Verification

All **55 sampled mel, encoder and decoder-logit tensor pairs** were byte-identical
across seven public audio variants. Token histories and transcripts matched.
The [comparison](benchmarks/rust-streaming-tensors-2026-10-25.json) records binary
and fixture hashes. This is a numerical regression check on the supplied fixture,
not a multi-speaker recognition accuracy study.

All **22 affected Rust tests** passed, and both crates passed Clippy with warnings
denied. New checks cover protobuf bounds, truncation, malformed/duplicate fields,
initializer ranges, reordered tensor metadata, exact compact storage, float32
fallback across I/O chunks, and cache-output byte order.

The full offline Python suite ran **296 tests in 41.414 seconds: 292 passed and
four Windows-only audio cases skipped on Linux**. It used the new checkout
binaries, existing local speech models, the public fixture and private fake
desktop/capture/IPC programs. The new binaries are staged in `src/jarvis/bin/`;
the installed service was not restarted. Changes remain local and uncommitted.

## Default selection and CodeRabbit review

CodeRabbit CLI 0.9.1 completed an authenticated review of the memory changes
and proposed default selection across 26 files, raising **0 issues**. The
completed refactor was the isolated review baseline, preserving its unrelated
uncommitted work. The raw result is saved in
`build/memory-optimization/coderabbit-review.ndjson`.

Linux now prefers the Rust worker when its executable, Vosk library and model
are available. Windows, PocketSphinx and installations missing those assets
retain Python; `JARVIS_VOICE_WORKER=python` selects it explicitly. The final
offline suite ran **300 tests in 41.965 seconds: 296 passed and four Windows-only
audio cases skipped**. It includes default selection and fallback checks.

A read-only service check confirmed the active worker's executable SHA-256
matches the benchmarked, staged `src/jarvis/bin/jarvis-voice`. It was already
running that binary, so no restart was necessary. Mute, recording and replay
state were left untouched. Verification details are saved in
`build/memory-optimization/default-verification.json`.

## Reproduce

Save the prior executables before building, then run these sequentially:

```sh
.venv/bin/python scripts/build_voice_worker.py --test --offline
.venv/bin/python scripts/check_rust_optimization.py \
  --fixture build/refactor-review/goforward.raw \
  --before build/memory-optimization/before/jarvis-whisper \
  --after native/whisper-recognizer/target/release/jarvis-whisper
.venv/bin/python scripts/benchmark_rust_voice.py \
  --fixture build/refactor-review/goforward.raw \
  --before-rust build/memory-optimization/before/jarvis-voice \
  --after-rust native/voice-worker/target/release/jarvis-voice
```

Add `--denoise` for GTCRN, and `--denoise --full-worker` for the complete worker
with paced fake capture. Omit `--before-rust` to compare against Python.
`load_peak_rss_mib` records the high-water mark immediately after loading;
`peak_rss_mib` includes later inference. For startup-only samples, run the old
and new standalone binaries alternately with `--threads 8 --batch MODEL_DIR`
and empty stdin, recording the ready event's memory metrics.

All audio is the deliberately supplied public fixture and its generated variants.
No live microphone, desktop action, installed-service restart or user-settings
change is part of these measurements. The Windows Python runtime is unaffected.
