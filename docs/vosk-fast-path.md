# Vosk fast-path experiment — 2026-10-26

The Rust experiment can avoid loading Whisper for an exact, confident Vosk
command. It remains **off by default**: measured cold fallback latency fails
the memory plan's non-regression requirement, and the complete recognition
corpus is still unavailable. Neither normal service settings nor the Windows
Python worker enable it.

## Decision rule

The internal native-worker payload can set `native.vosk_fast_path: true`.
The acceptance and performance scripts expose `--vosk-fast-path` where relevant.
The regular Python/Rust controller payloads omit this field, which defaults
to false. It is not a user-facing configuration setting.

Every grammar word must have a finite posterior between 0 and 1, the word list
must match the grammar transcript, and the minimum posterior must be at least
**0.98**. The complete normalized command and free-decoder transcript must
agree, allowing the existing courtesy prefixes/suffixes. Unknown words,
missing posteriors, partial matches, conflicting commands
and absent free decoding take the existing Whisper path. A fuzzy window can
score 1.0 for a substring, so that score alone never approves the fast path.

The free decoder and existing chooser remain in place. Agreement between two
decoders of one Vosk model is not proof of correctness; the full acoustic
acceptance corpus remains a hard gate.

To avoid loading weights for commands that never need Whisper, this experiment
also suppresses speculative prefetch. Fallback then loads Whisper when the
command decision needs it. Once loaded, the existing 60-second idle policy
still applies, so a fallback can keep later fast commands' memory above the
fresh-worker measurements below.

## Paired measurements

[Raw results](benchmarks/vosk-fast-path-2026-10-26.json) contain three fresh,
sequential workers per arm, each receiving three copies of the same public
`goforward.raw` recording through paced fake capture. “Go forward” is a
surrogate wake name and “ten meters” maps to a test command. No microphone or
desktop action runs. Vosk, Whisper small.en and GTCRN are real local models.
Whisper starts unloaded; cold means model-cold in a fresh process, not cold OS
file cache. These are Linux **worker-only** measurements, not Windows or
whole-application acceptance.

Separate offline `--engine` runs collect exact model-load/transcription counters.
The paced runs measure end-of-voiced-audio to command-event latency and process
RSS/PSS; counters and latency are not from one shared worker. Each arm has nine
paced commands. The reported p95 uses nearest rank on that small sample.

| Exact agreement, free decoder retained | Default | Experimental fast path |
| --- | --- | --- |
| Correct fixture commands | 9/9 | 9/9 |
| Whisper loads per three-command counter run | 1 | 0 |
| Whisper transcriptions per counter run | 3 | 0 |
| Peak worker RSS across three paced runs | 548.24–571.79 MiB | 178.73–180.57 MiB |
| PSS sampled after commands | 479.32–480.65 MiB | 171.11–175.18 MiB |
| Median command latency | 1041.8 ms | 704.0 ms |
| Small-sample p95 | 1170.0 ms | 874.6 ms |

The forced-fallback arms disable the free decoder in **both** sides of the
comparison. This verifies that the experiment retains Whisper, and isolates
the cost of losing prefetch; it is not an accent, noise or confusable-speech
test. Both sides recognized 9/9 commands, loaded Whisper once and transcribed
three times per counter run. Median latency was essentially unchanged
(981.1 versus 981.2 ms), but **p95 rose from 1017.7 to 2695.8 ms**. The first
command in each experimental worker took 2647.7–2695.8 ms, versus
905.3–925.8 ms with prefetch. Warm fallback commands were around 0.98 seconds.
Fallback still had high memory peaks: 530.55–537.16 MiB default and
536.23–556.68 MiB experimental. This design cannot be enabled under the stated
latency requirement, even though its exact-command arm improves substantially.

## Focused recognition controls

Five paced control cases passed with both the
[default path](benchmarks/vosk-fast-controls-default-2026-10-26.json) and the
[experiment](benchmarks/vosk-fast-controls-experimental-2026-10-26.json):
natural command-tail speech without a wake, reversed speech without a wake,
a two-second pause after the wake, a 300 ms pause inside the surrogate wake
name, and three background-speech/quiet/wake repetitions without added noise.
Each arm produced all five expected commands and wakes, with zero false
actions or extra wakes. The negative cases each repeat three times.

These controls use variants of the same public recording and do not supply
the missing real-command, confusable, music or game-audio categories. Both
acceptance artifacts correctly retain `passed: false` for the incomplete
corpus, and their runner exits 1. Unit tests additionally reject missing or
partial posteriors, conflicting/partial phrases and unknown words. Real-model checks verify zero Whisper loads for eligible commands
and retained Whisper loading/transcription when the free decoder is absent.

The [validation record](benchmarks/vosk-experiments-validation-2026-10-26.json)
records 320 passing offline tests (31 skipped), 51 passing selected voice/model
tests, 33 passing Rust tests and clean all-target Clippy with warnings denied.
The checkout-local worker was rebuilt without restarting the installed service.
It also records the measured and rebuilt binary hashes: their sole source
difference is Clippy's equivalent vector-copy simplification in the diagnostic
fixture-reversal branch. Recorded performance artifacts retain the original
measured hash.

## Reproduce

Build the worker and install the existing local models first. The benchmark
requires the public fixture documented in [speech research](speech-research.md).
It does not download models/audio or start the desktop service.

```sh
python scripts/build_voice_worker.py --test
python scripts/benchmark_vosk_fast_path.py \
  --fixture build/refactor-review/goforward.raw --runs 3 --commands 3 \
  --output docs/benchmarks/vosk-fast-path.local.json
```

Use `--counters-only` for the shorter load/transcription check. The recognition
runner accepts `--vosk-fast-path` with an explicit manifest and writes the
experimental setting into its result. Missing corpus categories still fail
that gate. Performance output also records `enable_by_default: false` and the
failed fallback latency gate.

The next adoption decision needs complete recorded commands, confusables,
unrelated speech, music/game audio, repeated background/quiet/wake tests, and
an approach that preserves acceptable cold fallback latency. This experiment
does not remove the free decoder or change production reset behavior.
