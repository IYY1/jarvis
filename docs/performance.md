# Current performance

The latest Rust startup and temporary-buffer measurements are in
[model loading memory](rust-memory-loading.md). Since 2026-10-25 the Rust
worker loads Whisper on demand: about 156 MiB while waiting for the wake
phrase, versus 469 MiB with Whisper kept loaded; see
[on-demand Whisper](whisper-on-demand.md). The dated comparisons below
retain their original scope and baselines.

## Catalog and temporary-buffer refactor (2026-10-30)

Application command catalogs now try each display name before looking up its
alternatives. If needed, a temporary reverse index replaces repeated scans of
every alias and fallback. Candidate priority and open/close resolution stay
unchanged; no additional cache survives the call.

Python transcription now stores PCM and denoiser output in compact float32
arrays, pads the owned buffer in place, and releases it before decoding. PCM
scaling is exact for every signed 16-bit value; native denoiser float32 values
also round-trip unchanged. The worker releases source JSON and grammar
scaffolding after constructing its decoders, and shares immutable phrase sets.
Python fuzzy matching filters candidates while iterating instead of copying
the full dictionary twice. Rust matching reuses a temporary row and work stack.

Fresh sequential Python processes compared `a8a0a8a5` with this refactor, using
three process pairs for each synthetic workload. The table reports medians;
allocation peaks exclude fixture setup and do not measure native model memory.

| Workload | Before | After |
| --- | ---: | ---: |
| 100-app catalog, wall time | 6.23 ms | 1.70 ms |
| 1,000-app catalog, wall time | 383.43 ms | 16.83 ms |

Output digests matched in every pair. Audio preparation uses a fake Whisper
stream; grammar retention uses fake Vosk decoders that retain only grammar
hashes. Allocation counters exclude native model memory and fixture setup.

| Python RAM workload | Before | After |
| --- | ---: | ---: |
| One-second audio staging, peak Python bytes | 712,984 | 102,484 |
| Ten-second audio staging, peak Python bytes | 6,635,544 | 714,484 |
| Fuzzy command matching, peak Python bytes | 17,028 | 5,426 |
| 128-app grammar scaffolding, retained Python bytes | 760,870 | 475,806 |
| 2,048-app grammar scaffolding, retained Python bytes | 4,594,682 | 2,581,816 |

The larger grammar includes 256 custom launch words and eight monitor names;
its compiled decoder grammar hashes and 12,747 matching candidates are unchanged.
Audio waveform hashes matched for one- and ten-second inputs with GTCRN off/on,
and installed Whisper produced the same synthetic-input transcripts. A separate
real-GTCRN comparison against the preceding in-place-list pass reduced ten-second
staging peaks from 11,523,728 to 6,424,040 Python bytes. Compact buffers trade
some preparation speed for RAM: ten-second staging rose from 5.56 to 8.02 ms
without denoising. The GTCRN sample measured 296.45 to 315.90 ms. These small
samples exclude Whisper decoding and do not establish complete command latency.

The isolated Rust matching recurrence allocated far less temporary storage
across repeated comparisons (51.36 MB to 0.27 MB cumulatively). Its temporary
peak increased from 344 to 592 bytes because it reuses capacity within a pass.
Complete `--match` timing and RSS measurements did **not** establish an
improvement; JSON parsing and candidate construction are included there.
Neither benchmark establishes a reduction in whole-app RSS or speech latency.
[Raw measurements and scope](benchmarks/code-refactor-2026-10-30.json).

A separate native cast-buffer experiment was rejected: although its tensors
matched, denoising peak RSS rose from 405.26 to 419.16 MiB and the full worker
from 538.27 to 551.43 MiB. That implementation is absent from this change.

Regression coverage checks all 65,536 signed PCM values, denoiser float32
preservation, release before decoding, grammar ownership, command resolution,
and Python/Rust policy parity. The Rust recurrence matched an independent
reference for 16,384 input pairs, including repeated characters and Unicode.
The final model-backed suite passed 384 Python tests with 14 optional/platform
skips and no failures, selecting the rebuilt Rust worker. All 16 Rust worker
tests and Clippy with warnings denied also passed.

Reproduce catalog and one-second preparation measurements with the current
benchmark script, supplying either checkout's Python source directory:

