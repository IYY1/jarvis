# Rust Whisper recognizer

Whisper's computation now runs in Rust. This replaces the earlier worker's
sherpa-onnx C API calls. The implementation uses RustFFT for audio features,
tract 0.23.9 for the quantized encoder and decoder operators, and Rust for
cache management, embedding lookup, vocabulary projection, greedy sampling
and byte-token decoding. The existing int8 ONNX linear weights and float32
embedding values are unchanged. The [memory optimization](rust-worker-memory.md)
stores exactly representable embeddings compactly and expands them for float32
computation. There is no retraining or change to the ONNX files.
The [incremental loader and cache changes](rust-memory-loading.md) further
reduce temporary memory while preserving the tested tensor bytes.

The voice worker calls `jarvis_whisper::Recognizer` directly and also uses the
existing [Rust GTCRN denoiser](rust-denoiser.md). Neither computation uses Python,
sherpa-onnx, ONNX Runtime or a subprocess inference engine. The standalone
`jarvis-whisper` and voice worker link only the platform C/math/compiler-support
libraries on the tested Linux system. Vosk wake and boundary detection still
loads the existing native Vosk library. The service and UI remain Python.

## Supported behavior

The default path supports the installed sherpa Whisper **small.en int8 export**,
16 kHz mono commands of 0.2–10 seconds, English transcription, and greedy
search. It validates model metadata, graph wiring, token IDs and size limits.
The compatibility signature identifies this export's wiring; it is not a
cryptographic integrity check. Model installation retains its pinned checksums.
Other Whisper exports, languages, timestamps and GPU inference require separate
implementation and validation.

An explicit [smaller-model experiment](whisper-model-experiments.md) now also
loads allowlisted English tiny.en/base.en int8 exports through the same Rust
kernels. Standalone use requires `--model-name`; service defaults and the model
catalog remain small.en. Tiny passed the available transcript comparison;
base has an unresolved reference discrepancy and remains unvalidated. This
does not authorize a model change or extend support to arbitrary exports.

The front end reproduces the current recognizer's frame placement, symmetric
boundary padding, 400-point periodic Hann window, 160-sample shift, 80 Slaney
mel bands and log normalization. The application's 250 ms silence on either
side and the recognizer's 1000 zero feature frames are preserved.

The encoder runs its existing graph with symbolic audio length. The exported
decoder's data-dependent Slice/ScatterND cache machinery could not be compiled
reliably by tract. Rust instead constructs decoder blocks using the same named
weights and ordinary normalization, quantized linear, attention and GELU
operations. Per-layer caches grow with the tokens in the current command,
reset for the next command, and never retain a previous utterance. Prefill
uses the two original start/no-timestamps tokens and a causal mask. Subsequent
single-token steps attend to the complete accumulated cache. The output limit
matches the reference's six tokens per second, capped at 224.

A targeted optimization precomputes constant int8 weight column sums.
Without it, tract's constant-folding memory budget leaves large int8-to-int32
expansions and reductions at runtime, retaining duplicate weights. A regression
test verifies the optimization preserves exact integer results and removes the
expanded-weight dependency. The incremental loader compiles the encoder first,
then imports decoder blocks individually. Raw initializer payloads are read from
validated ranges in the original ONNX file as needed. The embedding is converted
directly into lossless compact storage without an expanded float32 copy.
A worker without an idle limit still loads Whisper before Vosk; the default
[on-demand](whisper-on-demand.md) worker loads it after a wake name instead. The encoder returns the existing per-layer
cross-attention tensors directly, avoiding concatenation followed by splitting
and copying. Diagnostic tensor files retain their original layout.

Two additional Rust kernels accelerate the hottest operations without changing
precision. Dynamic quantization uses runtime-dispatched AVX2 on supported
x86-64 CPUs, with a portable scalar fallback. It retains division, nearest-even
rounding, separate zero-point addition and saturation exactly. Large conversions
share the configured bounded CPU pool. Contiguous scalar multiplication replaces
the generic broadcast iterator, preserving operand order and copy-on-write
ownership. Neither optimization changes weights, padding or decoding policy.

`--diagnose` now includes per-operator timings in its trace JSON. Normal
recognition does not collect operator profiles. Diagnostic timing includes
explicit tensor writes; the separate benchmark measures decoding without them.

## Build and use

Rust 1.91 or later is required. The build needs no Python speech package or
external inference runtime; the combined application's wake path still needs
its existing Vosk package/library and models.

```sh
.venv/bin/python scripts/build_voice_worker.py --test
MURMUR_VOICE_WORKER=rust ./run-jarvis daemon
```

