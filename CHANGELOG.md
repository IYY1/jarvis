# Changelog

## 0.3.1 — 2026-11-01

- **Light command model, now the default**: commands use Whisper tiny.en
  instead of small.en. On Linux the voice worker's peak fell from 546 to
  280 MiB and commands finished in 0.16 s instead of 0.33 s after speech.
  Settings → Voice → Command model → Small switches back
  ([details](docs/light-model.md)). Model downloads and the Windows installer
  include both models (about 790 MB instead of 680 MB); if only one is
  installed, Jarvis uses it.

## 0.3.0 — 2026-11-01

### Windows

- **Native voice worker**: speech recognition uses the same Rust worker as
  Linux, with WASAPI microphone capture. Whisper loads only after the wake word
  and unloads after 60 seconds idle. Measured on Windows 10, the whole app waits
  in 165 MiB instead of 599 MiB (−72%), peaks at 533 instead of 777 MiB, and is
  listening 1.2 s after starting instead of 2.4 s
  ([measurements](docs/windows-memory.md)). Each command takes about 0.3 s
  longer to recognize; this regression is under investigation.
- **One setup file**: choose a desktop shortcut and start at sign-in; upgrades
  replace the old program files and keep settings and recordings; uninstall can
  optionally delete settings. `JarvisConsole.exe startup on|off` added.
- Smaller bundle: ONNX Runtime, sherpa-onnx and PocketSphinx are no longer
  shipped on Windows.
- Validated on Windows 10 in a virtual machine (install, upgrade from 0.2.0,
  voice, reboot with start at sign-in, uninstall); physical hardware and
  Windows 11 are not yet tested.

### All platforms

- **Fixed**: the service could not stop while a status subscriber (the settings
  window, the Waybar module) was connected.
- **Removed**: monitor brightness by voice, which was unreliable (unknown
  monitor names for the wake model, dropped DDC/CI writes). The Arch installer
  no longer installs `ddcutil`. Existing settings files still load.
- Build and test container in `packaging/container/` for the Linux tests and the
  Windows installer.

## 0.2.0 — 2026-10-26

### New

- **Windows 10/11 x64**: per-user installer and portable ZIP with the offline
  models, FFmpeg and license notices. Native settings window, tray icon,
  microphone and system-sound capture, GPU recording and an in-RAM instant
  replay. Tested under Wine, not yet on real Windows hardware; see
  [Windows setup](docs/windows.md).
- **Brightness by voice**: name each monitor in **Displays**, pick a main
  monitor and say “dim Samsung” or “set KTC brightness to 60 percent”. Uses
  `ddcutil` on Linux and WMI or DDC/CI on Windows.
- **Custom wake names**: up to eight, such as “Alfred” alongside “Jarvis”.
- **Launch words**: each app voice name can use its own verb (“watch video”)
  or none (“Jarvis, Firefox”).
- **Recording choices**: GPU H.264, HEVC or AV1, size or quality budgets, an
  FPS slider with storage estimates, and HDR with AV1 as well as HEVC.
- **One-line Arch Linux installer** (`install.sh`) that also updates and
  uninstalls.

### Faster and lighter

- Whisper recognition and GTCRN noise suppression run in a Rust voice worker,
  preferred on Linux when built.
- Whisper loads only after the wake word and unloads after 60 seconds idle:
  RAM while waiting fell from 469 to 156 MiB, and the worker is ready in
  0.2 s instead of 1.9 s.
- Whisper model loading streams its weights: startup peak fell from 529 to
  344 MiB.
- The idle worker returns freed memory every 10 seconds, which reduces RAM
  creep during long sessions.
- Optional Rust background controller and a Rust `jarvis` command-line client.

## 0.1.0 — 2026-10-21

First public version: offline voice control for Hyprland on Linux with Vosk
wake detection and Whisper small.en commands, app launching and aliases,
workspaces, fullscreen, volume, screen recording with HDR, instant replay and
a Clips page, a GTK settings window and an optional user service.
