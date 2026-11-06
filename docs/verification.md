# Verification record and remaining acceptance checks

Current checks use Python 3.14.8 with Vosk + Whisper small.en. No test here
establishes perfect recognition, universally unattended capture, or gaming FPS.
Jarvis is MIT licensed; the completed review is described in
[the review record](review-ready.md).

## Current checks (2026-10-21)

- All 186 tests passed with the real local Vosk/Whisper models and the checksum-verified public PCM fixture; no tests were skipped.
- The combined replay/HDR/resident-worker review raised one minor issue: replay audio switches could be saved while the running child kept its old sources. Changes to those switches now require replay to stop, with regression coverage.
- A reproducible selector-order bug could starve Listen once/Cancel when microphone data and a control line were ready together. Control lines now take priority in either event order; models stay resident.
- Replay save requests allow enough time for the backend reply plus media validation, and the replay child uses a private file-creation mask. No recognition model, thread count or quality setting was reduced.
- The follow-up CodeRabbit review raised one minor test-harness race around late subscription. Fixture playback now waits until the status observer is connected; wake observation remains required.
- Automatic HDR capture on DP-3 produced both a recording and a saved replay with verified 10-bit HEVC, limited-range PQ/BT.2020 video; temporary silent clips were removed. See [recording verification](recording.md#hdr-recording-and-replay) for the limits.
- The GTK HDR selector passed hidden-window construction and load/save checks for all three modes. Source and wheel builds succeeded with replay, runtime assets and HDR tests included; local agent state is excluded.

## Initial publication checks (2026-10-21)

- All 150 tests passed in the independent publication check (3.231 seconds),
  with real local Vosk/Whisper models and the public PCM fixture enabled;
  no tests were skipped.
- CodeRabbit raised 0 issues across all 13 completed review commits. Its scope
  included 13 text files; the binary UI preview was excluded.
- Bounded capture/decode timestamps, deadline flush, cancellation, stale results
  and slow-decoding microphone-stall regressions.
- Identical paired transcripts across all 21 resource benchmark cases; measured
  CPU/latency/RSS tradeoffs in [performance](performance.md).
- User-confirmed Steam launch with settings closed, plus observed service state
  transitions and independent background microphone/worker ownership.
- MIT package metadata, wheel resources, portable example paths and default
  example configuration. See [GitHub Actions](https://github.com/abdullaho21/jarvis/actions)
  for remote core, optional-voice and packaging checks.
- The Jarvis GTK window was constructed and closed without being presented or
  connected to the live service. Wheel and source archives were inspected for
  license metadata, both CLI names, ONNX configuration, installer and examples.

## Historical checks (2026-10-20)

- Automated run after the October 2026 review fixes: **91 tests passed** with
  the upstream `goforward.raw` fixture and PocketSphinx 5.1.1 (Python 3.13);
  the ordinary offline run skips five fixture-dependent tests. The GTK window
  was smoke-tested under Xvfb against a dry-run service. Synthetic (espeak-ng)
  phrases were decoded through the new grammar; see [review history](history/development-notes.md#review-fixes-october-2026).
- A temporary systemd user-service check confirmed that desktop launches enter
  a separate scope and survive controller shutdown. The fixture/unit were
  cleaned up and no persistent service file was installed. See
  [result](benchmarks/service-scope-integration.json).
- Local editable package installation and CLI entry point, syntax compilation,
  user installer dry run, configuration serialization and private permissions.
  The installer was not run against real user directories; no service was enabled.
- Unit tests for deterministic commands, negative/out-of-range workspace values,
  shell-like phrases, custom phrases, app aliases/ambiguity/hidden overrides,
  malformed desktop entries, bounded settings, activation scores/timeouts,
  deduplication, typed handlers and separate open-recorder/start-recording behavior.
- Consent/mute tests including a deliberately delayed subprocess start, proving
  mute closes an in-flight worker before returning. Recorder fake-child tests
  cover SIGINT finalization, crashes, empty output, missing dependencies/source,
  source type checks, duplicate starts and disconnected feedback callbacks.
- PocketSphinx tests using an explicit upstream fixture, including KWS,
  unsupported tokens, grammar injection, exact frame size, cancellation, and
  recovery from unmatched speech segments. No fixture/audio/model is vendored.
- Full worker→service→parser→dry-run dispatch→IPC feedback replay in both PTT
  and hands-free modes. Hands-free uses a documented surrogate wake phrase;
  no real desktop action is triggered by prerecorded audio.
- Real GIO desktop launching through a temporary harmless `.desktop` fixture
  that writes a marker then exits. 85 installed apps and two monitors discovered.
  This does not mean every installed application's own startup succeeds.
- Two silent CPU screen captures and a silent VA-API capture on DP-5 completed,
  finalized and passed ffprobe video validation. Test videos were deleted.
  The first CPU test observed a focus change; the repeat and VA-API tests
  preserved focus. No cause was established for the first change; fullscreen
  game focus still needs user acceptance. No compositor configuration changed.
- Native GTK construction and live consent-disabled service IPC for six pages,
  search, app/config/device lists, setting round trips, command-alias editing,
  sound/combined-audio control persistence and single-instance Recorder routing.
  The native Wayland window was opened/closed for a 15 second resource sample.
- A 30 second live-input hands-free standby benchmark, with all desktop actions
  disabled and no audio/transcript files; separate paused/PTT/UI/encoder samples.
  [Measured results](performance.md) link raw JSON and scope limits.

## Re-run automated checks

```sh
PYTHONPATH=src .venv/bin/python -m compileall -q src scripts
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/install_user.py --dry-run --service
./run-jarvis doctor
.venv/bin/python scripts/check_desktop.py
```

Optional fixture tests require an explicitly supplied upstream sample:

```sh
curl --fail --location https://raw.githubusercontent.com/cmusphinx/pocketsphinx/main/test/data/goforward.raw -o /tmp/goforward.raw
sha256sum /tmp/goforward.raw
# f15c60ec54059d8b66e410d0064945a0b0a04ea56e1ddca1958e493c0cf70e71
PYTHONPATH=src \
JARVIS_VOSK_MODEL="$HOME/.local/share/jarvis/models/vosk-model-small-en-us-0.15" \
JARVIS_WHISPER_DIR="$HOME/.local/share/jarvis/models/sherpa-onnx-whisper-small.en" \
JARVIS_SPEECH_TEST_RAW=/tmp/goforward.raw \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_replay.py /tmp/goforward.raw --wake
```

Unix sockets, PipeWire and Wayland access are blocked by some build sandboxes.
In that case run checks in a normal user desktop session; this implementation
used explicitly permitted host checks rather than treating sandbox errors as
missing desktop software. Do not run Jarvis as root.

## Interactive acceptance still required

1. In Privacy, grant consent and confirm the default “hey Jarvis” works with
   your own voice. Test every intended phrase with headphones and speakers,
   quiet room and actual gaming sound. Record counts/rejections and false wakes
   over a representative session; never enable debug recording silently.
2. Use the input selector/level test, disconnect input, reconnect it, mute during
   loading/listening/processing, and verify no post-mute action. Confirm Listen
   once and timeout close PTT input/model. Repeat suspend/resume.
3. Test installed Firefox/Dolphin/Stremio via a reviewed alias; focus existing
   windows, deliberate ambiguous names, missing apps and app installation/removal.
   On a disposable window, exercise workspace moves and fullscreen set/unset.
   Those mutating desktop operations were unit-tested, not driven against the
   user's current windows during this task.
4. Review screencopy permissions and test the intended monitor's content visually.
   Test two starts, fullscreen focus, file playback/end, system audio/microphone
   sync, then combined source if used. Repeat after logout/reboot and hotplug.
   Existing session enforcement was disabled; no renewed-permission promise is made.
5. Start recording, force only the test recorder child to crash, confirm error
   and partial recovery. Test a full output filesystem, unavailable source,
   denied permissions and driver failure using disposable test locations.
6. Enable the user unit using the UI, log out/in, verify graphical-session
   environment, consent and bar indicator. Test a screen reader, keyboard-only
   navigation, fractional scaling, high text scaling and a small display.
7. Use the controlled game comparison in performance.md. Voice overhead,
   encoder/media-engine work and game frame times must remain separate metrics.

The exact commands for live microphone and temporary-screen benchmarks are in
[performance](performance.md). They are explicit opt-ins; regular unit tests do
not capture the screen, open a microphone, mutate desktop state or require cloud.
