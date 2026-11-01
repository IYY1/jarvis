# Rust voice worker

The worker now performs [Whisper small.en recognition](rust-whisper.md) and
[GTCRN denoising](rust-denoiser.md) with Rust audio processing and tract's Rust
CPU kernels. It uses the existing trained ONNX weights and does not link
sherpa-onnx or ONNX Runtime. Vosk wake detection and command boundaries still
use the existing Vosk shared library. The controller validates and dispatches
allowlisted actions; there is no Python interpreter in the Rust voice process.

The initial implementation only wrapped the native recognizers and denoiser.
Its historical measurements appear below; the current computation and resource
comparison are documented in [Rust Whisper](rust-whisper.md).
The later [memory audit and lossless embedding optimization](rust-worker-memory.md)
targets the resident voice worker, the largest Jarvis process in the audit.

Linux prefers the Rust worker when its executable, Vosk library and model are
available. Windows, PocketSphinx and installations without these native assets
use Python. `MURMUR_VOICE_WORKER=rust` forces Rust and reports missing assets;
`MURMUR_VOICE_WORKER=python` forces the compatibility worker. Rust supports Vosk
and the current default Whisper model; it does not port PocketSphinx. General recognition quality still
needs evaluation across speakers and environments.
GTCRN is noise suppression, and does not isolate one speaker from other voices.

## Build and select

Use this source checkout and its editable Python installation. Install a current
stable Rust toolchain (1.91 or later) with Cargo, then build:

```sh
.venv/bin/python scripts/build_voice_worker.py --test
```

The script uses each crate's `Cargo.lock`, runs Rust tests, and installs the
voice worker and standalone Whisper/GTCRN binaries under `src/murmur/bin/`.
Cargo needs network access on its first build; `--offline` uses cached crates.
It does not require sherpa-onnx or download models. Vosk's existing installed
shared library and model are discovered for wake detection at runtime.

Stop an existing daemon before starting a second one. For a foreground daemon:

```sh
MURMUR_VOICE_WORKER=rust ./run-jarvis daemon
```

To use the existing systemd service, add this environment entry through
`systemctl --user edit murmur.service`, then restart that service:

```ini
[Service]
Environment=MURMUR_VOICE_WORKER=rust
```

The environment must belong to the daemon that starts the worker. Setting it
only on a settings window does not change an already-running service. Set it
to `python` and restart to return to Python. Removing the override restores
automatic selection. Explicit
Rust selection reports a missing binary/model instead of silently changing
engines. Microphone consent and mute checks remain enforced.

The local binary is ignored by Git and is not included in portable Python
wheels. Rust sources and the lockfile are included in the source archive;
build the optional worker locally in an editable checkout. There is no new
system package, service registration or model conversion step.

## Initial wrapper comparison (historical)

These numbers describe the initial C-API-wrapper prototype. In particular,
the GTCRN row below does not measure the new Rust neural denoiser.

Measured on 2026-10-22, Ryzen 7 7800X3D, Linux x86-64, Python 3.14.8,
Rust 1.99.1, sherpa-onnx 1.13.8, eight inference threads. All figures are
worker RSS in MiB. Processes were fresh and ran sequentially.

| Scope | Python retained | Rust retained | Python peak | Rust peak |
| --- | ---: | ---: | ---: | ---: |
| Whisper, after 21 fixture variants | 449.2 | 421.2 | 717.8 | 703.9 |
| Whisper + GTCRN, after 21 variants | 453.4 | 424.3 | 721.9 | 690.5 |
| Complete Vosk + Whisper worker, after three commands | 601.2 | 583.2 | 823.8 | 815.9 |

Whisper median decode was 288.2 ms in Python and 286.5 ms in Rust. With GTCRN,
the medians were 375.1 and 377.2 ms. All 21 paired transcripts matched in each
comparison. These small differences do not establish a general latency win.
The full worker recognized the same three commands, with approximately 18 MiB
less retained memory (3.0%). CPU time was 4.30 s in Python and 4.05 s in Rust
over the paced worker runs. These short samples do not establish a CPU improvement.

The full worker used a fake `pw-record` replay of the public `goforward.raw`
fixture with a surrogate wake phrase, `go forward`, followed by the custom
allowlisted command `ten meters`. It exercised real native inference and the
capture loop without opening a microphone or dispatching actions. It excludes
the capture child, daemon and GTK. The isolated test uses the same public
utterance in seven conditions, repeated three times. Neither is an accent,
room-noise, live microphone, long-session or gaming performance evaluation.
The isolated figures include each benchmark process's measurement and fixture
overhead; the complete-worker comparison is more representative of normal use.

Raw results: [Whisper](benchmarks/rust-transcriber.json),
[Whisper + GTCRN](benchmarks/rust-transcriber-denoise.json), and
[complete worker](benchmarks/rust-worker.json).

The measured gain is modest because native model weights and inference scratch
buffers remain. Rust does not remove the resident Whisper model. The larger
earlier allocator savings remain enabled in both implementations.

## Reproduce and verify

Use the checksum-verified [public fixture](speech-research.md#reproducible-public-fixture).
The scripts do not download it or use a microphone:

```sh
.venv/bin/python scripts/benchmark_rust_voice.py --fixture /tmp/goforward.raw
.venv/bin/python scripts/benchmark_rust_voice.py --fixture /tmp/goforward.raw --denoise
.venv/bin/python scripts/benchmark_rust_voice.py --fixture /tmp/goforward.raw --full-worker
MURMUR_VOSK_MODEL="$HOME/.local/share/murmur/models/vosk-model-small-en-us-0.15" \
MURMUR_SPEECH_TEST_RAW=/tmp/goforward.raw \
  .venv/bin/python -m unittest tests.test_rust_voice -v
```

The new tests compare 382 matching inputs with Python, test real Vosk command
and repeated same-breath wake handling, and use a fake microphone to check
listen/cancel priority and capture cleanup. Consent and oversized payloads
are checked before capture. Rust unit tests cover ambiguity, disagreeing
passes, confidence, energy measurement and adaptive gating. The current recognizer tests and resource measurements are documented in
[Rust Whisper](rust-whisper.md). A service integration test selected
the actual Rust executable and accepted its command with dispatch mocked.
Tests using local Unix IPC need a runner that permits local sockets.

`--transcribe`, `--engine`, `--decoder-history` and `--match` are explicit offline diagnostic modes
of the binary; normal daemon launches use no diagnostic argument. The first
payload remains bounded to 1 MiB, control input to 4096 bytes, and utterance
audio to ten seconds. Capture is still one 30 ms frame at a time. The worker
uses Linux parent-death signals and reaps its capture process on shutdown.

The [decoder-history](vosk-decoder-history.md) and
[utterance-finalization](vosk-finalization.md) experiments describe the bounded
offline repetition/heap-trim requests and the internal, default-disabled
finalization flag. Normal controllers omit the experimental flags.

REA's ELF-layout provider was unavailable because no pwntools provider was
configured. Ordinary `nm`/`readelf` inspection confirmed the installed C entry
points and shared dependencies; actual model execution established runtime
compatibility. No REA decompilation evidence is claimed.
