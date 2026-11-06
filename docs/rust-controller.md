# Rust background controller

The optional `jarvis-controller` executable owns Jarvis's background service:
configuration, command parsing, desktop-entry discovery, the private Unix-socket
API, voice-worker supervision, desktop actions, recording and instant replay.
These run in Rust. There is no Python service or helper process behind it.
The existing Python/GTK settings window and CLI continue to use the same API.

The controller starts the existing Rust voice worker, whose Whisper and GTCRN
computation is already native Rust. Vosk wake detection still uses its shared
library. Screen encoding and replay buffering remain in `wf-recorder` and
`gpu-screen-recorder`; this port does not replace those encoders or video buffers.

## Build and select

From an editable checkout with Rust 1.91 or newer and the existing local models:

```sh
.venv/bin/python scripts/build_controller.py --test
.venv/bin/python scripts/build_voice_worker.py --test
src/jarvis/bin/jarvis-controller --check
```

The controller build is independent of the neural inference crates and installs
its executable atomically under `src/jarvis/bin/`. `--offline` uses cached Cargo
dependencies. Building never changes settings, starts capture or registers a
service. `--check` validates settings and resolves the voice model/library paths
without opening the microphone or starting the socket service.

Stop an existing controller before starting another. Either launch directly:

```sh
src/jarvis/bin/jarvis-controller
```

or select it through the existing launcher:

```sh
JARVIS_CONTROLLER=rust ./run-jarvis daemon
```

The latter replaces the launcher process with the executable; it does not retain
a Python parent. Python remains the default for ordinary source installations.
The standalone executable itself does not require a Python interpreter to run.
When the [Rust CLI](rust-cli.md) is built, `run-jarvis daemon` launches the Rust
controller directly. Use `JARVIS_CLI=python` to retain the compatibility launcher
and its `JARVIS_CONTROLLER` selector.

For the installed user service, an override can replace `ExecStart`:

```ini
[Service]
ExecStart=
ExecStart="/absolute/path/to/checkout/src/jarvis/bin/jarvis-controller"
Environment=JARVIS_CONTROLLER=rust
```

Reload the user manager and restart `jarvis.service` after installing a reviewed
override. Restarting the controller also restarts its voice and replay children.
Removing this controller override restores the original Python service; a
separate `JARVIS_VOICE_WORKER=rust` override can remain in place.

The Rust controller supports the Vosk/Rust worker path. PocketSphinx remains
available through the Python controller. `JARVIS_CONFIG`, `JARVIS_SOCKET` and
the existing XDG locations are preserved. Standalone deployments can supply
`JARVIS_VOSK_LIBRARY`, `JARVIS_RUST_VOICE_BINARY`, `JARVIS_PROJECT_ROOT` and
`JARVIS_UI_PYTHON` explicitly. The normal checkout discovers these locations
without importing a Python package. Model discovery never downloads files.

## API and ownership

The existing operations remain available:

| Area | Operations |
| --- | --- |
| Status and settings | `status`, `watch`, `config`, `save_config`, `set_startup` |
| Discovery | `apps`, `commands`, `devices`, `recording_devices`, `monitors` |
| Voice control | `mute`, `listen`, `cancel`, `mic_test` |
| Actions | `execute`, `open_ui`, `record_start`, `record_stop`, `replay_start`, `replay_stop`, `replay_save` |

Status has one additive `controller: "rust"` field. The socket remains 0600 in
an owned 0700 directory, with a shared `flock` service lock and same-user peer
credentials. Existing Python and Rust controllers cannot own the same socket
simultaneously. Requests are limited to 64 KiB, connections and event messages
to 64, action tasks to 16, voice events to 64 KiB and worker payloads to 1 MiB.
Subscribers retain the latest status, without an unbounded event history.

Configuration writes retain validation, 0600 temporary files, atomic rename and
file/directory fsync. Embedded default and command tables are checked against
the Python API by differential tests. Built-in command overrides, arbitrary
shell execution, signed/out-of-range workspaces and invalid custom targets
remain rejected. App launching still uses GIO and independent systemd scopes;
desktop targets pass the same allowlist checks before constructing arguments.

