# KWS allocation ownership and isolated pressure recovery

This follows [continuous input and floor attribution](kws-continuous-diagnostics.md).
All changes remain in the separate file-only Rust probe. Production Vosk/small.en,
controllers and installed models are unchanged. No real capture, action dispatch,
installed service restart, or host-wide pressure generator was used. The full
memory plan and all application/Windows/soak release gates remain open.

## Allocation ownership beyond the first eight cycles

A compile-time `allocation-diagnostics` feature wraps Rust's `System` allocator
and counts successful allocation calls, live blocks and requested live bytes.
It delegates the same sizes to the same system allocator; it does not trim,
reserve extra model buffers, or intercept native sherpa/ORT allocation calls.
The normal build has no allocator wrapper and emits null for this diagnostic.
The feature binaries are built in a separate ignored target directory, with
both normal and diagnostic binaries/source archived before measurement.

[Allocation evidence](benchmarks/kws-allocation-attribution-2026-10-26.json)
compares 128 unpaced requests per fresh detector, sequentially, with identical
PCM and settings in the normal and instrumented arms. All 256 expected wakes
match, including identical original-input trigger chunks between arms. A new
32-cycle instrumented parent/detector run streams reports instead of retaining
the JSON trees already attributed in the earlier report. It uses the explicit
tiny.en payload, RMS 0.001 endpoint, existing one-second settling and fresh
command-worker teardown. All 32 commands and saved buffers match, and every
release returns to exactly two processes. No arm requests allocator trimming.

| Diagnostic | First sample | Last sample | Change |
| --- | ---: | ---: | ---: |
| Detector Rust requested live bytes, 128 requests | 40,744 | 40,744 | 0 |
| Detector Rust live blocks, 128 requests | 41 | 41 | 0 |
| Detector glibc in-use bytes, 128 requests | 34,884,880 | 34,897,520 | +12,640 |
| Detector RSS, 128 requests | 53.520 MiB | 53.520 MiB | 0 |
| Parent Rust requested live bytes, 32 cycles | 100,330 | 100,331 | +1 |
| Parent Rust live blocks, 32 cycles | 1,063 | 1,063 | 0 |
| Parent glibc in-use bytes, 32 cycles | 193,936 | 229,376 | +35,440 |
| Parent released RSS, 32 cycles | 3.305 MiB | 3.531 MiB | +0.227 MiB |
| Resident detector RSS in parent run | 54.246 MiB | 54.246 MiB | 0 |

Rust requested bytes in the parent increase by one at the two-digit cycle
boundary and otherwise remain constant. These counters do not show accumulating
Rust objects in either process during these controls. The remaining glibc
in-use growth lies outside the changes in Rust requested bytes: native/runtime
allocations and malloc size/arena bookkeeping remain candidates. Subtracting
requested bytes from `mallinfo2` is not an exact native-allocation accounting
method because requested sizes omit allocator overhead and allocator snapshots
occur at slightly different points in report construction.

The detector's glibc in-use count spans only 32 bytes from request 33 through
128 in this run. The parent still gains 2,592 in-use bytes from cycle 25 to 32,
even while its sampled RSS stays at 3.531 MiB. Earlier cycle windows gain more.
This narrows the ownership and shape of the trend; it proves neither a native
leak nor leak freedom, a full-duration plateau, or the required soak result.
The next deeper ownership check needs native/runtime allocation stack evidence
or a controlled child-reader/thread lifetime comparison, without hiding the
residual trend with trimming. Unpaced detector repetition is not wall-clock
soak or latency evidence, and reusing this recording adds no acoustic coverage.

## Worker failure is a window failure with explicit recovery

The continuous probe now records worker errors, malformed protocol output,
stdin failures and unexpected exit status as failed windows. It kills/reaps
any surviving worker, releases its audio, and requires fresh quiet before a
later wake can start a new window. Failed audio is never retried or queued.
Exit code and Unix signal are included in the window report. Detector failure
and parent file/buffer errors still stop the probe with a clear error.

A successful command can exit before its stdout-reader thread delivers the
last event. The parent now allows that channel to drain through EOF before
rejecting a successful exit without a result. The existing input-time speech
and wall-clock worker deadlines remain bounded.

The final report exposes `worker_failures`. Any worker fault makes the final
probe exit **1**, even if a later wake succeeds; recovery does not turn the
missed command into a passing recognition gate. The synthetic process test
covers exit 7, an explicit worker error and invalid JSON, each followed by a
later successful synthetic result. Those are protocol/lifecycle tests, not
speech-recognition examples.

## Real cgroup limits, confined to the experiment

[Corrected pressure evidence](benchmarks/kws-pressure-recovery-v2-2026-10-26.json)
uses three new transient user units. Each unit contains only the probe and its
explicit detector/command children. The external harness verifies ownership of
that unit's cgroup before changing any limit, disables swap for that group,
sets a 60-second lifetime bound, checks per-process OOM handling, and records
kernel memory/OOM/pressure counters. Units are collected after completion.
No existing service or unrelated cgroup is changed.

