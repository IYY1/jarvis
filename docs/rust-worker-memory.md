# Voice-worker memory reduction

For the subsequent startup and temporary-buffer changes, see
[incremental model loading](rust-memory-loading.md). The measurements below
describe the earlier embedding compaction pass.

The host process audit found Firefox leading raw RSS at **772.31 MiB**. Jarvis's
voice worker used **689.76 MiB RSS / 685.22 MiB PSS / 685.15 MiB private memory**.
It was the largest Jarvis process and the largest individual PSS/private-memory
consumer in this snapshot. Firefox, ChatGPT and Discord each have additional
processes; this is a process ranking, not a total-application ranking.

| Process in the snapshot | RSS MiB | PSS MiB |
| --- | ---: | ---: |
| Firefox main process | 772.31 | 540.00 |
| ChatGPT process | 768.08 | 642.16 |
| Firefox content process | 706.89 | 578.90 |
| Discord process | 700.81 | 627.93 |
| Jarvis voice worker | 689.76 | 685.22 |
| Jarvis's screen recorder | 372.77 | 314.34 |
| Jarvis controller | 6.91 | 4.62 |

RSS counts shared resident pages in each process. PSS apportions those shared
pages; private memory counts pages belonging only to that process. The
[audit](benchmarks/rust-worker-memory-audit.json) is a live snapshot and is not
a before/after benchmark. Unrelated desktop applications were not modified.

## What changed

The largest Jarvis process was already the Rust voice worker. Its Whisper
recognizer retained a **151.95 MiB** token-embedding table as float32 values.
All **39,831,552 values** in the installed model round-trip through binary16
with exactly the same float32 bits. The Rust implementation now stores this
table in **75.97 MiB** and expands values back to float32 as they are used.
The ONNX model files and numerical weight values are unchanged.

The loader checks every value before enabling compact storage. Any nonfinite
or nonrepresentable value retains the original float32 table. Lookup,
position addition, projection and eight-lane accumulation use float32
arithmetic. On x86-64 with AVX/F16C, unaligned eight-value loads expand the
weights directly into float32 SIMD registers; multiply and add remain separate
operations. Other CPUs use the scalar conversion path. No expanded copy of
the full table is retained during recognition.

This reduces a model allocation inside the existing Rust recognizer. It does
not rewrite Firefox or replace the screen encoder, wake detector, or trained
speech model. A language rewrite of a component already running native code
would not itself remove the model's weight storage.

## Verification

Three new Rust tests cover lossless/signed-zero lookup, fallback for nonexact
values, and portable/SIMD/parallel projection equality against float32. The
projection checks exercise every finite binary16 encoding and unaligned rows.
An existing real-model integration test now checks the reported compact-table
format and **79,663,104-byte** size.

The strict [tensor comparison](benchmarks/rust-embedding-tensors.json) compares
the saved pre-change executable with the new one. All **55 mel, encoder and
sampled decoder-logit tensor pairs** were byte-identical across seven public
fixture variants. Token history and transcripts also matched. This is stronger
than transcript equality for those fixtures, but is not a broad recognition
accuracy study.

The benchmark runs fresh sequential old/new processes with identical model
files and thread counts. Isolated transcription uses seven fixture variants,
each repeated three times. The complete worker uses Vosk, Whisper and GTCRN
with a paced fake microphone and three surrogate wake/command utterances.
It never executes desktop actions or opens a real audio input. Raw comparisons:
[Whisper](benchmarks/rust-embedding-whisper.json),
[Whisper with GTCRN](benchmarks/rust-embedding-denoise.json), and
[complete worker](benchmarks/rust-embedding-worker.json).

| Controlled old/new Rust comparison | Before | After |
| --- | ---: | ---: |
| Whisper retained RSS | 392.44 MiB | 316.05 MiB |
| Whisper + GTCRN retained RSS | 395.62 MiB | 319.38 MiB |
| Complete worker retained RSS | 558.88 MiB | 482.98 MiB |
| Complete worker peak RSS | 649.18 MiB | 576.93 MiB |
| Whisper median decode | 314.8 ms | 293.1 ms |
| Whisper + GTCRN median decode | 387.6 ms | 371.9 ms |

The complete worker retained **75.90 MiB less RSS (13.6%)**. All 21 paired
transcripts matched with denoising off and on, and all three complete-worker
commands matched. The small latency differences do not establish a speed
change. Startup-to-ready time was approximately 1.88 versus 1.97 seconds in
the complete-worker fixture, which uses only a few application names.

The old/new live worker has a different lifetime and audio history, so a live
RSS change alone cannot establish the size of this optimization. Model storage
savings are exact; process RSS also depends on allocator layout, decoder state,
and scratch allocations. CPU/latency observations cover this machine and the
public fixture. Scalar-path speed on other hardware remains unmeasured.

The final build passed all **244 Python regression/integration tests** with the
existing local speech models and deliberately supplied public fixture. All
**18 Rust voice/recognizer/denoiser tests** passed, including the three new
embedding tests. Clippy passed for the worker and standalone recognizer with
warnings denied. Packaging includes the new source and omits compiled binaries
from portable wheels.

The [live activation](benchmarks/rust-embedding-live.json) replaced the running
worker through Mute/Resume after validation. Voice listening resumed with the
new executable; controller and recorder PIDs, effective settings and the
settings file were unchanged. GTCRN and instant replay remained enabled.
The old long-running worker used **707.48 MiB RSS** immediately before reload;
the fresh optimized worker used **463.90 MiB RSS** after becoming ready.
That larger live difference also clears accumulated decoder/allocator state.
Use the **75.90 MiB controlled reduction** to attribute savings to this change.

## Reproduce

Save the existing executables before building a new version, then run:

```sh
cp src/jarvis/bin/jarvis-voice /tmp/jarvis-voice-before
cp src/jarvis/bin/jarvis-whisper /tmp/jarvis-whisper-before
.venv/bin/python scripts/build_voice_worker.py --test
.venv/bin/python scripts/check_rust_optimization.py \
  --fixture /path/to/public.s16le --before /tmp/jarvis-whisper-before \
  --after native/whisper-recognizer/target/release/jarvis-whisper
.venv/bin/python scripts/benchmark_rust_voice.py \
  --fixture /path/to/public.s16le --before-rust /tmp/jarvis-voice-before \
  --after-rust native/voice-worker/target/release/jarvis-voice --denoise
.venv/bin/python scripts/benchmark_rust_voice.py \
  --fixture /path/to/public.s16le --before-rust /tmp/jarvis-voice-before \
  --after-rust native/voice-worker/target/release/jarvis-voice --denoise --full-worker
```

`--after-rust` measures a candidate build before installing it into the checkout.
Keep benchmarks sequential to avoid inference workloads competing for CPU.

Implementation references: [half binary16 storage/conversion](https://docs.rs/half/latest/half/struct.f16.html)
and [Rust F16C expansion](https://doc.rust-lang.org/core/arch/x86_64/fn._mm256_cvtph_ps.html).
