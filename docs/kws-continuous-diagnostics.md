# Continuous KWS handoff and memory/timing attribution

This extends the [isolated cycle/endpoint probe](kws-handoff-cycles.md).
Production Vosk/small.en, controllers, model selection and both experimental
Vosk flags are unchanged. No capture, installed service or action dispatcher
was started. The complete memory plan remains open.

## Continuous file implementation

The separate Rust handoff probe accepts an optional `continuous` configuration:

```json
{"max_windows":8,"worker_timeout_ms":12000,"recovery_quiet_ms":600}
```

It requires `cycles:1` and an explicit quiet endpoint. One paced detector file
request can now produce several command windows while keeping the same loaded
spotter. The optional quiet gate can retire native streams inside that request;
this is continuous file input, not a promise to retain one native stream.
File input is capped at 120 seconds, windows at 32, command audio at ten seconds,
worker lifetime at 15 seconds and recovery quiet at three seconds. Defaults
still use the earlier one-window file/cycle path.

The parent keeps receiving input during loading, decoding and asynchronous
worker shutdown. Further detections while busy are reported and discarded;
there is no command queue or implicit retry. The speech endpoint has its own
input-time timeout. A separate wall-clock deadline covers a stuck load,
recognition or shutdown. A missing quiet endpoint at EOF aborts that window.
Overflow fails the entire probe instead of submitting truncated audio. Owned
workers are killed and reaped on failures as well as timeouts.

After release, new quiet must accumulate before the parent rearms. A trigger
from before that boundary is stale and cannot start another window. The
rolling onset ring continues while busy, but a later buffer starts no earlier
than its rearm boundary. After `max_windows`, further detections are reported
as ignored. This is a bounded experimental policy requiring actual acoustic
acceptance before adoption.

[Final lifecycle evidence](benchmarks/kws-continuous-final-2026-10-26.json)
passes all eight lifecycle controls and returns seven expected commands across
11 accepted windows. It checks three commands in one file, a deliberately delayed load with a busy
wake and subsequent recovery, speech timeout followed by a successful command,
repeated worker timeouts, the window cap, EOF before quiet, command without
wake, and explicit overflow. All submitted slices are compared byte for byte
with their source intervals. The RMS 0.001 endpoint and tiny.en are explicit
experimental settings. These repeated public recordings add no speakers or
custom-name positives. The three-window tiny.en control peaks at 335.93 MiB
combined RSS and ends at 58.58 MiB waiting for the parent/detector only. These
exclude the controller, capture, UI, GTCRN and replay, and pass no application
budget. No new small.en matrix was needed to preserve its existing failure.

The earlier [first implementation run](benchmarks/kws-continuous-2026-10-26.json)
contains seven passing controls. Its release sample still included the fixed
command buffer. The final run adds overflow and samples after that allocation
is released; use the final evidence for current memory observations. Do not
add the repeated counts together as independent acoustic evidence.

## Timing regression attribution

[Diagnostic evidence](benchmarks/kws-timing-diagnostic-2026-10-26.json) uses
three already-supplied recordings: `go forward` and the two upstream keyword
WAVs. Each has amplitudes 0.1, 0.3 and 1.0, and leading quiet 0.60, 0.63, 0.66
and 0.69 seconds. The 36 variations were fixed before measurement. Threshold
0.25 and the existing RMS 0.001 / 300 ms pre-roll / 1200 ms hangover are
unchanged. Workers run sequentially without allocator trimming. These timing
controls are unpaced: they compare exact original-input trigger chunks, not
wall-clock response latency.

The saved ungated binary/archive/source hashes are verified before use.
Baseline and gated labels match in all 36 variants, with no expected misses or
extra wakes. Timing nonregression **fails**, and the script correctly exits 1:

| Source | Later gated variants | Gated minus ungated input trigger time |
| --- | ---: | ---: |
| Public go-forward | 11/12 | 0 to +270 ms |
| Upstream LIGHT_UP | 0/12 | −270 to −90 ms |
| Upstream LOVELY_CHILD | 7/12 | −30 to +90 ms |

The original 0.1-amplitude, 0.60-second-lead go-forward case retains its +180 ms
regression. For every gated positive, a separate ungated control is cropped
at that gated native stream's reported original-input origin. Adding that
origin back to the cropped control's trigger position reproduces the gated
trigger chunk in **36/36** cases. The changed input context/startup is
sufficient to reproduce these timing changes without gate execution. This
isolates the cause from event delivery and timestamp accounting; it does not
separate native feature context, normalization, and decoder/frame scheduling.

No constants were tuned, no gate fix is promoted, and the original failed
timing result remains intact. A new design must retain timing on a broader
corpus while preserving the measured standby-CPU saving; these controls are
not enough to select that design.

## Released-floor attribution