```sh
.venv/bin/python scripts/benchmark_refactor.py --source /path/to/checkout/src --case app_catalog_1000
.venv/bin/python scripts/benchmark_refactor.py --source /path/to/checkout/src --case transcribe_input
.venv/bin/python scripts/benchmark_refactor.py --source /path/to/checkout/src --case transcribe_input_10s
.venv/bin/python scripts/benchmark_refactor.py --source /path/to/checkout/src --case match_fuzzy
```

## Completed code review

Measured locally on 2026-10-21, AMD Ryzen 7 7800X3D, 16 logical CPUs,
Python 3.14.8. Voice recognition uses Vosk plus int8 Whisper small.en,
eight inference threads and CPU execution. The GTK client is separate.

CPU percentages below use **one logical CPU = 100%**. Divide by 16 for this
machine's total CPU capacity. RSS sums can count shared pages more than once;
PSS apportions shared pages. Short samples are engineering observations.

Claude's final review compared the original `f6713aa` tree with `0b19217` on
2026-10-21. A fake microphone replayed the public `goforward.raw` recording and
all actions were dry-run. The following values come from that completed review;
the publication pass independently reran the tests, not these performance
benchmarks.

| Measurement | Original | Reviewed |
| --- | ---: | ---: |
| Command after a pause: mean of four run medians, 20 commands per run | 1,054.5 ms | 1,045.0 ms |
| Command after a pause: mean of four run p95 values | 1,073.0 ms | 1,061.1 ms |
| Voice worker peak memory during commands | 890–945 MiB | 863–879 MiB |
| Isolated Whisper median decode | 304.8 ms | 286.0 ms |
| Isolated Whisper peak RSS | 767–772 MiB | 719–730 MiB |
| Silent standby CPU, median of four 45-second samples | 0.1125% of one core | 0.0889% of one core |
| Service start to socket, median of 80 samples | 87.6 ms | 87.7 ms |
| One-breath command median, 12 repetitions | 664.1 ms | 909.4 ms |
| One-breath command p95 | 664.3 ms | 1,012.3 ms |

Every paused command dispatched in both trees. The saved isolated-transcriber
results matched in all 70 paired transcriptions across repeated variations of
the same recording. Idle PSS stayed around 589 MiB and wake-ready time around
1.13 seconds. The headless UI first-paint measurement changed from 123 to 86 ms.

One-breath commands now receive the same Whisper cross-check as commands spoken
after a pause. That fixes a wrong-application dispatch at the cost of about
245 ms median latency. This is a deliberate correctness tradeoff; the review
does not establish zero regression. The reviewed wheel grew by 1,731 bytes and
the source archive by 219 bytes before publication documentation was updated.

These small fixture runs do not measure population p95 latency, accuracy across
speakers, room noise, or gaming FPS. The review's temporary benchmark harness is
not distributed in this repository; the existing reproduction commands below
cover the earlier allocator comparison. In hands-free mode, Listen once and
Cancel still restart the voice worker and reload its models (about 1.15 seconds
in the review). Reusing that worker remains a separate follow-up.

## Earlier live background samples

The existing service, voice worker and `pw-record` used **0.18 CPU seconds over
30 seconds**: **0.6% of one logical CPU**, or **0.0375% of machine capacity**.
The daemon recorded zero CPU ticks in that interval; almost all work was the
voice worker. The microphone was already active. No audio or transcript files
were created. Natural room conditions were not recorded or acoustically scored.
See [raw sample](benchmarks/live-standby-cpu.json).

A final sample after thread-pool tuning used **0.06 CPU seconds over 30 seconds**:
**0.2% of one logical CPU / 0.0125% of machine capacity**, ending in Idle with
the microphone active. [Final raw sample](benchmarks/live-standby-cpu-final.json).
Room conditions were not controlled across those two samples; their difference
is not presented as a measured standby speedup. The controlled inference
comparison below supports the CPU optimization.

After successful live commands with no Jarvis window open, the process tree
used **657.1 MiB RSS / 628.9 MiB PSS**, compared with **1,089.6 MiB RSS /
1,050.7 MiB PSS** before allocator tuning. The histories differ; the controlled
comparison below isolates the effect. The live user confirmed Steam opened
from the background. [Background investigation](memory-background.md).

## Earlier allocator comparison

