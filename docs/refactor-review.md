# Local refactor verification — 2026-10-24

The refactor preserves Jarvis's command policy, model weights, inference thread
counts, IPC protocol and consent rules. Changes remain local and uncommitted.
The comparison baseline is commit `8903b22634a74876f12ffcc9762206e1737fed2c`.

## Changes

- Python and Rust now parse brightness commands directly instead of generating
  every monitor/percentage combination per request. The controller and worker
  share Rust normalization. Parsing reuses normalized text and number words.
- Python builds fuzzy-match windows only when the whole transcript fails, and
  extracts each monitor identity once. Rust's longest-block matcher uses one
  temporary row instead of allocating a row per character. Matching thresholds,
  tie breaking and ambiguity rejection are unchanged.
- Application lookup avoids repeated alias normalization and window-class
  construction. The Rust controller shares immutable registry snapshots across
  queued actions; replacing a registry keeps earlier actions' snapshots valid.
- Configuration merging copies retained defaults once. Validation avoids
  redundant parser construction and repeated parsing of Rust's default JSON.
- Speech input padding uses one combined list. Rust vocabulary checks use the
  existing sorted vectors and avoid temporary token vectors. Neural kernels,
  quantization, embedding precision and decoding remain unchanged.
- Native IPC request routing and event handling have explicit functions, keeping
  blocking discovery outside the service actor. Three build entry points share
  their build, verification and atomic executable-install steps.
- Replay shutdown tolerates an already-exited child and closes its diagnostic
  pipe. Invalid JSON response shapes produce actionable connection/replay errors.

The pass covered command/configuration policy, app discovery and desktop
adapters, IPC and worker lifetime, capture/replay supervision, both speech
implementations, GTK settings flows, native model loading/kernels, CLI/setup,
build scripts and existing CI. Code without a justified simplification was
retained, including numerical kernels and capture ownership protections.

## Measured results

Five fresh Python processes per case used identical synthetic input. Timing ran
without allocation tracing; a separate call measured temporary Python
allocations. The ten result digests match between the baseline and refactor.
Full samples and workload boundaries are in the
[measurement record](benchmarks/refactor-2026-10-24.json).

| Python workload | Before, ms/call | After, ms/call | Before/after peak temporary bytes |
| --- | ---: | ---: | ---: |
| Brightness command | 0.4063 | 0.0222 | 3,363 / 2,011 |
| Unknown brightness target | 3.6646 | 0.0378 | 3,667 / 2,249 |
| Application command | 0.0374 | 0.0328 | 1,937 / 1,897 |
| Exact brightness matching | 1.4787 | 0.2081 | 39,672 / 4,267 |
| Fuzzy command matching | 0.5458 | 0.4897 | 39,588 / 12,002 |
| Candidate generation | 859.3382 | 26.2719 | 157,410 / 157,018 |
| Configuration validation | 1.9758 | 1.4024 | 48,516 / 48,276 |
| Search 100 fixture windows | 1.4537 | 0.0209 | 8,704 / 3,587 |
| Merge settings | 0.0539 | 0.0441 | 16,200 / 12,920 |
| Prepare one second of speech input, inference stubbed | 0.4720 | 0.4000 | 873,040 / 712,984 |

CPU time also decreased in all ten cases. Python process peak RSS includes the
interpreter, imports and benchmark setup; individual medians moved by less than
0.15 MiB and the before/after sample ranges overlap. The allocation reductions
are clearer evidence than those small RSS differences. These measurements do
not establish whole-app speech latency, resident model memory or gaming FPS.

Both Rust controllers were rebuilt with Rust 1.99.0 and identical release
settings. With 1,000 synthetic applications, median controller PSS was
**6.74 → 6.73 MiB** before queuing work. With 16 blocked fixture actions it was
**15.84 → 6.73 MiB**, removing about **9.1 MiB** of registry copies. This is a
bounded queue stress case, not normal single-command standby.

Five alternating fresh paused-service pairs measured **3.88 → 3.83 MiB PSS**,
**5.77 → 5.86 ms** startup and **0.0286 → 0.0294 ms** status latency. Timing
ranges overlap; these small timing differences do not establish an improvement
or regression. Both recorded zero idle CPU ticks during the one-second samples
(Linux process-accounting resolution applies). An initial comparison against an
older prebuilt binary was discarded because it used a different build baseline.

## Verification

- **261 Python tests passed, no skips**, including local Vosk/Whisper/GTCRN and
  public-fixture comparisons, worker cancellation/lifetime, parser parity,
  recorder/replay fakes, IPC bounds, configuration and CLI checks. The final run
  completed in 41.213 seconds.
- **31 Rust unit tests passed**: controller 4, CLI 9, voice worker 5, Whisper 11,
  GTCRN 2. Native release builds and Clippy with warnings denied passed.
- A separate original/new Python comparison matched **23,180 command forms**.
  New Python and Rust regression cases cover every whole brightness percentage,
  aliases, optional articles, default targets and malformed number wording.
- Python compilation, `git diff --check`, source archive and wheel construction
  passed. Package inspection checked runtime assets, native sources and the
  shared build helper. Source archives exclude toolchains, build output and
  local settings.

All service integration used private fixture sockets and fake desktop/capture
programs. The only audio was the public `goforward.raw` fixture, verified as
SHA-256 `f15c60ec54059d8b66e410d0064945a0b0a04ea56e1ddca1958e493c0cf70e71`.
No live microphone, display capture, desktop action or installed-service
configuration was used or changed. The installed service was not restarted.

CodeRabbit did not review this diff. Automatic approval review rejected the
source upload because the user authorized local verification only. No source
was uploaded; external review requires explicit permission to send the diff.

## Reproduce locally

The later Windows/recording pass preserves the measured improvements: the final
three-process samples are in
[shared-policy follow-up](benchmarks/windows-shared-policy-2026-10-24.json).
All ten measured wall-time medians and temporary-allocation peaks remain below
the original baseline. This does not establish whole-app Windows performance.
Final validation ran 296 Python tests on Linux in 43.314 seconds: 292 passed and
four optional Windows audio conversion cases were skipped there; those four
passed in the separate 29-test Windows suite. All 31 native
Rust unit tests and Clippy with warnings denied passed after rebuilding. Windows
packaging and its real-hardware limits are described in [Windows release](windows.md).

```sh
PYTHONPATH=src .venv/bin/python -m compileall -q src scripts tests
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/build_controller.py --test --offline
.venv/bin/python scripts/build_cli.py --test --offline
.venv/bin/python scripts/build_voice_worker.py --test --offline
.venv/bin/python scripts/benchmark_refactor.py --repeats 5 --output policy.json
.venv/bin/python scripts/benchmark_controller_queue.py --output queue.json
```

Set `JARVIS_VOSK_MODEL`, `JARVIS_WHISPER_MODEL`, `JARVIS_GTCRN_MODEL` and
`JARVIS_SPEECH_TEST_RAW` to explicit existing local assets for the full model
tests; optional tests otherwise skip. `--offline` builds require cached locked
Cargo dependencies. For policy comparison, use `--source OTHER_CHECKOUT/src`;
for queued-action comparison, use `--binary OTHER_CONTROLLER`.

Detailed local logs, the same-compiler baseline binaries and restored build
tools are under the ignored `build/refactor-review/` directory. That directory
is local verification material and is excluded from packages and Git changes.
