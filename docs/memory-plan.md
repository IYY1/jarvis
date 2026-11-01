# Memory plan — 2026-10-26

Goal: Jarvis should run comfortably on an ordinary 8 GB Windows 10/11 laptop,
not only on a 32 GB Arch desktop. Today the Linux voice worker alone ranges
from 160 MiB to 750+ MiB, and instant replay adds 270–560 MiB.

Two rules apply throughout:

- **Savings are hypotheses until measured.** Every figure below marked
  "expected" is an estimate. A step is done only when before/after numbers
  exist on the platform it claims.
- **Recognition quality is a hard constraint.** A smaller worker that misses
  commands, or acts on speech it should have ignored, is a regression.

## Current position — 2026-10-27

The implementation foundations and isolated experiments span all six steps;
**the plan is still in validation and architecture selection, with no release
sign-off**. An implementation count would give a misleading completion
percentage. The combined targets remain ≤150 MiB waiting with replay off,
≤300 MiB with replay on and ≤400 MiB during commands, without recognition or
latency regressions.

| Step | Available now | Work still required |
| --- | --- | --- |
| 1. Measure and gate | Process accounting, application sampler, recognition runner and soak runner | Full real acoustic corpus, whole-app workloads and Windows measurements |
| 2. Missed wake | Quiet-boundary reset and repeated fixture recovery | Broader acoustic and Windows acceptance |
| 3. Optional replay (deferred) | Opt-in disk storage, RAM cap, cleanup and synthetic buffer measurements | Strict Linux recorder byte cap; real Linux/Windows capture, quota and failure evidence |
| 4. Retained memory | Vosk history attribution, disabled finalization trial, isolated child ownership and pressure/cache diagnostics | Full live-floor attribution where needed, broader pressure and long-run acceptance |
| 5. Fast path | Disabled exact-agreement experiment with measured savings | Resolve cold-fallback latency regression and pass the full corpus |
| 6. Models and wake | Rust Whisper profiles and isolated KWS/buffered command probes | Resolve detector timing/recognition tradeoff, validate chosen smaller recognizer, then select and integrate the architecture |

The user has set Windows work aside and deferred further optional replay
storage optimization because of continuous SSD writes. The local installation
is back on RAM replay with its existing 120-second HDR/audio settings. Disk
mode remains opt-in. Strict Linux disk quotas remain unpassed and outside the
current critical path; see [local settings](local-memory-activation.md).

The next work focuses on the everyday voice path: waiting memory, command
peaks, wake reliability and latency. Model-derived KWS chunk alignment is
already tested and remains unaccepted; see [the alignment report](kws-alignment.md).
The selected real Jarvis command is also missed by all KWS arms. The subsequent
[fixed PocketSphinx screen](pocketsphinx-wake-screen.md) detects the original
Jarvis tail but fails expanded quiet/interrupted recognition controls. Neither
wake candidate is accepted for integration. [Subsequent screening](wake-candidate-screening.md)
recovers the original PocketSphinx VAD failures, but new public positives still
fail. Standalone tiny wake recognition and the stock dedicated Hey Jarvis
model also fail. The [fixed combined detector](dual-wake-screen.md) is now
rejected: 24/32 fresh Jarvis positives versus Vosk 31/32, with eight paired
regressions. All 54 familiar controls passed. No target changes without an
explicit user answer to the pending 150-versus-200 MiB waiting-budget question. Small.en's command-memory
failure and base.en's reference discrepancy remain open; tiny.en needs broader
acoustic evidence. No production model or allocator change is adopted.

After candidate selection, the remaining work includes integration, GTCRN and
capture interaction, combined budgets, Windows/8 GB pressure, and **three
sequential 24-hour soaks: at least 72 hours of final soak time alone**, plus
preparation, investigation and any reruns. There is no defensible overall ETA
while acoustic/platform evidence and candidate regressions remain unresolved.
Use [the handoff](memory-plan-handoff.md) for exact assets and the next action.

## Where the memory goes today

Linux, Rust controller and worker, measured live and in the benchmarks under
[docs/benchmarks](benchmarks). RSS in MiB.

