# Memory-plan code review — 2026-10-27

The full memory-plan diff was reviewed after the live replay fixes were
committed as `928fab4`. The comparison starts at the supplied handoff base,
`62142a1`, and includes the foundation/experiment commit `51b8627` plus the
live replay validation commit: **97 changed code, test, build and CI files**.

CodeRabbit raised **0 issues** in its full review of 124 files, including
supporting documentation. Independent local inspection and fault tests found
three additional issues, fixed below. A CodeRabbit follow-up on those fixes
raised **one minor test-timing issue**, also fixed. The
[machine-readable review record](benchmarks/memory-plan-code-review-2026-10-27.json)
contains scope, source/log hashes, review output and validation results.

## Fixes from local inspection

| Area | Problem | Result |
| --- | --- | --- |
| Soak acceptance | Window boundaries used the last available floor rather than the declared run duration; a short post-warm-up sample cluster could report stable for a nominal day. Invalid parameters and negative/out-of-run values could also pass or raise an exception. | Fits span the declared run; floors must cover it with no post-warm-up gaps longer than five minutes. Invalid values are rejected. Short diagnostics cannot set the full-duration worker-soak flag. |
| Disk-store lifecycle | A blocked helper write or response held the capture condition indefinitely, preventing Stop/cleanup from finishing. | One owned watchdog per buffer applies a ten-second request deadline to writes and replies. It terminates an unresponsive helper, unblocks the caller and joins during cleanup. Marked crash sessions are cleaned on the next start. |
| Windows save recovery | Failed verification left a completed staging clip on disk but omitted its path from status; a concurrent capture error could also be overwritten with an on state. | Failed saves report any retained partial clip path and preserve an existing capture error state. |

The watchdog uses a separate condition from capture operations, avoiding a lock
dependency on the thread it must unblock. It remains asleep between requests;
it does not create a new timer thread for each segment. Tests reproduce stalled
writes/responses and stop a real Rust helper with `SIGSTOP`, then verify bounded
failure, child/thread cleanup and recovery on a subsequent start.

The new tests fail against their earlier implementations. The full suite also
caught an encoder test that counted newly logged IPC events as process launches.
It now checks the launch arguments and IPC readiness explicitly. CodeRabbit's
minor follow-up correctly identified that the fault test's short deadline also
covered helper startup; the short deadline now applies only after `SIGSTOP`.

## Validation and scope

- Full offline Python suite: **377 run, 338 passed, 39 skipped**. Skips retain
  their explicit model, optional UI or platform requirements. The initial
  encoder-test failure remains in the raw log, followed by the passing run.
- Final focused checks after the timing adjustment: **10 passed**, including
  soak gates, both stalled-I/O cases, stopped-helper recovery and failed-save
  retention. These overlap the full suite and are not added to its count.
- Native tests across controller, replay store, voice worker, Whisper,
  KWS and CLI: **69 passed**. KWS's allocation-diagnostics build separately
  passed its same 18 tests. All six crates passed formatting and all-target,
  all-feature Clippy with warnings denied.
- Windows Python under the existing private Wine prefix: **17 run, 11 passed,
  6 skipped**, covering the native disk helper, stalled-I/O deadlines and
  retained save failures. This is component evidence; hardware capture,
  Windows memory budgets and the 8 GB platform gate remain open.
- Python compilation, source-package inclusion/exclusion and diff checks pass.

The external review used a private, isolated Git fixture. Every included changed
file was checked byte-for-byte against the committed original. Raw benchmark
JSON and redistributed Windows license bundles were excluded from that fixture;
runtime code, scripts, tests, build/CI changes and explanatory documents were
included. Automatic approval review initially blocked the external upload; the
user explicitly authorized it before the successful reviews. No live media was
sent. Findings above distinguish local inspection from CodeRabbit's output.

These review fixes change the Windows disk-store adapter, failed-save reporting
and measurement tooling. The live Linux service continues using the controller
that passed [save/decode validation](replay-live-validation.md); its binary did
not change during this review. Production Vosk/small.en and all disabled
recognition-experiment defaults remain unchanged.

Review and code-test success do not complete the memory plan. Strict Linux
byte bounds, broader recognition/timing evidence, integrated whole-application
budgets, real Windows/8 GB pressure and three sequential full 24-hour soaks
remain open. Existing failed candidate measurements are preserved. See
[the progress report](memory-plan-progress.md) for the remaining engineering work.
