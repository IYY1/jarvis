# Jarvis

Offline voice control for Linux with Hyprland and Windows 10/11. Say
**“Jarvis, open Firefox”**, **“Jarvis, go to workspace three”**, or
**“Jarvis, start recording”**.

Everything runs on your CPU: Vosk listens for the wake word and Whisper small.en
understands the command. No account, API key or cloud service. The settings
window can close while Jarvis keeps listening in the background.

Jarvis is early software. Recognition depends on your voice, microphone and room.

## What's new in 0.3.1

- **Lighter by default**: commands now use Whisper tiny.en, which halves the
  memory Jarvis needs while you give commands (546 → 280 MiB on Linux) and
  answers faster. Settings → Voice → Command model → Small switches back to the
  more accurate model ([details](docs/light-model.md)).

## What's new in 0.3.0

- **Much lighter on Windows**: speech now runs in the same native Rust worker as
  on Linux, so Jarvis waits for its name in about 165 MiB instead of about
  600 MiB ([measurements](docs/windows-memory.md)).
- **One setup file for Windows** with a desktop-shortcut and start-at-sign-in
  choice, clean in-place upgrades, and an uninstaller that can keep or delete
  your settings.
- **Fixed**: Jarvis could hang on shutdown while the settings window or the
  Waybar module was open.
- **Removed**: monitor brightness by voice. Old settings files still load.

See the [changelog](CHANGELOG.md) for everything else.

## Features