| Process | Idle | Peak | Notes |
| --- | ---: | ---: | --- |
| Voice worker, waiting for the wake phrase | 156–165 | — | Vosk small model and three Vosk decoders |
| Voice worker, Whisper loaded | 475–695 | 754–803 | Whisper small.en int8 adds ~315; loading adds scratch on top |
| Voice worker after 3 h, Whisper released | 366 | — | floor rose ~8 MiB per load on average; cause open, not load/release itself ([details](whisper-on-demand.md)) |
| Instant replay (gpu-screen-recorder) | 271–334 | 559 | ~110 for the process; the rest is the 120 s RAM buffer |
| Controller | 10 | — | |
| `pw-record` | 9 | — | |

Windows runs the Python service with Vosk and sherpa-onnx Whisper small.en,
and keeps up to 256 MiB of encoded replay in RAM. An earlier review of the
Python worker on Linux measured about 590 MiB PSS idle and 863–945 MiB peak
during commands ([performance](performance.md)). Native Windows buffer
components have now been measured in an 8 GB VM
([validation](memory-plan-windows-validation.md)). Complete Windows application
workloads remain unmeasured, and Linux numbers cannot establish their requirements.

## What went wrong

1. **Two general speech recognizers for a closed set of commands.** Jarvis
   recognizes a known list of phrases, yet runs Vosk's general English model
   with grammars and then Whisper small (244 M parameters) as a second pass.
   Whisper is the single biggest cost. Vosk's free-form decoder is not pure
   duplication: `Matcher::choose` uses its text to reject grammar results
   whose intent disagrees ([matching.rs](../native/voice-worker/src/matching.rs)).
2. **Wake detection uses a full recognizer.** Listening for the wake name
   keeps the whole Vosk model and decoder resident (~150 MiB).
3. **Model weights are private heap copies.** tract parses, quantizes and
   plans the ONNX files in anonymous memory, which raises the loading peak and
   makes unloading depend on the allocator.
4. **Instant replay keeps 120 seconds of very-high-quality video in RAM**, on
   by default. During games that is ~350 MB of buffer.
5. **Windows still ships the Python service**, the heavier implementation, on
   the platform where memory matters most.
6. **A missed-wake bug, found while benchmarking.** After background speech,
   the next wake name can be missed entirely: only ~1.5 s of quiet reaches
   Vosk, which is not enough for its grammar decoder to end an utterance of
   unrecognizable speech, so the wake name never starts a new one.
7. **Smaller leftovers:** Vosk keeps feature history for up to 10 minutes of
   speech; partial hypotheses trigger Whisper loads (27 loads in 3 hours live).

## What others do