[New attribution evidence](benchmarks/kws-floor-attribution-2026-10-26.json)
asks a new question about the earlier ten-cycle growth. Two sequential fresh
parent/detector sets replay the identical natural file eight times, using the
same tiny.en payload, RMS 0.001 endpoint and one-second settling. A fresh
command worker is loaded/reaped each cycle. The only arm setting changed is
`retain_reports`: true retains bounded JSON value trees, false streams cycle
reports without storing them for the final aggregate. A third fresh detector
alone processes eight identical paced requests. No arm requests a heap trim.

All 16 handoff commands and retained buffers match. Every handoff release
leaves two processes. All eight detector-only requests have the expected wake.
The sampler now exposes parent/detector RSS, PSS, anonymous and private-dirty
memory. Linux GNU builds also report `mallinfo2` in-use/free-arena/mmap bytes
for the parent and detector; other builds return null for this diagnostic.

| First to eighth released sample | Retained reports | Streamed reports |
| --- | ---: | ---: |
| Parent RSS | 3.313 → 3.707 MiB | 3.305 → 3.512 MiB |
| Parent RSS growth | 0.395 MiB | 0.207 MiB |
| Detector RSS | 54.340 → 54.340 MiB | 53.832 → 53.832 MiB |
| Parent in-use heap growth | 188,016 bytes | 20,560 bytes |
| Detector in-use heap growth | 5,840 bytes | 10,128 bytes |
| Serialized final retained reports | 26,710 bytes | 2 bytes (`[]`) |

The detector-only process also stays at 53.953 MiB RSS and 32.289 MiB anonymous
memory across eight requests; its in-use heap count rises 8,080 bytes. In each
handoff arm, **all observed released RSS growth is in the parent**. Report
retention accounts for substantial live allocations and adds 0.188 MiB to the
observed parent RSS growth between these fresh runs. Rust JSON value trees
occupy more heap than their serialized byte count. Parent allocator caching
and transient report/sampling/child-reader allocations also count; streaming
reports does not remove the residual parent growth.

These measurements distinguish the native-detector process from parent/report
allocations. They do not assign every allocation inside the detector to native
C++ versus Rust glue, or prove the precise ownership of the old binary's
0.418 MiB trend. The smaller detector in-use increases and residual parent
increase remain visible. Flat RSS in this short control is **not** leak freedom,
a full-duration plateau, pressure acceptance or a passing soak gate.

## Validation and reproduction

The [validation manifest](benchmarks/kws-continuous-validation-2026-10-26.json)
records 12 passing Rust unit tests and eight passing explicit Python
model/protocol tests, with no skips. Formatting, all-target Clippy with warnings
denied, the offline locked release build, Python compilation, diff checks and
source-package inclusion/exclusion pass. Exact source/binary/evidence hashes
and all seven submitted continuous buffers were rechecked. The full Python
suite was not rerun. All experiment processes have exited. The
baseline is unchanged. Current binaries and their source archive are saved
separately at `build/memory-plan-kws/continuous-candidate/snapshot.json`.
The previous cycle binary is preserved at
`build/memory-plan-kws/handoff-cycles-preserved`.

Use the existing cached toolchain and assets listed in
[the handoff](memory-plan-handoff.md). These scripts require new evidence/work
paths and do not run real capture:

```sh
.venv/bin/python scripts/benchmark_kws_continuous.py --config "$WORK/endpoint-controls/natural-rms-0.001/config.json" --fixture "$PUBLIC_PCM" --work-dir "$NEW_CONTINUOUS_WORK" --output "$NEW_CONTINUOUS_RESULT"
.venv/bin/python scripts/benchmark_kws_timing_diagnostic.py --config "$WORK/cycles/tiny.en-ungated-natural/detector.json" --fixture "$PUBLIC_PCM" --model-dir "$MODEL" --baseline-dir "$WORK/baseline" --work-dir "$NEW_TIMING_WORK" --output "$NEW_TIMING_RESULT"
.venv/bin/python scripts/benchmark_kws_floor_attribution.py --config "$WORK/endpoint-controls/natural-rms-0.001/config.json" --fixture "$WORK/cycles/tiny.en-gated-natural/input.raw" --work-dir "$NEW_ATTRIBUTION_WORK" --output "$NEW_ATTRIBUTION_RESULT"
```

The timing diagnostic's exit 1 is an acoustic timing gate failure and must
remain separate from implementation checks. The earlier RMS 0.005 endpoint
matrix remains failed, the RMS 0.001 focused results remain experimental,
and small.en still fails the reduced command-memory target. No full acoustic,
whole-application, Windows, pressure or three-run 24-hour soak gate passes.

Next work is to resolve the gate's context/startup timing behavior on broader
supplied acoustic evidence, bound/attribute the residual in-use trends over
longer runs, exercise worker failure and recovery under memory pressure, and
complete the missing custom-name/negative/platform corpus before integration.

The [subsequent allocation/pressure report](kws-pressure-allocations.md) adds
Rust requested-byte ownership counters, 128-request/32-cycle evidence and
worker-fault recovery under confined real cgroup limits. Residual glibc
in-use trends and all full release gates remain open.
