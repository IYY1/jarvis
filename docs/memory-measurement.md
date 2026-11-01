# Memory-plan measurement gates

Step 1 of [the memory plan](memory-plan.md) is in progress. The reporting and
gate tools below are implemented. Windows hardware measurements, a complete
recorded recognition corpus, replay-on/settings-open baselines, command peak
baselines, live CPU/latency comparisons and memory-pressure tests remain pending.
The 24-hour soak runner has not been run for 24 hours.

## Read the current process memory

After building/restarting the updated service, `jarvis status` includes a
top-level `pid` and `memory` object. `jarvis doctor` includes the same service
snapshot, or an explicit unavailable result when the service cannot be reached.
Neither command starts the service or opens a microphone for this measurement.
Memory collection happens only on explicit status requests; regular status
publications and the watch stream do not collect memory.

All counters are **bytes**, per process and in `memory.totals`:

| Platform | Counters | Collection |
| --- | --- | --- |
| Linux | `rss_bytes`, `pss_bytes` | `/proc/PID/smaps_rollup` |
| Windows | `private_working_set_bytes`, `commit_bytes` | `K32GetProcessMemoryInfo`, legacy `K32QueryWorkingSet` fallback |

RSS sums can count shared pages in multiple processes; PSS apportions them.
Windows private commit is not private working set. The latter uses
[`PROCESS_MEMORY_COUNTERS_EX2`](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters_ex2),
which requires Windows 10/11 22H2 with the September 2023 update. Earlier builds
use [`QueryWorkingSet`](https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-queryworkingset)
to count resident pages whose [Shared bit is clear](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-psapi_working_set_block),
and [`PROCESS_MEMORY_COUNTERS_EX`](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters_ex)
for commit. The fallback needs query/read access and permits four attempts and
at most a 16 MiB page-list buffer. Denial, repeated growth or an oversized list
leaves private working set unavailable; it does not trim the target. A failed or inaccessible
counter makes its total `null` and `complete` false. It never silently counts
as zero. Native Windows tests are included in the Windows CI job. The
[8 GB KVM validation](memory-plan-windows-validation.md) now records actual
Windows execution, legacy-counter checks and component buffer measurements.

The status snapshot covers the service and descendants found in the OS process
inventory: workers, capture children, replay, and any settings process started
as its child. A settings window launched separately is outside that tree.
Include it explicitly in application benchmarks. These snapshots do not
identify independently launched windows by their executable name, and cannot
see processes hidden by the host's process namespace or permissions.

## Whole-application sampler

Start the desired workload, then run:

```sh
.venv/bin/python scripts/benchmark_application_memory.py \
  --scenario waiting-replay-off --settings closed --seconds 30 \
  --output docs/benchmarks/application-idle.local.json
```

The script gets the root PID from the updated service. `--pid PID` overrides
it, and repeatable `--extra-pid PID` adds roots, including a separately
launched settings window. Overlapping roots are counted once. On Windows use
the installed Python interpreter to run the same script. Declare `--settings
included` when the open settings process is covered. Scenario and coverage
labels are caller declarations, so inspect the per-process rows before using
an artifact as release evidence.

Use `--scenario waiting-replay-on` for replay and `--scenario command` while
issuing commands. `--check-budget` enforces the plan's 150/300/400 MiB targets
against the **largest simultaneous sampled total**, using RSS on Linux and
private working set on Windows. Incomplete measurements exit non-zero even
without budget enforcement. The JSON also retains PSS/commit totals, every
sample, the interval, duration, scenario and settings declaration. Sampling
can miss peaks between samples; this is not an OS high-water measurement.
The benchmark process itself is outside the measured roots.

The initial [isolated Linux result](benchmarks/application-memory-isolated-2026-10-26.json)
measured a real Rust controller and voice worker with the installed models,
feeding silence through a Python capture substitute. Replay was off and
settings closed. Over 10 seconds the simultaneous total was **176.84 MiB RSS /
154.55 MiB PSS**, above the 150 MiB RSS target. This validates accounting and
establishes an isolated baseline; it does not replace live PipeWire, replay,
settings, command, or 8 GB Windows measurements. No memory saving is claimed.