Building does not change user settings or restart the running service. Python
remains available for compatibility. Linux prefers Rust when its native assets
are available. See [worker selection](rust-voice.md#build-and-select) for
the existing systemd setup. The optional noise-suppression setting selects
Rust GTCRN when using this worker.

The standalone recognizer also builds and runs independently of Python:

```sh
cargo build --release --locked --manifest-path native/whisper-recognizer/Cargo.toml
native/whisper-recognizer/target/release/jarvis-whisper \
  /path/to/sherpa-onnx-whisper-small.en /path/to/input.s16le --threads 8
```

It prints JSON load/resource and recognition results. `--batch MODEL_DIR`
accepts JSON lines containing `{"path":"/absolute/path/to/fixture.raw"}` on
stdin and retains the model. No microphone or desktop actions are involved.
`--diagnose MODEL_DIR INPUT NEW_TRACE_DIR` explicitly saves mel, encoder and
per-step logit tensors for numerical analysis. Ordinary recognition saves no
audio or intermediate tensors. The diagnostic creates a new directory and
never overwrites an existing directory.

## Optimized performance

The controlled before/after median was **671 → 285 ms**, a
57.5% reduction (2.35× faster). Retained RSS was effectively unchanged:
392.2 → 392.5 MiB. Decode CPU across all 21 cases fell from
38.57 to 29.71 CPU-seconds.

A fresh comparison with the Python/native implementation measured:

| Scope | Python median ms | Rust median ms | Python retained MiB | Rust retained MiB | Python decode CPU s | Rust decode CPU s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Whisper | 295 | 292 | 449.1 | 392.6 | 22.84 | 30.03 |
| Whisper + GTCRN | 366 | 348 | 453.4 | 396.1 | 41.93 | 30.74 |

All 21 transcripts matched in each comparison. The complete voice worker
recognized the same three commands, retaining 553.8 MiB versus 601.7 MiB;
peak RSS was 643.7 versus 825.3 MiB. The small hot-latency differences versus
Python do not establish a general speed advantage. Rust still consumes more
decode CPU without denoising, and model loading still takes longer. The
retained-memory benefit remains, without the initial large hot-latency penalty.


The before/after check saved the previous Rust executable and used fresh,
sequential processes with the same weights, eight threads and seven public
fixture variants repeated three times. Binary SHA-256 values are in the report.
A separate strict check compares mel features, encoder key/value tensors and
sampled decoder logits: **all 55 tensor pairs were byte-identical across all
seven variants**. Quantizer unit tests additionally compare the original tract
operator against SIMD, parallel and portable paths on a million activations,
rounding boundaries, unaligned tails, NaNs, infinities and zero/subnormal ranges.
Scaling tests check shared-input ownership and exact output bytes.

The optimized kernels are selected automatically when building the Rust worker;
Python remains available for comparison and compatibility. These are fixture results, not a broad
speaker/accent/noise accuracy study.

Reproduce an optimization comparison by saving the earlier executables before
rebuilding, then running:

```sh
python scripts/benchmark_rust_voice.py --fixture /tmp/goforward.raw \
  --before-rust /path/to/saved-jarvis-voice --output /tmp/rust-before-after.json
python scripts/check_rust_optimization.py --fixture /tmp/goforward.raw \
  --before /path/to/saved-jarvis-whisper --output /tmp/rust-exact-tensors.json
```

Raw results: [before/after Rust](benchmarks/rust-whisper-optimization.json),
[optimized Whisper](benchmarks/rust-whisper-optimized.json),
[optimized Whisper + GTCRN](benchmarks/rust-whisper-optimized-gtcrn.json),
[optimized complete worker](benchmarks/rust-whisper-optimized-worker.json), and
[exact tensor comparison](benchmarks/rust-whisper-optimized-tensors.json).
The [operator profile](benchmarks/rust-whisper-profile.json) shows the two bottlenecks
and their measured reduction on the clean fixture.

## Initial measurement before kernel optimization

Measured on 2026-10-22, Ryzen 7 7800X3D, Linux x86-64, eight inference
threads. Fresh Python/Rust processes ran sequentially. Values below are RSS
in MiB for the named process, excluding the service, UI and capture child.

| Scope | Python retained | Rust retained | Python peak | Rust peak |
| --- | ---: | ---: | ---: | ---: |
| Whisper only | 449.3 | 392.6 | 720.3 | 564.2 |
| Whisper + Rust GTCRN | 453.4 | 395.3 | 717.5 | 564.3 |
| Complete Vosk + Whisper worker | 599.7 | 557.4 | 832.4 | 648.2 |

Whisper median decode was 436 ms for Python/native and
790 ms for Rust. With denoising, it was
541 and 864 ms. The isolated retained reduction
was about 57–58 MiB; the complete worker retained about 42 MiB less. Startup
peaks are included, rather than reporting only steady allocations.


All 21 paired transcripts matched with denoising disabled, and all 21 matched
with denoising enabled. The complete worker recognized the same three commands
through the real Vosk/Whisper path with paced fake capture. These tests use one
public PocketSphinx utterance under seven perturbations, not a representative
speaker, accent, room-noise or live-microphone accuracy corpus. No desktop
actions were executed.

Before kernel optimization there was a memory/latency tradeoff: lower retained
and startup memory, with slower transcription. The optimized comparison above
supersedes those latency figures. Linux now prefers the Rust worker when its
assets are present; see [worker selection](rust-voice.md#build-and-select).

The intermediate-tensor diagnostic separately compares clean and repeated
long audio with ONNX Runtime 1.31.0, using identical mel inputs and then the
Rust encoder tensors with identical token history. All sampled token argmax
values matched. Intermediate floating-point tensors are **not equivalent**:
for the clean case, relative RMS differences were approximately 4%/8.5% for
encoder key/value outputs and 0.3–2.9% for decoder logits. This is distinct
from the end-to-end transcript comparison against the application's existing
sherpa-onnx 1.13.8 recognizer. Wider accuracy testing remains necessary.

Reproduce the fresh-process comparisons and offline integration tests with
explicitly supplied local assets:

```sh
.venv/bin/python scripts/benchmark_rust_voice.py --fixture /tmp/goforward.raw
.venv/bin/python scripts/benchmark_rust_voice.py --fixture /tmp/goforward.raw --denoise
.venv/bin/python scripts/benchmark_rust_voice.py --fixture /tmp/goforward.raw --full-worker
MURMUR_WHISPER_MODEL=/path/to/sherpa-onnx-whisper-small.en \
MURMUR_WHISPER_DIR=/path/to/sherpa-onnx-whisper-small.en \
MURMUR_VOSK_MODEL=/path/to/vosk-model-small-en-us-0.15 \
MURMUR_GTCRN_MODEL=/path/to/gtcrn_simple.onnx \
MURMUR_SPEECH_TEST_RAW=/tmp/goforward.raw \
  .venv/bin/python -m unittest discover -s tests -v
```

The optional tensor diagnostic needs numpy and onnxruntime in a separate
Python validation environment, and does not install or change application
dependencies:

```sh
python scripts/check_rust_whisper.py --fixture /tmp/goforward.raw \
  --output /tmp/rust-whisper-tensors.json
```

Tests also inspect linked dependencies and the loaded recognizer's process
maps, run it with Python absent from PATH, check repeated-command cache reset,
exercise silence/minimum-length denoised input and bounds, and cover the
service's Rust-worker protocol with action dispatch mocked.

All 203 Python tests passed with the explicit local models and public fixture
enabled, as did all fifteen Rust tests across the three crates. Clippy checks use
`--all-targets -- -D warnings`.

Raw results: [Whisper](benchmarks/rust-whisper.json),
[Whisper + GTCRN](benchmarks/rust-whisper-gtcrn.json),
[complete worker](benchmarks/rust-whisper-worker.json), and
[intermediate tensors](benchmarks/rust-whisper-tensors.json).

Implementation references:

- [sherpa-onnx 1.13.8 Whisper export](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/scripts/whisper/export-onnx.py)
- [Whisper recognition pipeline](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/sherpa-onnx/csrc/offline-recognizer-whisper-impl.h)
- [Greedy decoding](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/sherpa-onnx/csrc/offline-whisper-greedy-search-decoder.cc)
- [kaldi-native-fbank 1.22.3 Whisper features](https://github.com/csukuangfj/kaldi-native-fbank/blob/v1.22.3/kaldi-native-fbank/csrc/whisper-feature.cc)
- [tract 0.23.9](https://docs.rs/tract-onnx/0.23.9/tract_onnx/)
- [Rust runtime CPU feature detection](https://doc.rust-lang.org/std/macro.is_x86_feature_detected.html)
- [Rust packed division](https://doc.rust-lang.org/core/arch/x86_64/fn._mm256_div_ps.html)
- [Rust packed rounding](https://doc.rust-lang.org/core/arch/x86_64/fn._mm256_round_ps.html)