- Open or focus installed applications, with exact voice aliases.
- Switch workspaces, move a window, set fullscreen and adjust volume.
- Start and stop preconfigured screen recording, with automatic HDR detection.
- Optional instant replay: the last two minutes of screen, game sound and
  microphone stay in RAM; “clip that” saves them with a confirmation chirp
  (needs `gpu-screen-recorder`; idea from [Kairos](https://github.com/BasilForge/Kairos)).
- A Clips page that plays saved clips in the window (needs `gst-plugins-good`).
- Choose GPU AV1, HEVC or H.264, simple quality/bitrate budgets and an FPS slider with storage estimates.
- Preserve HDR in recordings and replays with 10-bit HEVC or AV1 and validated PQ/BT.2020 color metadata.
- Configure microphone, recognition, aliases, recording and startup in GTK.
- Mute immediately; the voice worker and microphone capture are released.
- Optional event-driven Waybar indicator and keyboard shortcuts.

Speech maps to allowlisted actions. It cannot run arbitrary shell commands.
Audio and transcripts are not saved by voice recognition. Screen recordings,
including optional microphone tracks, are separate deliberate user output.

## Install

### Arch Linux: one line

On Arch Linux, CachyOS or another Arch-based system, run as your normal user:

```sh
curl -fsSL https://raw.githubusercontent.com/abdullaho21/jarvis/main/install.sh | bash
```

[`install.sh`](install.sh) installs missing packages with pacman (asking for
your password), downloads Jarvis into `~/.local/share/jarvis`, builds the Rust
parts, downloads and verifies the voice models (about 790 MB), and adds an app
launcher, a `jarvis` command and a disabled user service. It does not edit
Hyprland. Run the same line again to update.

Options go after `bash -s --`, for example `... | bash -s -- --gpu-recorder`:

| Option | Effect |
| --- | --- |
| `--gpu-recorder` | Also install `gpu-screen-recorder` for GPU formats, HDR and instant replay. Review its privileged capture helper in [recording setup](docs/recording.md) first. |
| `--basic-models` | Download only the 40 MB wake model; commands are less accurate. |
| `--python-only` | Skip the Rust builds and use the Python worker and client. |
| `--uninstall` | Remove Jarvis; settings, models and recordings are kept. |
| `--help` | List every option, including `--dir`, `--branch`, `--replace` and `--yes`. |

To read the script first: `curl -fsSLO https://raw.githubusercontent.com/abdullaho21/jarvis/main/install.sh`,
then `bash install.sh`.

### Windows 10/11

Download `Jarvis-0.3.1-Windows-x64-Setup.exe` from the
[releases page](https://github.com/abdullaho21/jarvis/releases) and run it. It
installs for your account only, with no administrator rights, downloads or
restart, and upgrades an earlier Jarvis in place. A portable ZIP is also
available. The installer passed its install, upgrade, voice, restart and
uninstall checks on Windows 10 in a virtual machine, not yet on physical
hardware; see [Windows setup](docs/windows.md) for details and known limits.

### By hand from a checkout

You need Python 3.11+, GTK 4 with PyGObject, PipeWire/WirePlumber, Hyprland
and systemd user services. Recording needs FFmpeg and either `wf-recorder`
(SDR) or `gpu-screen-recorder` (GPU formats, HDR and instant replay). Use your
distribution's GTK; do not build a second GTK with pip. On Arch/CachyOS:

```sh
sudo pacman -S --needed python python-gobject gtk4 python-setuptools \
  pipewire pipewire-pulse wireplumber wf-recorder ffmpeg base-devel cmake ninja
```

From the checkout directory:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e '.[voice]'
./run-jarvis install-model
.venv/bin/python scripts/install_user.py --dry-run --service
.venv/bin/python scripts/install_user.py --service
./run-jarvis
```

The model download is about 790 MB and checked against pinned SHA-256 hashes.
`install_user.py` creates the app launcher and a disabled user service; review
an existing installation before using `--replace`. Keep the checkout and venv
where they are. Optional Rust parts are described under [Development](#development).

After a system Python upgrade, rebuild the venv;
`MURMUR_CLI=python ./run-jarvis doctor` reports the mismatch.

### First run

In **Privacy & startup**, allow the microphone, then unmute. The default wake
phrase is **“hey Jarvis”**; plain **“Jarvis”** also works. The command can
follow in the same breath or after a pause. Closing settings keeps Jarvis
running; Mute closes voice input.

In **Voice & microphone**, add other wake names separated by commas, such as
**Alfred**, up to eight. Without the downloaded models, the PocketSphinx
fallback recognizes less accurately.

## Commands and app aliases

| Action | Examples |
| --- | --- |
| Applications | “open Firefox”, “launch Dolphin”, “switch to Steam” |
| Workspaces | “go to workspace three”, “move this window to workspace two” |
| Fullscreen | “make this window fullscreen”, “exit fullscreen” |
| Volume | “turn the volume down”, “mute volume”, “turn the sound back on” |
| Recording | “start recording”, “stop recording”, “open the video recorder” |
| Instant replay | “clip that”, “save replay”, “start replay”, “stop replay” |
| Voice control | “cancel”, “stop listening” |

The **Commands** page has the full list. **Applications → Add voice name** maps
a phrase to an installed app, or from a terminal (`./run-jarvis` in a checkout):

```sh
jarvis alias streamio stremio
jarvis alias
jarvis alias streamio --remove
```

Each voice name has a **Launch word**, initially `open`. Change it to `watch`
for “Alfred, watch video”, or clear it for “Jarvis, Firefox”. A `the` after the
launch word is ignored. A wake name (or Listen once) is still required.

An alias selects the application; it never adds an executable. Close matches
must clearly beat other commands. If both native and Flatpak copies exist,
Jarvis prefers the native one; use an alias to pick the other.

## Background startup and recording

After installing the user service, enable **Start at login** in Privacy &
startup. The service follows `graphical-session.target`, so your session must
start that target (for example with uwsm) and export its environment:

```sh
systemctl --user import-environment WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_CURRENT_DESKTOP XDG_RUNTIME_DIR
systemctl --user start murmur.service
```

`./run-jarvis daemon` runs the service in the foreground; run one at a time.
The service, package and storage paths keep the earlier name Murmur, so older
settings, models and recordings still work. Optional [Waybar](examples/waybar.jsonc)
and [Hyprland](examples/hyprland.conf) examples are never installed automatically.

Before recording, choose the output, folder, encoder and audio in **Recorder**,
test a short clip and save. CPU encoding costs far more than voice standby; see
[recording setup](docs/recording.md). Muting voice control does not mute a
microphone track that is already recording.

## Performance

Linux, Rust voice worker with noise suppression, Ryzen 7 7800X3D.

| | Result |
| --- | ---: |
| RAM while waiting for the wake word | **156 MiB** (was 469 MiB) |
| Peak RAM during a command | **553 MiB** |
| Command, from end of speech to action | **about 1 s** |
| Ready after starting | **0.2 s** |
| Background CPU | **0.2–0.6%** of one core |

Whisper loads when Jarvis hears its name and unloads after 60 seconds without a
command. These are fixture and short live measurements on one machine, not
accuracy or gaming FPS tests. Methods, dates and limits:
[performance](docs/performance.md) and [on-demand Whisper](docs/whisper-on-demand.md).

## Development

On Linux, Jarvis prefers its Rust parts when they are built. Each falls back to
Python:

| Part | Build | Notes |
| --- | --- | --- |
| [Voice worker](docs/rust-voice.md) (Whisper and noise suppression) | `scripts/build_voice_worker.py` | `MURMUR_VOICE_WORKER=python` selects Python |
| [Command-line client](docs/rust-cli.md) | `scripts/build_cli.py --test` | `MURMUR_CLI=python` selects Python |
| [Background controller](docs/rust-controller.md) | `scripts/build_controller.py` | Opt in with `MURMUR_CONTROLLER=rust` |

The settings window and the Windows service use Python; Windows releases
ship only the Rust voice worker. Tests and checks:

```sh
.venv/bin/python -m unittest discover -s tests -v
./run-jarvis doctor
```

Regular tests never open the microphone or capture the screen. [GitHub
Actions](https://github.com/abdullaho21/jarvis/actions) runs them on every push.

## Project layout

```text
src/murmur/       Voice engine, service, desktop adapters and GTK UI
native/           Rust voice worker, Whisper, denoiser, controller and CLI
tests/            Policy, lifecycle, model and regression tests
scripts/          Installer, build scripts, integration checks and benchmarks
packaging/        Windows installer, PyInstaller files and the build container
examples/         Portable configuration, Hyprland and Waybar examples
docs/             Architecture, setup, verification and review notes
docs/benchmarks/  Raw measurements, with workload boundaries
```

[Documentation index](docs/README.md) · [Changelog](CHANGELOG.md) ·
[Contributing](CONTRIBUTING.md) · [Security boundaries](SECURITY.md)

## License

Jarvis is licensed under [MIT](LICENSE). Models and dependencies keep their own
licenses; see [dependency and model notices](docs/dependencies.md).
