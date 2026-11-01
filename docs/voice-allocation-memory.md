# Voice allocation reductions — 2026-10-28

This pass reduces allocations in the existing Rust Whisper small.en/GTCRN path.
The trained models, precision, end-of-speech timing, idle release policy and
production recognition experiments are unchanged. The close-program feature
was developed alongside it; the allocation-only measurements below were taken
before adding its command grammar.

## Programming changes

- Replace the 50,256-entry token hash map and its separately allocated byte
  vectors with a dense range table and one packed byte arena. Read the bounded
  token file one line at a time. Keep duplicate/missing/invalid-ID checks,
  arbitrary input order, binary token pieces and empty pieces.
- Write mel features directly into the inference tensor. Read the existing
  silence padding virtually instead of allocating and growing a padded audio
  copy, then copying a second complete feature array.
- Accumulate GTCRN output directly into the returned center crop. Replace the
  full-length normalization vector with one 256-sample periodic divisor table,
  preserving the original floating-point addition order. Avoid the final
  full-output copy. At the ten-second bound this avoids two allocations of roughly 0.61 MiB
  apiece at different phases; their sizes must not be added together as a
  simultaneous peak saving, or added to the process measurements below.
- Calculate constant int8 matrix column sums directly. Previously a 3072×768
  matrix expanded into a 9 MiB int32 temporary to produce a 3 KiB sum. Keep the
  original reduction for other layouts/operators, with an overflow bound on
  the direct path. The existing exact-integer regression still passes.

## Controlled measurements

Fresh sequential old/new processes used the same installed model files,
eight threads and seven variants of the deliberately supplied public
`goforward.raw` fixture, repeated twice in each process. Three pairs were run
with denoising off and three with it on, alternating which binary ran first.
The isolated rows are medians across those three process pairs.

| RSS scope, MiB | Before | After |
| --- | ---: | ---: |
| Whisper, retained after commands | 316.29 | 314.45 |
| Whisper + GTCRN, retained after commands | 318.83 | 317.16 |
| Whisper, inference high-water mark | 436.51 | 403.93 |
| Whisper + GTCRN, inference high-water mark | 437.59 | 406.48 |
| Complete voice worker, retained after commands | 482.33 | 478.53 |
| Complete voice worker, high-water mark | 563.88 | 562.85 |

The complete-worker rows are one additional old/new pair with real Vosk,
Whisper and GTCRN, a paced fake microphone and three surrogate commands per
worker. Both returned all three expected commands. They exclude capture child,
controller, replay and settings UI; these are not whole-application savings.

The retained reduction is modest: about 1.7–1.8 MiB in the isolated transcriber,
and 3.8 MiB in the complete-worker pair. High-water marks vary with allocation
layout: Whisper-only after values ranged from 402.85 to 433.32 MiB, and the
GTCRN after values from 406.23 to 435.00 MiB. Before values ranged from
435.63 to 437.11 and 408.31 to 438.39 MiB respectively. The median isolated
peak reduction must not be represented as a guaranteed 31–33 MiB application
saving. Whole-worker peak savings were only about 1 MiB in this pair.
Model-loading high-water marks were nearly unchanged despite removing the
large column-sum temporary. The model weights remain the dominant allocation.

## Numerical and functional validation

- All 55 saved mel, encoder and sampled decoder-logit tensor pairs across
  seven public variants are byte-identical to the saved previous recognizer.
  Transcripts and token histories also match.
- All 30 before/after denoised waveforms are byte-identical and repeated-command
  state is stable. Cases include seven public variants plus generated lengths
  257, 511, 512, 513, 3072, 3200, 159999 and 160000 samples, each run twice.
- All 84 isolated before/after transcription comparisons match (six process
  pairs × fourteen commands, or 168 total decodes). These are perturbations
  and repeats of one recording, not independent speakers or an accuracy corpus.
- Rust tests, all-target Clippy with warnings denied, and format checks pass
  for the affected crates. Token parsing and overlap-add boundary regressions
  cover the changed storage and arithmetic.

Raw evidence: [process measurements](benchmarks/voice-allocations-2026-10-28.json),
[exact Whisper tensors](benchmarks/voice-allocation-tensors-2026-10-28.json), and
[exact denoised waveforms](benchmarks/voice-allocation-waveforms-2026-10-28.json).
The saved reports pin the measured binaries and fixture with SHA-256 values.
No microphone, desktop action or live service is used in these comparisons.

## Reproduce

Save the previous binaries before rebuilding, then use the existing scripts:

```sh
.venv/bin/python scripts/build_voice_worker.py --test --offline --cargo /path/to/cargo
.venv/bin/python scripts/check_rust_optimization.py \
  --fixture build/refactor-review/goforward.raw \
  --before /path/to/previous/jarvis-whisper \
  --output /tmp/whisper-exact-tensors.json
.venv/bin/python scripts/benchmark_rust_voice.py \
  --fixture build/refactor-review/goforward.raw \
  --before-rust /path/to/previous/jarvis-voice \
  --after-rust native/voice-worker/target/release/jarvis-voice \
  --denoise --output /tmp/voice-allocation-comparison.json
```

Repeat the benchmark with and without `--denoise`, alternating process order.
Add `--full-worker` for paced complete-worker comparisons. Keep the model files,
thread count and command grammar identical between the two sides.

## Final build and activation

The complete offline suite passes: 390 tests, 376 passed and 14 skipped. The
affected Rust crates pass 46 unit tests in total. The close-command regressions
cover shared names/aliases, configured launch words, exact close matching,
normal Win32 WM_CLOSE messages, validated Hyprland addresses and isolated
controller dispatch against fake desktop clients. No real application is closed
by the tests. Windows native desktop behavior was mocked on Linux.

The rebuilt binaries are staged atomically in `src/murmur/bin/`. The running
voice worker was reloaded through Mute/Resume, with its executable hash checked;
the controller and RAM-replay recorder PIDs were preserved during that first
activation. The user subsequently approved restarting Jarvis and clearing the
current replay buffer. The controller was restarted, and its executable and
voice-worker hashes match the validated binaries. Voice listening and 120-second
RAM replay resumed, configuration was unchanged, and the live Commands API
now includes “Close an app.” The cleared replay history is refilling. No real
application was closed as an activation test.
[Controller activation](benchmarks/voice-controller-activation-2026-10-28.json).
The full memory plan, broader acoustic acceptance and long-duration soak remain open.

[Final validation and build hashes](benchmarks/voice-allocation-validation-2026-10-28.json).

A final additional pair supplied the new close-enabled command grammar to
both old and final voice workers. Both recognized all three surrogate commands.
Retained RSS was **478.89 → 474.54 MiB**, a **4.34 MiB reduction**; peak RSS was
540.75 → 538.30 MiB. This pair verifies the final staged voice binary with the
expanded grammar and remains a worker-only, single-fixture comparison.
[Final worker pair](benchmarks/voice-allocation-final-worker-2026-10-28.json).