## Recognition acceptance

`scripts/benchmark_recognition_acceptance.py` runs the real Linux Rust worker
with a paced fake `pw-record`. Only explicit local PCM is played; it never
downloads audio, opens the real microphone, or dispatches recognized actions.
Supply mono 16 kHz signed 16-bit little-endian raw PCM. Each case uses a fresh
worker; repetitions within a case share that worker and its decoder history.

A manifest has `config`, optional `custom_phrases`/`apps`, and `cases`:

```json
{
  "config": {"activation": {"wake_phrase": "go forward"}},
  "custom_phrases": {"ten meters": "start recording"},
  "cases": [{
    "name": "public-command",
    "category": "command",
    "segments": [{"pcm": "/path/to/goforward.raw"}, {"silence": 2}],
    "repeat": 3,
    "expected_commands": ["ten meters", "ten meters", "ten meters"],
    "expected_wakes": 3
  }]
}
```

PCM paths are relative to the manifest, or absolute. A PCM segment can set
`reverse: true` for a synthetic speech-like stressor. Time-reversed speech
does not stand in for real unrelated speech, music or game audio. Silence is
literal zeros; the runner does not add room noise. For the missed-wake case,
repeat wake+command, 1.5 seconds quiet, background speech, then quiet again.
Expected commands and wakes describe the entire repeated case.

```sh
.venv/bin/python scripts/benchmark_recognition_acceptance.py \
  --manifest /path/to/corpus.json \
  --output docs/benchmarks/recognition.local.json
```

The required categories are `command`, `recorded_command`, `confusable`,
`unrelated_speech`, `music`, `game_audio`, and `background_wake`. The one-case
example is a smoke test and **fails the overall gate** until all categories
exist and pass. Corpus provenance and category labels still need human review.
For negative audio expect no commands and no wakes. Wrong or extra command
events count as false actions; missed commands and extra wakes also fail.
Artifacts retain expected/actual text, counts, errors, clip hashes, worker
hash, events and process-tree memory samples, including failed cases.

The initial [smoke result](benchmarks/recognition-gate-smoke-2026-10-26.json)
uses only the existing public PocketSphinx fixture and its reversed waveform.
The standalone command passed. The three-repeat no-noise case produced only
two wakes and two commands, with zero false actions, so the gate failed and
preserved the evidence. The artifact embeds its original manifest for
reproduction. This is incomplete corpus coverage, not a recognition-quality
sign-off. The Step 2 implementation and focused checks are described below.

## Wake recovery (Step 2)

The Rust engine now discards an unfinished wake utterance when its existing
40-frame quiet tail ends (1.2 seconds). Previously the energy gate stopped
feeding silence while Vosk could still be waiting to finish background speech.
The next wake then joined that stale utterance. The Python Vosk fallback uses
the same boundary rule. Command sessions retain their existing behavior.

The reset does not force a final hypothesis into a wake. It runs at the quiet
boundary; subsequent idle frames remain gated and keep only the existing
300 ms pre-roll. Fake-recognizer tests check the boundary, continued idle
gating, and preservation of an armed command. A real-model regression failed
before the change and passed after it.

An [offline Python Vosk probe](benchmarks/wake-gap-candidates-2026-10-26.json)
compared three fresh engines per candidate, each receiving three wake+command
utterances separated by one reversed fixture and literal quiet:

| Candidate | Commands per trial | Wake-decoder audio per trial |
| --- | --- | --- |
| Original, 1.2-second quiet tail | 1 / 3 in each trial | 19.77 seconds |
| Extend the quiet tail to 3 seconds | 3 / 3 in each trial | 23.85 seconds |
| Keep the tail and reset at its boundary | 3 / 3 in each trial | 18.69 seconds |

The chosen reset recovered the fixture without the longer decoder workload.
This probe excludes Whisper, real-time capture, controller and Windows; its
CPU samples are not a standby CPU or application latency measurement.