Voice results retain confidence, capture-deadline, maximum-result-delay and
deduplication checks. Mute/cancel invalidate queued results. Listen and Cancel
steer the resident wake worker. A listen-once worker's accepted result finishes
before its normal exit triggers replacement. Recorder/replay finalization and
device queries run separately from the state actor so mute and status remain
responsive while those operations wait.

The controller uses Tokio's current-thread runtime, with all owned process
creation on its long-lived main OS thread. Linux parent-death signals follow
the creating thread, so using temporary spawning threads would break ownership.
Voice/query children receive termination signals; recorder/replay children
receive SIGINT. Tests cover hard controller failure as well as graceful shutdown.
Saved recordings retain private staging files, HDR verification, fsync and
atomic publication without overwriting an existing final file. Unfinished
nonempty recordings remain available for recovery after failure.

## Measurements and verification

The [controller-only comparison](benchmarks/rust-controller.json) uses three
fresh sequential process pairs, alternating order, identical private default
settings, normal read-only app discovery, 100 measured status requests and
three idle seconds per process. Voice, replay and the UI are off in this test.

The measured controller comparison retained about **25.5 MiB RSS in
Python versus 5.9 MiB in Rust**. PSS was about **15.9 versus 3.6 MiB**. This
removes roughly 19.6 MiB RSS from the controller alone; the speech models and
video buffers have separate memory costs. Short idle samples used zero
measurable CPU ticks in both implementations. Request/startup timings are
engineering observations, not end-to-end speech or gaming performance claims.

```sh
.venv/bin/python -m unittest tests.test_rust_controller -v
.venv/bin/python scripts/benchmark_controller.py --output /tmp/controller.json
```

Integration tests use temporary settings/sockets, fake voice and desktop
backends, fake encoders and private fixture files. They cover schema/parser/app
parity, consent, IPC bounds, watcher disconnects, duplicate controller locks,
worker errors, resident controls, push-to-talk completion, cleanup, recording
publication, HDR failures, replay saves and responsiveness during finalization.
The installed desktop registry also matched all 85 entries in a read-only
comparison on the development machine.

Supplying the existing `JARVIS_VOSK_MODEL`, `JARVIS_WHISPER_MODEL`,
`JARVIS_GTCRN_MODEL` and `JARVIS_SPEECH_TEST_RAW` enables an additional test:
the real Rust recognizer/denoiser consumes the public utterance through a paced
fake microphone and dispatches only a dry-run command. It never opens a real
audio input. Binary linkage and runtime maps are checked for the absence of
Python in the controller. The completed full suite passed all **230 Python
tests**, including the controller integration tests, and the controller's two
Rust activation-state tests passed. Clippy completed with warnings denied.

The controller was then activated in the existing user service. A
[read-only live snapshot](benchmarks/rust-controller-live.json) found about
**6.9 MiB RSS / 4.6 MiB PSS** in the Rust controller, with the Rust voice
worker, denoising and HDR replay active. The effective settings and settings
file were unchanged, and no Python process remained in the service's owned
process tree. This snapshot has a different workload from the controlled
comparison and is not an additional before/after benchmark.

The port needs broader interactive acceptance across desktops, recording
backends, hardware, suspend/resume and long sessions. Differential fixture
coverage does not prove equivalence for every possible desktop entry or Unicode
version. The source archive includes the crate, tables and lockfile; portable
Python wheels omit locally compiled executables.

Implementation references:

- [Tokio process ownership](https://docs.rs/tokio/latest/tokio/process/struct.Command.html)
- [Unix peer credentials](https://docs.rs/tokio/latest/tokio/net/struct.UnixStream.html#method.peer_cred)
- [Bounded latest-value subscriptions](https://docs.rs/tokio/latest/tokio/sync/watch/index.html)
- [Linux parent-death signal semantics](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html)
