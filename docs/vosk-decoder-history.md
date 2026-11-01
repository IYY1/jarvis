# Vosk decoder history — 2026-10-26

The Rust attribution probe isolates history retained by each of the wake,
command and free-form decoders. All three stay resident, sharing the same
model, while exactly one receives audio. Whisper and denoising are disabled;
capture and action dispatch never run. These are Linux process measurements,
not Windows results, a live workload or a 24-hour soak.

## Method

Each fresh worker decodes 32 cycles of 42 seconds (1,344 seconds of audio)
without real-time pacing. The input repeats the public `goforward.raw` fixture,
reversed by 16-bit sample for the main comparison. This is an attribution
stimulus, not representative unrelated speech. There is no energy gate and
utterances are bounded at about ten seconds. Each cycle records RSS/PSS before
and after `malloc_trim`, the lifetime RSS peak, transcripts and CPU time.

The `reset` policy calls Vosk `Result()` at detected endpoints and `Reset()`
at the utterance/cycle bounds. The experimental `finalize` policy instead
calls `FinalResult()` directly at those boundaries, before any reset. It is
part of `--decoder-history`; normal recognition reset behavior is unchanged.
A subsequent [default-disabled Engine experiment](vosk-finalization.md)
tests finalization at the real recognition boundaries.

This order matters. In [Vosk 0.3.45's recognizer source](https://github.com/alphacep/vosk-api/blob/v0.3.45/src/recognizer.cc),
`Result()` and `Reset()` leave the recognizer in the endpoint state while
retaining the feature pipeline. `FinalResult()` frees the decoder and feature
pipeline only when called from the running state. Calling it after `Result()`
does not provide that reclamation. `CleanUp()` periodically rebuilds the
pipeline after 20,000 decoded feature frames, approximately 600 seconds. The
observed sawtooth is consistent with that implementation.

## Results

Three sequential, fresh workers per decoder/policy pair produced the
[reversed-fixture results](benchmarks/decoder-history-reversed-2026-10-26.json).
The table reports the largest post-trim RSS floor minus the first cycle's
post-trim floor within each run, then the range across runs. It does not report
total process memory saved by an adopted recognition change.

| Active decoder | Reset: maximum floor rise | Finalize: maximum floor rise |
| --- | ---: | ---: |
| Wake | 36.23–36.26 MiB | 0.12–0.16 MiB |
| Command grammar | 36.22–36.30 MiB | 0.11–0.17 MiB |
| Free-form | 39.24–41.29 MiB | 3.13–3.55 MiB |

Every reset run dropped by more than 10 MiB at cycles 15 and 29, after about
630 and 1,218 seconds of decoded audio. The final cycle was already on the
next rise, so comparing only the first and last values would hide much of
the retained history. Trimming each cycle did not prevent the rises; once
Vosk released old features, trimming could reclaim the freed heap pages.

A separate [forward-fixture free-decoder control](benchmarks/decoder-history-forward-2026-10-26.json)
showed a 37.73 MiB maximum floor rise with reset, versus 1.14 MiB with
finalization, with the same reset drops at cycles 15 and 29. This control has
one worker per policy, rather than the main comparison's three.

These results attribute a bounded contribution to each decoder's retained
history. They do **not** explain the complete 366 MiB live floor after three
hours. The free decoder's first-cycle floor was also higher than the grammar
decoders', even with finalization; feature history is not its only cost. The
isolated maxima cannot simply be added together to predict a live process,
where decoders receive different amounts of speech and share allocations.

## Decision and reproduction

An allocator change remains unsupported by this evidence. Finalization is a
promising separate experiment, but changing production boundaries requires
paired wake/command latency, false-action and complete acoustic acceptance
measurements first. This probe provides no recognition-quality sign-off and
does not alter the production policy.

```sh
python scripts/build_voice_worker.py --test
python scripts/benchmark_decoder_history.py \
  --fixture build/refactor-review/goforward.raw --repeat 3 \
  --output docs/benchmarks/decoder-history-reversed.local.json
python scripts/benchmark_decoder_history.py \
  --fixture build/refactor-review/goforward.raw --decoder free --no-reverse \
  --output docs/benchmarks/decoder-history-forward.local.json
```

The [public fixture and model provenance](speech-research.md) describe the
required local inputs. The runner records their hashes and the binary hash,
starts workers sequentially, and saves every completed arm. It does not
download audio/models or start the desktop service.