The [paced Rust comparison](benchmarks/wake-recovery-repeats-2026-10-26.json)
uses real Vosk, Whisper and GTCRN, with a fake microphone and no actions:
the saved worker recognized 2 of 3 commands, and three fresh patched workers
recognized **9 of 9**, with zero false actions or extra wakes. The original
[long-background smoke case after the fix](benchmarks/recognition-wake-fixed-2026-10-26.json)
also recognized all 3 commands, compared with 2 before the fix.

The [before controls](benchmarks/wake-controls-before-2026-10-26.json) and
[after controls](benchmarks/wake-controls-after-2026-10-26.json) both passed:
three repetitions of the natural spoken command tail without a wake, three
repetitions of reversed speech, a two-second pause between wake and command,
and a 300 ms pause inside the two-word wake name. Negative cases produced
zero wakes/actions; positive cases produced exactly their expected results.
All clips derive from the same public recording, so these controls do not
replace a conversation/music/game-audio corpus.

The original [no-room-noise load-cycle reproducer](benchmarks/load-cycles-wake-fixed-2026-10-26.json)
now completes all three cycles with identical fixture command text. Worker
RSS at the three wakes was 158.20, 194.16 and 198.83 MiB; the worker-only
high-water mark was 556.90 MiB. These short-run observations do not establish
a memory-growth slope or meet the combined 400 MiB command target.

```sh
.venv/bin/python scripts/benchmark_load_cycles.py \
  --fixture /path/to/goforward.raw \
  --arm fixed=src/murmur/bin/jarvis-voice --cycles 3 --no-room-noise \
  --output docs/benchmarks/load-cycles-wake.local.json
```

A separate [three-command before/after latency sample](benchmarks/wake-latency-2026-10-26.json)
used the same settings and waveform in fresh sequential workers:

| Metric | Before | After |
| --- | ---: | ---: |
| Median end-of-speech to command | 994.5 ms | 982.6 ms |
| Nearest-rank p95 (maximum of three) | 1108.4 ms | 1142.8 ms |

Each sample includes one cold Whisper load and two warm commands. The higher
after p95 means this small sample does **not** establish the plan's latency
non-regression criterion. Larger representative latency measurements remain
required; the improved median is not a latency sign-off.

Linux verification passed: the offline Python suite ran 331 tests (28
skipped), all six real native-worker fixture tests passed, the Python Vosk
upgrade tests passed with the local model/fixture, and all 11 voice-worker
Rust unit tests passed. The checkout-local worker was rebuilt. Windows CI now
also runs the fake-recognizer boundary tests; native Windows execution has
not been verified here.

These are focused fixture results. The full acceptance runner still reports
`passed: false` because recorded commands across speakers, similar commands,
real unrelated conversation, music and game audio are not all covered.
Native Windows validation, latency sign-off and the 24-hour campaign remain pending. No
whole-application memory saving is attributed to this wake fix.

## Replay storage (Step 3)

The runtime storage policy is in the shared Rust `native/replay-store` crate.
Linux links its private session/lock/filesystem checks into the controller;
Windows uses its small executable through a Python pipe adapter. No replay or
service was started against a live screen or microphone during this work.

