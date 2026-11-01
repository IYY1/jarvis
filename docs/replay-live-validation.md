# Linux live replay validation — 2026-10-27

Live save testing found two functional bugs in the memory plan's disk replay
adapter. Both are fixed; earlier failed results remain in the evidence rather
than being replaced. The user authorized local activation, saves, private
decode checks and deployment of the tested controller fixes.

## Problems and fixes

GSR 6.1.5 writes saved replays beside its `-o` buffer directory. Its `-ro`
option selects a simultaneous regular recording directory. The first fake
backend modeled this incorrectly, so the original tests missed that Stop
would delete a completed replay with its session. The first live save exposed
the error. That clip was recovered into the configured Clips directory and
its hash survived cleanup; no validation clip was lost.

The controller now publishes completed disk replays outside the disposable
session before verification. It atomically creates a new destination without
overwriting an existing clip. A cross-filesystem destination uses a private,
synced staged copy. If the Clips destination fails, a recovery file is retained
outside the session and reported. A save holds the session cleanup lease through
publication and verification, including when pressure stops the recorder.
Clips that fail HDR verification are still retained for inspection.

The next live save then exposed a second bug: the old IPC command omitted
`data.seconds`. GSR interprets omission as the entire backing buffer, not its
configured replay history. The saved file contained **942.633 seconds** even
though history was set to 120 seconds. Both video and audio decoded completely,
so this was real excess footage. The controller now explicitly requests the
history length captured when replay starts, in RAM and disk modes.

Installed-client evidence comes from REA process captures, without opening or
decompiling a native binary: [recorder CLI](benchmarks/gsr-cli-evidence-2026-10-27.json),
[save CLI](benchmarks/gsr-save-cli-evidence-2026-10-27.json), and an
[isolated IPC protocol probe](benchmarks/gsr-save-duration-protocol-2026-10-27.json).
The probe uses the installed `gsr-cli` against an owned fake socket and records
`{"id":1,"name":"save-replay","data":{"seconds":120}}`. Actual save behavior
and the local cached upstream source establish routing; recorder help alone
does not document the distinction.

## Verification

The [regression record](benchmarks/replay-save-regressions-2026-10-27.json)
preserves binary/source hashes and failing-before/passing-after results:

- Five retention regressions produced six expected assertions against the old
  controller, then passed with the fix. Coverage includes filename collisions,
  a real cross-filesystem destination, failed HDR verification and recorder
  pressure/exit during a delayed save verification.
- Duration regressions produced four expected assertions against the first
  fixed controller, then passed with explicit default 120-second and changed
  30/60-second requests in both storage modes.
- The final controller module ran 38 tests: **37 passed and one optional real
  voice/model test skipped**. All seven native controller unit tests passed,
  the locked offline release build passed, and all-target Clippy passed with
  warnings denied. Formatting passed; the source package includes the new
  publication module and excludes compiled targets.

The tested controller is installed locally. Both deployments preserved existing
recognition, HDR, monitor, audio and replay settings; the service restarted to
load each tested fix. Replay remains on with disk storage, 120 seconds, and a
2048 MiB supervised stop threshold. Vosk/Whisper small.en remains the normal
voice path, with the recognition experiments disabled.

Final live verification is recorded separately in
[the third save run](benchmarks/replay-live-save-validation-v3-2026-10-27.json).
It warmed the buffer for 125 seconds, saved/decoded a clip, observed another five
minutes, saved/decoded again, then checked Stop and restart. **All functional
checks passed.** The clips were **122.000 and 120.867 seconds**, allowing the
recorder's keyframe padding around the configured 120-second request. Both had
HEVC 10-bit HDR/PQ/BT.2020 metadata and audio, and both fully decoded without
errors. Their hashes survived Stop and restart; the old private session was
removed and a new private session resumed writing. Configuration and the
controller/voice-worker PIDs remained unchanged during that run.

Recorder RSS rose **116.12 → 118.93 MiB** over the five-minute observation
(2.82 MiB). The final readable process sum was **292.69 MiB RSS**, with incomplete
coverage. This does not establish the 300 MiB whole-application budget or a
stable plateau. No natural backing-file rollover occurred in the sampled run.
The installed controller SHA-256 is
`80a6d70ffd5dc773b878be903826101d86488b53800c189534d036911a9d6272`.

## Evidence and limits

The [initial routing failure](benchmarks/replay-live-save-validation-2026-10-27.json),
[safe recovery](benchmarks/replay-disk-save-recovery-2026-10-27.json),
[duration failure](benchmarks/replay-live-save-validation-v2-2026-10-27.json),
[first deployment](benchmarks/replay-save-fix-deployment-2026-10-27.json) and
[duration-fix deployment](benchmarks/replay-duration-fix-deployment-2026-10-27.json)
remain available. Saved clips stay in the user's configured local Clips folder.
Media checks decode privately to null output; no media is displayed, played or
uploaded. Only metadata, diagnostics and hashes enter the repository.

This is an observational desktop check, not a controlled game workload or a
24-hour soak. One privileged helper's memory is unreadable; a separately launched
settings process is outside the service tree. Readable RSS sums are lower
bounds, and a short recorder trend does not establish a stable memory plateau.
The final run does not establish natural 256 MiB file-boundary rollover unless
explicitly recorded. Strict Linux byte quotas, full disk/pressure behavior,
Windows hardware, complete acoustic acceptance and combined application
memory targets remain open. See [overall plan progress](memory-plan-progress.md).
