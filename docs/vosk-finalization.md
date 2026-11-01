# Vosk utterance-finalization experiment — 2026-10-26

The Rust worker has a separate, **disabled-by-default** experiment for releasing
Vosk feature history at existing utterance boundaries. The normal controllers
omit `native.vosk_finalize_utterances`, whose native default is false. There is
no new user setting. This experiment retains the free decoder, the existing
matcher and Whisper prefetch; it does not enable the Vosk fast path.

## Runtime behavior

With the internal flag enabled, detected endpoints use `FinalResult()` directly
instead of `Result()`. At reset boundaries, including quiet wake gaps,
ten-second bounds, cancellation and session changes, the decoder is finalized
before its usual reset. Results from those reset boundaries are **discarded**,
so they cannot create a wake or action. Recognition results still go through
the existing wake and command checks. Native errors propagate through the
worker's normal error/shutdown path.

[Vosk 0.3.45's implementation](https://github.com/alphacep/vosk-api/blob/v0.3.45/src/recognizer.cc)
only frees the feature pipeline when `FinalResult()` is called in the running
state. Calling it after `Result()` or `Reset()` is too late. Vosk preserves
cumulative word timestamps when recreating that pipeline, which lets the
existing wake-to-command audio cut continue to use the stream offset. Repeated
wake/command checks exercise this across multiple pipeline lifetimes.

Flushing the feature pipeline can change the last frames, posteriors and
transcripts. Fixture agreement does not establish complete recognition
equivalence, so the full acoustic corpus remains required before adoption.

## Measurement method

`scripts/benchmark_vosk_finalization.py` compares fresh workers sequentially,
using the same binary with the internal flag false or true. All input comes
from the public `goforward.raw` fixture and its time reversal. It opens no
microphone, dispatches no actions and downloads no models or audio.

The unpaced probe runs 32 cycles per worker through the actual `Engine`,
including its energy gate, ten-second bound and matcher. Each cycle supplies
12 concatenated reversed fixtures as wake-background audio, two seconds of
quiet, a same-breath wake/command, and a separate command-only session. The
surrogate wake is “go forward”; both command phrases map to the test action
“start recording.” Each worker must return exactly 64 expected commands and
zero background events. Whisper and denoising are disabled for attribution;
all three Vosk handles and their shared model remain resident.

Both arms explicitly trim the heap after every offline request. The reported
floor is RSS after the command-only request, after its temporary PCM buffer
has been freed. The maximum rise is the highest floor minus the first floor,
not the last-minus-first value of a sawtooth. This unpaced probe is not a live
workload, a real-time CPU/latency measurement or a soak gate.

Separate paced workers use real Vosk, Whisper small.en and GTCRN with prefetch
and the existing 60-second idle policy. Each receives three wake/commands,
four seconds apart. Latency runs from the fixture's last voiced frame to the
command event. Fresh-process cold loading does not imply a cold OS file cache.
Worker-only RSS/PSS excludes capture, controller, replay and settings. These
Linux measurements cannot establish Windows or whole-application budgets.

## Paired results

[Raw results](benchmarks/vosk-finalization-2026-10-26.json) contain three fresh
workers per policy for the Vosk-only probe and three more per policy for paced
recognition. One saved baseline arm was resumed after its launcher stopped;
the completed comparison retains that arm rather than replacing it. Both
policies use the same measured binary and fixture hashes.

| Measurement | Default policy | Finalize utterances |
| --- | ---: | ---: |
| Correct Vosk-only probe commands | 192/192 | 192/192 |
| Background events in probe | 0 | 0 |
| Maximum floor rise across each 32-cycle probe | 44.93–45.21 MiB | 2.68–2.72 MiB |
| Ending probe RSS floor | 225.57–228.09 MiB | 177.91–179.93 MiB |
| Probe CPU time per worker, excluding model initialization | 20.05–22.59 s | 19.82–20.09 s |
| Correct paced commands | 9/9 | 9/9 |
| Median paced command latency | 1145.8 ms | 980.0 ms |
| Small-sample p95, nearest rank | 1243.3 ms | 1168.7 ms |
| Peak paced worker RSS | 543.31–568.25 MiB | 542.89–563.82 MiB |

The paired fixture latency gates pass. This is a small single-recording
comparison, not evidence that latency improves across accents, noise or real
commands. The reduced history does not remove the Vosk model or Whisper's
command-time memory peak. It also does not establish standby CPU, Windows
requirements, the combined 400 MiB command budget, the unexplained full live
floor or a 24-hour soak result. The adoption gate remains false and the
experiment remains disabled by default.

## Focused recognition controls

Both the [default arm](benchmarks/vosk-finalization-controls-default-2026-10-26.json)
and [finalization arm](benchmarks/vosk-finalization-controls-finalize-2026-10-26.json)
passed all five paced controls with real Vosk, Whisper and GTCRN: three natural
command tails without a wake, three reversed-speech negatives, a two-second
pause between wake and command, a 300 ms pause inside the surrogate wake name,
and three background/quiet/wake repetitions without added noise. Each arm
returned five expected commands and five wakes, with zero false actions or
extra wakes.

These are variants of one public recording. Recorded real commands,
confusables, music and game audio are still missing; both acceptance artifacts
correctly retain `passed: false`, and the runner exits 1 for incomplete coverage.
The internal experiment is not an adoption or platform sign-off.

The [validation record](benchmarks/vosk-finalization-validation-2026-10-26.json)
records 320 passing offline tests (33 skipped), 53 passing selected voice/model
tests, 33 passing Rust unit tests, clean all-target Clippy with warnings denied,
format checks and rejection of a mismatched benchmark resume. Real-model tests
cover successive decoder lifetimes, repeated background/quiet/wake handling,
command-only recognition, bounded offline repetition and the earlier fast path.
The rebuilt checkout-local worker has the same hash as the measured binary;
the installed service was not restarted.

## Reproduce

Build the worker and supply the existing local models and
[public fixture](speech-research.md), then run:

```sh
python scripts/build_voice_worker.py --test
python scripts/benchmark_vosk_finalization.py \
  --fixture build/refactor-review/goforward.raw --runs 3 --cycles 32 \
  --output docs/benchmarks/vosk-finalization.local.json
```

Use `--probe-only` for the shorter Vosk-only attribution. Completed arms are
saved incrementally; `--resume` requires identical parameters, input hashes,
platform and benchmark scope. A partial comparison cannot pass the paired
gate. `--engine`'s explicit offline `repeat` field accepts 1–20 repetitions
totaling at most 60 seconds; normal microphone utterances remain bounded to
ten seconds. `trim_heap` is also an explicit offline measurement request.

The acceptance and load-cycle scripts accept `--finalize-vosk-utterances`.
Missing acoustic categories continue to fail acceptance. This experiment is
separate from the earlier [single-decoder history probe](vosk-decoder-history.md)
and [Vosk fast path](vosk-fast-path.md).