| Project | Approach | Lesson, with caveats |
| --- | --- | --- |
| [Home Assistant Speech-to-Phrase](https://www.home-assistant.io/blog/2025/02/13/voice-chapter-9-speech-to-phrase/) ([code](https://github.com/OHF-Voice/speech-to-phrase)) | Compiles known sentences into a Kaldi language model (FST, OpenGRM) with a fuzzy correction layer; under a second per command on a Raspberry Pi 4, versus ~8 s for Whisper there | One grammar-trained recognizer plus fuzzy matching can replace general ASR for fixed commands. Its free-form gaps (timers, shopping lists) mirror Jarvis's app names |
| [Picovoice Rhino](https://picovoice.ai/platform/rhino) and Porcupine | Domain "context" models for speech-to-intent; Porcupine uses about 1 MB of memory on a Pi 3 ([benchmark](https://picovoice.ai/docs/benchmark/wake-word/)) | Shows how small wake and intent can be; proprietary licensing rules it out for Jarvis |
| [openWakeWord](https://github.com/dscripka/openWakeWord) | Shared speech-embedding backbone plus a small classifier per wake word; one Pi 3 core runs 15–20 models | Cheap always-on listening; each wake word needs training |
| [sherpa-onnx keyword spotting](https://k2-fsa.github.io/sherpa/onnx/kws/index.html) | Streaming Zipformer KWS (~3.3 M parameters); keywords configured at run time without retraining | A candidate for configurable wake names. Thresholds trade detection against false alarms, and a ~4 MB model file does not establish the running footprint |
| [sherpa-onnx hotwords](https://k2-fsa.github.io/sherpa/onnx/hotwords/index.html) | Biases a general recognizer toward listed phrases | Not equivalent to a constrained grammar; it can still output other text |
| [Moonshine](https://arxiv.org/pdf/2410.15608) | ASR for short commands: Tiny is 27 M parameters (~26 MB int8 ONNX, [source](https://huggingface.co/moonshine-ai/moonshine/discussions/6)), lower WER than Whisper tiny.en/base.en in the authors' tests | A candidate second pass. Needs an integration: the worker's recognizer is Whisper-specific |
| [Handy](https://handy.computer/docs/faq) | Configurable model unload timeout and a tray "Unload model" action | On-demand loading with user control is common |
| [Steam background recording](https://help.steampowered.com/faqs/view/23B7-49AD-4A28-9590), [NVIDIA instant replay](https://shacknews.com/article/142202/change-shadowplay-save-file-location-nvidia-app?amphtml=1) | Rolling buffer in temporary files on disk with a size limit | Disk buffers are normal for replay |
| [OBS replay buffer](https://obsproject.com/forum/threads/replay-buffer-maximum-memory-supported.167531/) | RAM buffer with a maximum-memory setting | A RAM mode should have an MB cap |

Whisper sizes for reference (whisper.cpp ggml files): small q5_1 181 MiB,
base q5_1 57 MiB, tiny q5_1 31 MiB
([model list](https://huggingface.co/ggerganov/whisper.cpp)).

## Budgets and acceptance criteria

Per-process targets stay, but the release gate is a **combined budget** for
everything Jarvis runs: voice worker, replay, controller, capture helper and
the settings window when open.

| Budget, 8 GB laptop | Target (to confirm in step 1) |
| --- | --- |
| All Jarvis processes, waiting, replay off | ≤ 150 MiB |
| All Jarvis processes, waiting, replay on | ≤ 300 MiB |
| Peak while recognizing a command | ≤ 400 MiB total |
| Growth over a week of normal use | none beyond the soak tolerance |
| Standby CPU | no worse than today (~0.1% of one core) |
| Command latency, median and p95 | no worse than today (~1.0 s / ~1.1 s after a pause) |
| Under memory pressure | keeps working or fails with a clear message; no crash loop |

Measure Windows with Private Working Set and commit charge, Linux with RSS
and PSS. Memory pressure: Linux under a cgroup memory limit; Windows on a
machine or VM with 8 GB and other applications open.

Recognition acceptance suite, required by every step that touches recognition:

- correctly spoken commands (fixtures plus a recorded set of real commands);
- similar commands that must map to the right one, not a neighbour;
- unrelated speech, music and game audio that must not trigger any action;
- repeated background speech → quiet → wake, **without** added noise;
- false actions counted and gated at zero for the fixture set.

Soak criteria (long-run memory):

- discard a warm-up period (first hour, or the first N load cycles);
- fit the RSS floor (value at each wake) over several windows; growth means
  a positive slope beyond a stated tolerance (for example 1 MiB/hour) in each
  of three repeated runs;
- a plateau after warm-up is bounded allocator caching and passes; a slope
  that persists across windows fails.

## Order of work

Steps depend on each other; the order matters.

### Step 1 — baselines and recognition tests

Implementation started 2026-10-26: on-request process memory in status/doctor,
application JSON sampler, an audio acceptance runner with coverage and false
action gates, and a three-run 24-hour soak tool. See
[measurement instructions and remaining evidence](memory-measurement.md).
Step 1 remains open until the full corpus and platform measurements exist;
the isolated Linux baseline is not Windows or live-desktop evidence.

- Whole-application measurement on Linux and Windows, in the units above,
  stored as benchmark JSON. Report per-process memory in `jarvis status` and
  `doctor`, so users and tests can see it.
- Build the recognition acceptance suite above.
- Harden the scripts used as gates: `benchmark_load_cycles.py` now exits
  non-zero when any command differs from the fixture (done 2026-10-26). Add
  the soak criteria to a 24-hour variant.

### Step 2 — fix the missed wake

The quiet-boundary reset is implemented in the Rust engine, with a matching
small fix in the Python fallback. Three fresh paced Rust workers recovered
9/9 fixture commands; the saved worker recovered 2/3. See
[candidate comparison and validation scope](memory-measurement.md#wake-recovery-step-2).
This is not a full recognition-suite or Windows sign-off.

- Baseline reproduction: `scripts/benchmark_load_cycles.py --no-room-noise`
  failed after the first cycle; also test repeated background
  speech → quiet → wake. The default benchmark adds room noise specifically to
  help Vosk end the utterance, which hides the bug; it is a memory benchmark,
  not evidence that wakes work.
- Candidate fixes to compare: feed Vosk enough quiet frames to end the
  utterance, or finalize/reset the wake decoder after a quiet gap.
- Done when the no-noise test passes repeatedly and the unrelated-speech
  tests show no new false wakes.

### Step 3 — optional replay on disk (further optimization deferred)

Implementation added 2026-10-26: explicit RAM/Disk controls, a shared Rust
private-session/segment store, native Linux GSR disk integration and a thin
Windows adapter. Three generated-video buffer comparisons on Linux reduced
retained process-tree RSS from about 266 MiB to 27.5 MiB; this excludes capture,
voice and settings and does not establish Windows savings. Crash cleanup,
rollover/save and failure checks are implemented. Strict Linux byte-quota
acceptance and real Windows/Linux capture measurements remain open: Linux
currently supervises a stop threshold every 250 ms. Linux disk budgets must be
at least 512 MiB (GSR writes 256 MiB files), and Linux RAM mode is bounded by
replay length only, as before this step. See
[measurement details](memory-measurement.md#replay-storage-step-3) and
[privacy/limits](recording.md#replay-storage-and-privacy).

This trades retained RAM for continuous encoded writes. Users who do not use
replay receive no saving; the write cost depends on bitrate and time enabled.
At a constant encoded bitrate of B Mbit/s, logical buffer writes are about
0.45 × B GB/hour, excluding filesystem overhead and save copies. This is a
rate calculation, not an SSD lifetime prediction. RAM remains the default.
Further storage optimization is deferred by the user. Requirements for any
future disk-storage acceptance remain:

- bounded disk usage (time and size caps); cleanup of stale buffer files after
  a crash, at the next start; clear errors and a safe stop when the disk is
  full; tests that save during buffer rollover;
- the buffer directory must be real disk: Linux temporary directories can be
  RAM-backed [tmpfs](https://docs.kernel.org/filesystems/tmpfs.html), which
  would only move the memory (check the filesystem type, refuse tmpfs);
  private permissions;
- **privacy:** README, recording.md and windows.md promise that replay stays in
  RAM until saved. Disk buffering breaks that promise, so it must be an
  explicit, explained setting, and those documents updated. Keep a capped RAM
  mode;
- Linux: gpu-screen-recorder's `-replay-storage disk`. Windows: write the
  one-second segments to the buffer directory instead of RAM.
- Document measured write rates for the tested workload and time enabled; do
  not infer device endurance or typical-user costs from one desktop capture.

### Step 4 — find the per-command growth, then decide allocator or process

- **Measured 2026-10-26: mimalloc not adopted.** In the load-cycle benchmark
  (results below) the floor grew ~3 MiB per cycle with glibc, with mimalloc
  for Rust allocations, and with Whisper kept loaded. The growth is not caused
  by Whisper load/release, so an allocator for Whisper does not address it.
  (That test was Linux GNU only and says nothing about Windows.)
- **Measured 2026-10-26: the benchmark growth is bounded.** Over 32 cycles
  the floor rose ~3 MiB per cycle to ~234 MiB, then fell back by 27–38 MiB
  every ~14 cycles (~600 s of decoded speech): a sawtooth that matches Vosk
  rebuilding a decoder's feature history after 20,000 decoded frames
  ([results](benchmarks/load-cycles-long-2026-10-26.json)). It does not yet
  explain the full live floor of 366 MiB after three hours.
- **Decoder attribution added 2026-10-26.** A Rust offline probe keeps all
  three decoders resident and drives one at a time. Three repeats of each
  reversed-fixture arm show maximum post-trim floor rises of 36.23–36.26 MiB
  for wake, 36.22–36.30 MiB for command and 39.24–41.29 MiB for free decoding,
  with periodic drops. Direct `FinalResult()` at boundaries reduces those
  rises to 0.11–0.17 MiB for the grammar decoders and 3.13–3.55 MiB for free
  decoding. A forward-fixture free-decoder control agrees. These unpaced
  measurements attribute bounded history, not the entire live floor; normal
  recognition reset behavior is unchanged. See
  [method, source and raw results](vosk-decoder-history.md).
- **Engine finalization trial added 2026-10-26, disabled by default.** Three
  paired 32-cycle probes through the real energy gate and matcher reduced the
  maximum floor rise from 44.93–45.21 MiB to 2.68–2.72 MiB, with all 384 probe
  commands correct and no background events. Separate paced workers retained
  Whisper prefetch: nine commands per policy passed, with median/p95 latency
  1145.8/1243.3 ms default and 980.0/1168.7 ms experimental. Whisper peaks
  remained above 500 MiB. See [scope and raw results](vosk-finalization.md).
- Next: complete the acoustic acceptance suite, real workloads and platform
  evidence before adopting finalization. Vosk must be finalized before
  `Result()`/`Reset()` changes its state; a late `FinalResult()` does not
  reclaim the feature pipeline.
- Fallback if growth stays unexplained: a recognizer child process, a strong
  reclamation boundary for its own memory. It needs audio buffering while it
  starts, cancellation, and a measured startup latency. Keep it alive briefly
  after commands instead of assuming a restart on every wake is cheap.

### Step 5 — recognition fast path

Rust experiment added and measured 2026-10-26, **disabled by default**. Exact
whole-command Vosk/free agreement with all word posteriors ≥0.98 avoided every
Whisper load on the public fixture: three fresh workers recognized 9/9 commands,
peaked at 178.73–180.57 MiB RSS versus 548.24–571.79 MiB, and reduced median
latency from 1041.8 to 704.0 ms. However, a separate forced-fallback pair
exposed a cold-load cost: p95 rose from 1017.7 to 2695.8 ms without speculative
prefetch. The full acoustic corpus is also incomplete. This is **not Step 5
sign-off**; the existing default path remains in place. See
[experiment and raw results](vosk-fast-path.md).

- Skip the Whisper pass when Vosk's grammar result is exact and confident.
- Test it **separately** from removing the free-form decoder, which today
  rejects disagreeing grammar results.
- Done when the acceptance suite passes with zero false actions and memory,
  load count and latency improve measurably.

### Step 6 — smaller models and a wake detector, as experiments

Rust tiny.en/base.en export profiles added and measured 2026-10-26; small.en
remains the default. Three fresh paced workers per profile recognized 9/9
fixture commands each. Peak worker RSS was 278.98–282.04 MiB for tiny.en,
329.23–330.81 MiB for base.en and 548.31–570.70 MiB for small.en. Tiny and small
matched all 63 reference transcripts; base matched 45/63, with noisy punctuation
and truncated-speech differences still unresolved. The prior small.en path's
55 diagnostic tensors remain byte-identical. These are Linux fixture results,
not acoustic, Windows or combined-budget acceptance. See
[method, failed base gate and raw evidence](whisper-model-experiments.md).

An isolated Rust KWS probe using native sherpa-onnx/ONNX Runtime peaked at
53.30–54.30 MiB RSS in three paced workers, with 15/15 surrogate wakes and no
extra triggers on selected controls. At the primary threshold 0.25, all 51
focused paced/unpaced cases pass. Higher thresholds miss intended wakes,
and ungated silence costs 1.40–1.42% of one core, above the standby target.
The subsequent opt-in Rust quiet gate reduces silence CPU to 0.013–0.015%
with 33/33 focused wakes and 53.57–54.59 MiB peak RSS, but delays the low-volume
wake by 180 ms. A separate buffered Rust handoff passes 18/18 fixture cases
and retains audio byte for byte through loading. Its combined parent/detector/
command-worker peaks are 228–231 MiB with Vosk/free decoding, 594–617 MiB with
default small.en and 320–326 MiB with explicitly experimental tiny.en. Small
fails the command target even at that reduced scope. A subsequent quiet-endpoint
experiment passes 18 natural commands across repeated detector/load/release
cycles, returning to two processes. Its RMS 0.005 endpoint misses a low-volume
command; a separate RMS 0.001 candidate passes seven focused controls. Ten
tiny.en cycles leave 57.37–57.79 MiB RSS waiting (0.418 MiB growth after the
first cycle) and peak at 333.67 MiB. This short file-only result establishes
neither a stable plateau nor live multi-command handling or soak acceptance.
The later isolated continuous-file probe adds multiple windows with explicit
busy discard, timeout and quiet recovery. A fixed 36-variant diagnosis across
three supplied recordings reproduces every gated trigger chunk by cropping an
ungated stream to the same origin; timing still regresses in 18 variants by up
to 270 ms. New eight-cycle report-retention controls place all observed released
RSS growth in the parent (0.395 MiB retained / 0.207 MiB streamed), with flat
detector RSS but small continuing in-use heap increases. This is attribution,
not leak freedom or a timing fix; see [the diagnostics](kws-continuous-diagnostics.md).
A subsequent separate allocator counter holds detector Rust live bytes/blocks
constant over 128 requests and parent live blocks constant over 32 streamed
cycles, while glibc in-use counts still increase. Three isolated cgroup controls
show two commands at a 384 MiB cap, recovery on a later wake after a worker OOM
at 192 MiB and limit release, and bounded repeated failures under sustained
192 MiB. Both OOM runs retain exit 1 and failed recognition gates. These reduced
file-only controls establish neither native leak freedom nor application or
Windows pressure acceptance; see [allocation and pressure](kws-pressure-allocations.md).
The next controlled comparison adds owned/cancellable child-reader joins and
keeps live thread counts constant. Joining leaves the small glibc trend present;
disabling only the diagnostic malloc cache removes measured in-use growth over
128 detector requests and eight parent cycles, while disabling the stack cache
does not. Parent RSS still warms by 0.121 MiB without the malloc cache. No tunable
is adopted, and no long-run leak-freedom/plateau gate follows from these short
controls; see [reader and cache attribution](kws-reader-caches.md).
A model-derived 320 ms stream-alignment candidate was measured on 2026-10-27
against 288 fixed timing/gain variants. It detects 287 wakes versus 288 for
the old gate and 253 for ungated, without extras. All 253 jointly detected
baseline/aligned wakes are no later and silence CPU remains below target,
but the one lost old-gate wake fails recognition acceptance. Cropping ungated
streams at the measured origins reproduces the miss/hit difference in both
saved and current binaries. No thresholds are tuned or candidate adopted;
see [the alignment evidence](kws-alignment.md).
Custom Jarvis-name positives, Windows, full acoustic acceptance and all
whole-application budget gates remain unverified. Vosk/small.en remain the
defaults. See [original sweep](kws-experiment.md) and
[quiet-gate/handoff methods](kws-quiet-gate.md) and
[cycles, failed endpoint and floor trend](kws-handoff-cycles.md).

- Second pass: compare Moonshine Tiny/Base and Whisper base.en/tiny.en with
  small.en on the acceptance suite (accuracy, false actions, latency, peak
  memory). Moonshine needs a recognizer integration first.
- Wake: trial sherpa-onnx keyword spotting for the configurable wake names.
  Measure its running footprint, tune thresholds against false alarms on the
  unrelated-speech set, and compare with the fixed Vosk wake path.
- Do not treat hotword biasing as a replacement for the command grammar.

### Then — choose the architecture

With steps 1–6 measured, decide among: Vosk grammar plus a small on-demand
second pass; a keyword spotter with an on-demand recognizer; a recognizer
child process; and when to move Windows to the Rust worker and controller.
These interact: a fast path makes loads rare (the allocator matters less), a
smaller model makes a child process cheaper to start, and a keyword spotter
changes where the missed-wake fix lives.

### Deferred

- Memory-mapped model weights: runtime engineering with an uncertain payoff.
  Revisit only if smaller models and fewer loads are not enough.

## Keep Whisper loaded or load on demand?

Keeping Whisper loaded does not stop the growth: in the benchmark below it
grew at the same rate as on-demand, from a floor 300 MiB higher. It saves the
reload CPU and avoids a possible wait on very short commands, but it is over
the budget above. On-demand stays the default; steps 4–6 decide the rest.

## Allocator and resident benchmark

15 live-like cycles per worker (wake plus command, 35 s of speech-like audio,
3 s of room noise), run one after another on Linux.
[Raw results](benchmarks/load-cycles-allocator-2026-10-26.json); method in
[whisper-on-demand](whisper-on-demand.md#load-cycles-allocator-and-resident-whisper).

| Worker | RSS at first → last wake, MiB | Growth per cycle | Peak MiB | Median latency | CPU |
| --- | --- | ---: | ---: | ---: | ---: |
| glibc, periodic trim, on demand (current) | 156.2 → 232.0 | 3.05 MiB | 597.8 | 908.5 ms | 59.6 s |
| mimalloc for Rust allocations, on demand | 158.5 → 231.6 | 2.71 MiB | 638.7 | 908.8 ms | 59.8 s |
| glibc, Whisper kept loaded | 459.8 → 518.1 | 2.74 MiB | 598.7 | 991.9 ms | 36.2 s |
