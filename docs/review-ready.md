# Jarvis review handoff

The completed project is published at
[abdullaho21/jarvis](https://github.com/abdullaho21/jarvis). The source is MIT
licensed; dependencies and models retain their own terms. This is an early
desktop application with the acceptance limits below.

## Review map

| Area | Files |
| --- | --- |
| Parsing, confidence, capture deadlines | `commands.py`, `state.py` |
| Audio, activation and worker lifetime | `voice.py`, `vosk_engine.py` |
| Accurate inference and resource policy | `transcriber.py`, `onnx-cpu.conf` |
| Service, IPC, consent and cancellation | `daemon.py`, `client.py`, `config.py` |
| Desktop launch/focus and Hyprland dispatch | `apps.py`, `desktop_process.py`, `hyprland.py`, `actions.py` |
| Recorder ownership and recovery | `recorder.py` |
| Native settings | `ui.py`, `style.css` |
| Installation and reproducible measurements | `scripts/`, `examples/`, `docs/benchmarks/` |

Files above are under `src/jarvis/`. This namespace, socket/storage IDs,
`jarvis.service`, recording prefixes and legacy CLI are intentionally retained
for compatibility. Public branding, preferred CLI and launcher are Jarvis.

## Changes in this preparation

- Preserved the working Vosk + Whisper recognition path and existing settings.
- Fixed valid command rejection when decoding finished after capture's deadline;
  added bounded capture timestamps and slow-decode microphone-stall coverage.
- Reduced retained ONNX memory and native thread spinning with measured results
  and transcript comparisons. Kept eight threads after testing a slower option.
- Added MIT, current package metadata and a source manifest. Added `jarvis` /
  `run-jarvis` while keeping old shortcuts functional.
- Separated historical notes and raw benchmarks from current setup/performance
  documentation; replaced user-specific example paths and stale default config.
- Prepared separate CI jobs for core tests, optional voice dependencies and
  source/wheel construction; current runs are visible in
  [GitHub Actions](https://github.com/abdullaho21/jarvis/actions).
- Completed 13 review commits covering command validation, microphone-test
  lifecycle, model readiness, matching speed, voice-loop CPU, Whisper peak
  memory, and the GTK settings redesign.
- Corrected stale performance and verification documentation during publication.

## Verification and limits

CodeRabbit raised **0 issues** in the changes from `f6713aa` through `0b19217`:
13 text files were reviewed; the PNG preview was excluded from automated review.
The independent publication check passed all **150 tests**, with real Vosk,
Whisper and the public PCM fixture enabled and no skipped tests. A prior live
user check confirmed Steam launches with settings hidden. Wheel and source
distributions are checked separately from the working checkout.
See [verification](verification.md) and [performance](performance.md) for exact
scope and commands. Passing tests and automated review are not a security audit.

The correctness fix for one-breath commands adds latency; the performance report
records that tradeoff explicitly. Listen once and Cancel still reload models
in hands-free mode. Controlled gaming comparisons and longer voice/noise
acceptance remain open. See [security reporting](../SECURITY.md#reporting) for
handling sensitive findings.
