# Build and test container

Jarvis has one supported container use: **building and testing** it. The
image is a Debian trixie toolchain with Rust (plus the `x86_64-pc-windows-gnu`
target), MinGW-w64, Wine, Xvfb and NSIS. It runs the offline Linux test suite
and produces the Windows installer without a Windows machine.

What this container is not:

- **Not a runtime for Jarvis.** Jarvis is a desktop app: it needs the user's
  microphone (WASAPI/PipeWire), GPU screen capture, the Windows Shell or
  Hyprland, and a tray icon. None of these exist inside a Linux or Windows
  container, so no Jarvis component runs in one. End users never need Docker,
  Podman or WSL.
- **Not containerized services on Windows.** Running speech recognition as a
  Docker Desktop/WSL2 service would add a Linux VM (typically 1–2 GB of RAM)
  to save nothing: the native Rust worker already uses less.
- **Not Windows validation.** A Linux container cannot provide a Windows
  kernel, and Wine is not Windows. The installer must still be checked on real
  Windows; see `scripts/windows/validate-installer.ps1` and docs/windows.md.

## Use

```sh
podman build -t jarvis-build -f packaging/container/Containerfile .
podman run --rm -v "$PWD:/src:Z" jarvis-build test
podman run --rm -v "$PWD:/src:Z" -v "$HOME/jarvis-inputs:/inputs:ro,Z" jarvis-build windows
```

`docker` works the same way. `test` runs the Python unit tests, the Rust crate
tests and a Windows cross-compile of the voice worker. `windows` writes
`dist/windows/Jarvis-<version>-Windows-x64-Setup.exe`, its SHA-256 file, the
portable ZIP and the frozen folder.

## Inputs for `windows`

Release inputs are explicit files, never downloaded during the build:

| Path under /inputs | Source |
| --- | --- |
| `python-3.13.17-amd64.exe` | python.org Windows x64 installer |
| `wheels/` | wheels for every line of `packaging/windows/requirements-lock.txt` (`pip download --require-hashes`) |
| `models/` | `jarvis install-model` output: Vosk small en-us 0.15, Whisper small.en int8, `gtcrn_simple.onnx` |
| `ffmpeg-tools/bin/` | `ffmpeg.exe` and `ffprobe.exe` from `scripts/build_windows_ffmpeg.py` |
| `FFmpeg-Corresponding-Source.tar.xz` | `scripts/bundle_windows_sources.py` |

The Wine prefix lives in the container at `/opt/wine` and is created on the
first `windows` run. Mount a volume there to reuse it between runs.