Fresh processes, identical model files, eight threads, identical public fixture
variants, seven cases repeated three times. The baseline restores original ONNX
CPU arena allocation and spinning. The optimized path frees scratch buffers and
lets the native worker threads sleep between tasks. Both remain resident.

| Isolated Whisper process | Original settings | Optimized settings |
| --- | ---: | ---: |
| CPU work across 21 transcriptions and 0.2 s settling per case | 53.50 CPU s | 25.77 CPU s |
| Median transcription wall time | 338.3 ms | 348.3 ms |
| Retained RSS after all cases | 953.7 MiB | 455.5 MiB |
| Peak RSS | 984.6 MiB | 741.2 MiB |

All 21 paired transcripts matched exactly. Total CPU work fell about **52%**;
median wall time increased about **10 ms**. These are the same utterance with
controlled variations, not 21 different speakers or an accuracy corpus.
Transient peak memory remains higher than idle memory. The measured CPU
includes a bounded 0.2-second post-decode interval to count any spinning.
[Raw results](benchmarks/cpu-memory-benchmark.json).

An isolated comparison retaining the memory optimization but disabling only
spinning reduced CPU from 51.14 to 25.92 CPU seconds, with a 13 ms median wall
time increase. Four sleeping threads used 19.36 CPU seconds but increased median
decode time from 339.7 to 376.0 ms. The faster eight-thread default was retained.
[Thread comparison](benchmarks/thread-comparison.json).

ONNX documents spinning as a latency/CPU tradeoff in its
[thread management reference](https://onnxruntime.ai/docs/performance/tune-performance/threading.html#thread-spinning-behavior).
The configuration is passed through the
[pinned sherpa-onnx provider parser](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/sherpa-onnx/csrc/session.cc).

## Reproduce

Install `.[benchmark]` for psutil; it is not imported by the application service.
Use the [public fixture instructions](speech-research.md), then run each case in
a fresh process:

```sh
.venv/bin/python scripts/benchmark_transcriber.py --fixture /tmp/goforward.raw --baseline
.venv/bin/python scripts/benchmark_transcriber.py --fixture /tmp/goforward.raw
.venv/bin/python scripts/benchmark_transcriber.py --fixture /tmp/goforward.raw --threads 4
.venv/bin/python scripts/benchmark_service.py --mode paused --seconds 30
.venv/bin/python scripts/benchmark_service.py --mode wake --seconds 30 --live-microphone
```

The last command starts an isolated service with temporary configuration and
actions disabled. Its live-input flag deliberately opens a microphone; use it
only when wanted. It does not record input. UI and recording benchmarks have
separate costs and explicit opt-ins. Avoid parallel inference benchmarks when
comparing timing.

## Practical limits

The [voice-worker memory audit](rust-worker-memory.md) targets the largest
Jarvis process. Lossless embedding storage cut the controlled complete Rust
worker's retained RSS from **558.88 to 482.98 MiB**, with the same model values
and identical fixture commands. [Raw comparison](benchmarks/rust-embedding-worker.json).
This supersedes older Rust worker memory figures for the new build; it does
not change the screen recorder's RAM buffers.

The optional [Rust controller](rust-controller.md) has a separate
[controller-only comparison](benchmarks/rust-controller.json). It measures the
background service with voice/replay off and excludes the UI, speech models and
video buffers. Those memory savings must not be interpreted as a percentage
reduction in the full running application.

The [Rust CLI comparison](rust-cli.md#verification-and-memory) measures the
persistent offline Waybar client separately: median RSS was **16.16 MiB for
Python versus 3.56 MiB for Rust** across three process pairs. This is about
78% less memory for that indicator process; it does not change model or replay
buffer memory. [Raw samples](benchmarks/rust-cli.json) include PSS and timings.

Whisper has a brief CPU burst per command; low standby CPU does not imply zero
interference during inference. No controlled game FPS/frame-time comparison has
been performed, so no FPS claim is made. That acceptance check remains open.
CPU screen encoding may dominate both voice and UI cost; GPU media encoding has
different driver, permission and resource constraints.

The earlier screen/encoder measurements and initial PocketSphinx standby results
are preserved in [historical performance](history/performance-2026-10-20.md).
They are not current Vosk + Whisper resource figures. Long-run false wakes,
suspend/resume, representative game audio, p95 command latency, long recordings,
disk-full recovery and fractional scaling still need acceptance testing.
