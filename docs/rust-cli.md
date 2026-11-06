# Rust command-line client

The `jarvis` executable in `native/cli` implements command parsing, local IPC,
voice aliases, the persistent Waybar indicator, read-only diagnostics, verified
model installation and desktop/service installation in Rust. These operations
do not import Python or start a Python helper. The CLI shares configuration,
policy and integration code with the Rust controller through its library.

`ui` still opens the existing Python/GTK settings window. `daemon` replaces the
client process with `jarvis-controller`. Screen capture/encoding continues to
use the existing external programs; Vosk wake detection uses `libvosk.so`.
Whisper and GTCRN computation are already Rust in the voice worker.

## Build and use

With Rust 1.91 or newer:

```sh
.venv/bin/python scripts/build_cli.py --test
./run-jarvis --version
./run-jarvis status
./run-jarvis doctor
./run-jarvis alias
./run-jarvis waybar
```

The build installs `src/jarvis/bin/jarvis` atomically. `--offline` uses cached
Cargo dependencies. Building does not restart the running controller, alter
settings, download models, or open audio. The Python build script is a build-time
convenience; `cargo build --release --locked --manifest-path native/cli/Cargo.toml`
also produces the standalone native executable.

The checkout's `run-jarvis` prefers this native client when built. Set
`JARVIS_CLI=python` to select the compatibility client, or `JARVIS_CLI=rust` to
require the native binary. The Python package's `.venv/bin/jarvis` entry point
remains Python. Existing launchers and Waybar configurations using that path
must point to `run-jarvis` or the native executable to use this port. The
[Waybar example](../examples/waybar.jsonc) uses the checkout launcher.

The native CLI talks to either controller through the existing API; controls
do not start a controller automatically. `JARVIS_SOCKET` and XDG locations are
preserved. Peer credentials must match the current user. Requests are limited
to 64 KiB, responses to 1 MiB, and watch events to 64 KiB. Operation-specific
timeouts preserve recorder/replay finalization behavior. Waybar uses one event
subscription and reconnects with a 2–30 second backoff while offline; SIGINT,
SIGTERM and closed output end the client cleanly.

## Setup and diagnostics

```sh
./run-jarvis install-model --basic
./run-jarvis install-model --archive /path/to/vosk-model-small-en-us-0.15.zip
./run-jarvis install-user --service --dry-run
./run-jarvis install-user --service
```

`install-model` uses HTTPS with certificate verification, including redirects,
and the same pinned Vosk, Whisper small.en int8 and GTCRN SHA-256 hashes as the
Python installer. Transfers stream through bounded buffers; archive extraction
rejects traversal, duplicate paths, links and special files. Only the required
Whisper int8 files are extracted. Downloads and unpacked sizes have limits.
Private staging is cleaned on failure, and atomic publication never replaces
an existing path. Existing complete Vosk/Whisper models are reused; the existing
GTCRN file is checksum checked. Incomplete destinations require manual review.
There is no background download or automatic restart after installation.

`install-user` writes a launcher that runs the native CLI and optionally a unit
that runs the Rust controller directly. Build the controller first. Existing
different files require `--replace`; all destinations are checked before any
write. The unit is reloaded but never enabled or started by this command. It
does not remove existing systemd overrides. Dry runs do not write files.

`doctor` checks executable versions, session variables, configuration and model
file presence without loading models or invoking Python. Its JSON identifies
the runtime as Rust. It reports the Python/GTK UI launcher separately and does
not claim to have verified GTK imports. The compatibility diagnostic remains
available for Python/venv troubleshooting:

```sh
JARVIS_CLI=python ./run-jarvis doctor
```

## Verification and memory

```sh
.venv/bin/python -m unittest tests.test_rust_cli -v
.venv/bin/python scripts/benchmark_cli.py --output /tmp/cli.json
```

Rust unit tests cover command validation, application matching, Waybar output,
launcher quoting, checksums, size limits, archive extraction and publication
without replacement. Synthetic ZIP/tar fixtures exercise missing files, links,
duplicates and traversal. Integration tests use temporary sockets/configuration
and fake backends: protocol mappings, aliases against a Rust controller, invalid
responses, reconnects, shutdown and installation conflicts. They run the native
controls with no Python executable on PATH.

The [Waybar comparison](benchmarks/rust-cli.json) measures three fresh sequential
Python/Rust pairs in alternating order, with the service offline and two idle
seconds per client. It excludes voice models, recording, replay and the UI.
This measures the persistent indicator process, rather than memory savings
from short-lived control commands. Both implementations used zero measurable
CPU ticks during the sampled idle periods. The benchmark records individual
RSS/PSS samples and startup-to-first-event times.

| Median, offline Waybar client | Python | Rust |
| --- | ---: | ---: |
| RSS | 16.16 MiB | 3.56 MiB |
| PSS | 9.02 MiB | 1.48 MiB |
| First event | 31.7 ms | 0.85 ms |

The RSS reduction is about **78% for this client alone**. These three-pair
measurements do not measure speech latency, gaming performance or total
application memory. Background desktop activity can affect timings and PSS.

The completed full regression suite passed **244 Python tests**, including
14 CLI tests; the CLI's **9 Rust unit tests** and the controller's **2 Rust unit
tests** also passed. Clippy passed with warnings denied. Source/wheel packaging
was checked for included source/fixtures and excluded compiled binaries.
The [read-only live check](benchmarks/rust-cli-live.json) confirmed native
status/configuration/diagnostics and reuse of all installed models, with the
service PID, settings and denoising/replay state unchanged.

Fresh downloads of the large upstream model archives were not repeated for
this port. Checksums match the existing verified manifests; extraction is
covered with synthetic archives, and local installed models are reused in the
read-only live check. Broader interactive acceptance is still needed for the
desktop environment and long-running sessions. The source archive includes
the native crates, lockfiles and synthetic archive fixtures; portable Python
wheels omit locally compiled binaries.

Implementation references: [ureq HTTPS client](https://docs.rs/ureq/latest/ureq/),
[ZIP archive reader](https://docs.rs/zip/latest/zip/),
[tar reader](https://docs.rs/tar/latest/tar/struct.Archive.html), and
[Rust bzip2 decoder](https://docs.rs/bzip2-rs/latest/bzip2_rs/).