For these diagnostic controls only, `continuous.command_oom_score_adj:1000`
raises the command child's OOM victim preference before configuration is sent.
It accepts only 0–1000 and is omitted by default. This makes worker death the
intended pressure fault while allowing the parent/detector to report it.
It does not demonstrate that arbitrary application-wide OOM victim selection
will preserve those processes. The complete Jarvis application is not running.

Each input contains two separated copies of the already-supplied natural
file. The endpoint remains the separate RMS 0.001 candidate and the command
payload remains explicitly experimental tiny.en.

| Control | Limit | Actual commands by window | Kernel OOM kills | Raw probe/unit exit |
| --- | --- | --- | ---: | ---: |
| Bounded success | 384 MiB | `ten meters`, `ten meters` | 0 | 0 |
| OOM, then release limit | 192 MiB; 384 MiB after first failure | none, `ten meters` | 1 | **1** |
| Sustained OOM | 192 MiB | none, none | 2 | **1** |

All three lifecycle controls pass. In the two OOM controls, kernel `oom_kill`
counts match command workers terminated by SIGKILL. Every release leaves the
parent and detector alive with two sampled processes. The sustained limit
starts exactly one worker per accepted wake; there is no restart between wakes.
All three submitted buffers are byte-identical to their source intervals.
The OOM controls retain `command_recognition_gate_pass:false`; the complete
recognition gate for this pressure matrix is **false**, not waived by expected
failure handling.

The 384 MiB success control has a kernel cgroup peak charge of 279.48 MiB;
the released-limit control peaks at 277.31 MiB and the sustained control at
192.00 MiB. Cgroup charges differ from summed RSS/PSS: shared/file pages may
already be charged to another group, and these runs reuse warm filesystem
caches. These are actual enforced limits, not new combined application memory
figures. They do not establish behavior on an 8 GB Windows machine, arbitrary
memory pressure, denoising/capture/replay interaction, or a pressure release
sign-off.

The [first setup attempt](benchmarks/kws-pressure-recovery-2026-10-26.json)
failed before any probe started because this systemd rejects `MemoryOOMGroup`
as a unit property. Its empty event lists, stderr and exit 1 remain preserved
as an infrastructure failure. The corrected harness instead verifies the
private group's `memory.oom.group` directly; the earlier failed setup is not
counted as acoustic or pressure evidence.

## Validation, saved state and next gates

[Validation](benchmarks/kws-pressure-allocation-validation-2026-10-26.json)
records 12 passing Rust tests in the normal build and the same 12 in the
feature build (do not add those overlapping counts), nine passing explicit
Python tests with no skips, all-feature/all-target Clippy with warnings denied,
formatting, Python compilation, diff checks and source-package inclusion/target
exclusion. All 35 submitted allocation/pressure buffers and measured source/
binary hashes were rechecked. The old full Python suite was not rerun.
All experiment processes have exited and all three transient units were collected.

`build/memory-plan-kws/pressure-allocation-candidate/snapshot.json` identifies
both build variants and their exact source archive. All earlier baseline and
candidate snapshots remain intact. Reproduction requires new paths:

```sh
cargo build --release --offline --locked --features allocation-diagnostics --target-dir build/memory-plan-kws/allocation-build --manifest-path native/kws-probe/Cargo.toml
.venv/bin/python scripts/benchmark_kws_allocations.py --config "$WORK/endpoint-controls/natural-rms-0.001/config.json" --fixture "$WORK/cycles/tiny.en-gated-natural/input.raw" --work-dir "$NEW_ALLOCATION_WORK" --output "$NEW_ALLOCATION_RESULT"
.venv/bin/python scripts/benchmark_kws_pressure.py --config "$WORK/endpoint-controls/natural-rms-0.001/config.json" --fixture "$WORK/cycles/tiny.en-gated-natural/input.raw" --work-dir "$NEW_PRESSURE_WORK" --output "$NEW_PRESSURE_RESULT"
```

The pressure harness needs access to the user systemd bus and write access only
to its own transient cgroup. The restricted shell blocks that bus; this campaign
used approved extended execution for the confined harness. It is not an automatic
job and leaves no service running. Both scripts exit 0 only for their explicitly
stated comparison/lifecycle controls; their release/acoustic fields remain false.

The earlier gate timing regression (up to +270 ms), failed RMS 0.005 endpoint,
small.en memory failure, missing real Jarvis-name positives and full negative
corpus remain unchanged. Next work is native/runtime allocation attribution,
longer pressure/recovery coverage and a timing design that retains context
without losing the silence-CPU saving. Full acoustic, application, Windows and
three sequential 24-hour soak acceptance remain required before integration.

The [subsequent reader/cache comparison](kws-reader-caches.md) now owns and joins
child readers, tests cancellation/backpressure, and separates malloc-cache
effects from stack caches and live-reader counts. It keeps normal allocator
settings and all full release gates unchanged.