The opt-in setting, privacy warning, directory choices and limits are described
in [recording](recording.md#replay-storage-and-privacy) and [Windows setup](windows.md).
RAM remains the default. Windows reserves an encoded-byte budget before writing
and includes pending and save-pinned files. Linux GSR owns its live files and
has no total-byte cap, so the controller supervises a stop threshold every
250 ms; **strict Linux quota acceptance remains open**. Because GSR fills a
256 MiB file before it can remove one, Linux disk mode needs a budget of at
least 512 MiB and refuses smaller ones at start. Linux RAM mode has no byte
budget: an earlier recorder-RSS stop at the 256 MiB default sat below the
recorder's measured live RSS and was removed on 2026-10-27. A reached limit, low
space or reported buffer-write failure safely stops the owned capture instead
of restarting it. Saved clips are outside the rolling-buffer budget.

### Generated-video comparison

The [three fresh RAM/disk pairs](benchmarks/replay-storage-synthetic-2026-10-26.json)
feed the Windows buffer classes on Linux with **180 distinct copies** of one
generated one-second Matroska segment, retain 120 seconds, and exercise rollover
with a 256 MiB budget. Each segment is 2,057,118 bytes; retained video is
235.42 MiB. Distinct bytes avoid falsely sharing one fixture object throughout
the RAM ring. Disk mode includes the Rust helper in the process tree.

| Retained process-tree memory | RAM | Rust disk store |
| --- | --- | --- |
| Linux RSS, three runs | 265.70–265.82 MiB | 27.38–27.57 MiB |
| Linux PSS, three runs | 256.36–256.38 MiB | 15.95–16.07 MiB |

This demonstrates about **238 MiB less resident process memory in this buffer
layer**. It excludes encoders, named-pipe capture readers, voice, controller and
settings. File cache is reclaimable OS memory outside process RSS/PSS. Filling
was accelerated, with samples every ten appends; brief peaks can be missed.
It establishes neither sustained gaming/SSD throughput nor whole-app/8 GB
Windows acceptance. No actual GSR disk-memory saving is claimed.

Reproduce without capture:

```sh
python scripts/build_replay_store.py --test
mkdir -p build/replay-storage
ffmpeg -v error -nostdin -f lavfi -i testsrc2=size=1280x720:rate=30 \
  -t 1 -c:v libx264 -preset ultrafast -qp 0 -threads 2 -pix_fmt yuv420p \
  build/replay-storage/generated-second.mkv
python scripts/benchmark_replay_storage.py \
  --fixture build/replay-storage/generated-second.mkv \
  --output docs/benchmarks/replay-storage.local.json
```

The fixture hash and helper hash are in the JSON. Codec-library updates can
produce a different fixture; compare each RAM/disk pair with the same file.
Use a real disk for `--work-dir`, not a RAM-backed temporary directory.

### Verification and remaining checks

The [validation record](benchmarks/replay-storage-validation-2026-10-26.json)
records binary hashes and the test scope: the full offline suite passed
319 tests with 29 skips; the later RAM-budget/lifecycle check passed 22 tests;
Wine passed 41 Windows store/policy tests with three platform skips. The final
Linux store tests passed 13 with one Windows-only skip. Native Rust tests
passed (store 3, controller 6, CLI 9), and the store passed Clippy with warnings
denied. Both settings forms loaded and serialized RAM/Disk controls on a
private display, without connecting to the service. The source package builds
with the new crate and its notices, excluding compiled targets.

Tests cover out-of-order one-second segments, time/byte eviction, pending and
save-pinned accounting, save during rollover, Stop during a pinned save,
warm-up saves, exclusive active leases, marked crash cleanup, tmpfs/symlink/
permission refusal, failed OS writes and partial cleanup. Native Rust tests
inject `StorageFull` on write and flush. A Linux integration injects a real
OS file-write error using a process file-size limit; this is not a physically
full-disk test. Fake GSR integration covers size/write-error stops, private
buffers, crash recovery and immutable active storage settings. Live validation
later exposed two assumptions the initial fake did not model: GSR saves beside
`-o`, and omitted save duration includes the entire backing buffer. The
corrected fixture and controller now cover persistent clip publication,
collision handling, cross-filesystem copying, save/cleanup races and explicit
default/custom duration requests. See [live replay validation](replay-live-validation.md).

The Windows x64 helper was cross-compiled with Rust 1.99 and the existing
LLVM-MinGW toolchain, using static runtime linkage. Headless Wine checks cover
protected file/directory ACLs, locking, rollover/remux/full decode and the real
Windows named-pipe → Rust disk store → save/stop flow with generated video.
Wine needed its C: test disk because volume mount queries on the Z: mapped
checkout are incomplete. This does not replace native Windows hardware checks.
The Windows CI job builds/tests the helper with its native Rust toolchain.

Native Windows Private Working Set/commit comparisons, actual Linux GSR
capture comparisons, suspend/crash behavior on real disks, physically full
disk and memory-pressure runs, strict Linux quota enforcement and whole-app
budget sign-off remain pending. Recognition code is unchanged by Step 3.

## Decoder history and fast-path experiments (Steps 4–5)

The [Rust decoder-history probe](vosk-decoder-history.md) attributes bounded
history separately to the wake, command and free-form decoders. Direct
finalization reduces the isolated floor rises, but production reset behavior
remains unchanged pending complete acoustic and workload validation.

A separate [default-disabled Engine finalization trial](vosk-finalization.md)
now applies finalization at the actual boundaries. Three pairs reduced maximum
Vosk-only probe floor rises from 44.93–45.21 MiB to 2.68–2.72 MiB. All 384 probe
commands and 18 paced commands matched expectations, with paired fixture
median/p95 latency gates passing. This retains Whisper prefetch and differs
from the fast-path experiment below. The full corpus and live/platform gates
remain open; command peaks still exceeded 500 MiB.

The [Rust Vosk fast-path experiment](vosk-fast-path.md) retains the free decoder
and can avoid Whisper loads for exact agreement with complete word confidence.
Three paired runs reduced fixture-only worker peaks to 178.73–180.57 MiB,
but the forced fallback p95 rose to 2695.8 ms. The experiment is disabled by
default. Neither set of measurements establishes the combined application
budget, Windows behavior, complete recognition acceptance or long-run soak.

## Smaller Whisper profiles (Step 6)

The [Rust smaller-model experiment](whisper-model-experiments.md) compares
small.en, base.en and tiny.en through the same matcher and prefetch policy.
Tiny.en peaked at 278.98–282.04 MiB for the worker and matched all 63 isolated
reference transcripts. Base.en peaked at 329.23–330.81 MiB but failed strict
reference parity on two perturbations, so its completed benchmark exits
nonzero. Small.en remains the default. These figures exclude the rest of the
application and do not establish Windows or combined-budget acceptance.

The [configurable wake experiment](kws-experiment.md) measures a separate Rust
KWS probe, including its native sherpa-onnx/ONNX Runtime backend: 53.30–54.30 MiB
peak RSS, 15/15 primary paced surrogate wakes, no extra triggers on selected
controls. Its higher-threshold arms miss wakes and its ungated silence costs
1.40–1.42% of one core. The probe is not integrated; replacing Vosk's resident
command decoders requires buffered audio and an on-demand command recognizer,
with their memory and latency measured together. No production default changed.

## Long-run RSS-floor gate

```sh
.venv/bin/python scripts/benchmark_memory_soak.py \
  --binary src/murmur/bin/jarvis-voice --fixture /path/to/goforward.raw \
  --hours 24 --runs 3 --warmup-hours 1 --windows 3 --tolerance 1 \
  --output docs/benchmarks/soak.local.json
```

This is **three sequential 24-hour runs**, not a completed result. Each run
records wake timestamps and RSS floors, discards the first hour, and fits a
least-squares slope in three equal windows spanning the declared run duration.
Every window needs at least three floors, and the post-warm-up observations
must cover that duration with no gaps longer than five minutes, including its
start and end. Negative, non-finite, unordered or out-of-run observations are
invalid. A short sample cluster cannot stand in for an entire day.
A run is stable only when every slope is at most 1 MiB/hour;
persistent growth means every slope exceeds that tolerance. Mixed windows or
repeated-run outcomes are inconclusive and exit non-zero. Only three or more
stable completed runs pass. Short runs, missing/non-finite observations and
incorrect command text cannot pass. Each completed run is saved before the
next starts; an interrupted campaign is not a pass.
Short diagnostic campaigns keep `full_duration_worker_soak_pass` false even if
their requested shorter-duration slope checks pass. This field requires at
least three passing runs of at least 24 hours each.

This runner retains the load-cycle benchmark's historical room-noise workload
by default for comparison. Its scope is memory, not wake correctness. Use the
no-noise acceptance case to gate the missed-wake fix. Worker RSS excludes the
controller, settings and capture process, so the soak is not the combined
application budget. Its gate math is unit-tested against warm-up growth,
plateaus, sustained growth, mixed windows, missing data and bad recognition.
