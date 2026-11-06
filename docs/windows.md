# Windows release

Target: **Windows 10 22H2 and Windows 11, x64**. The release includes a per-user
installer and a portable ZIP. Both carry the same application, offline Vosk and
Whisper models, optional denoising model, the native Rust voice worker,
FFmpeg/ffprobe and license notices. Normal first use does not download models,
install prerequisites or require an API key.

Speech runs in `tools\jarvis-voice.exe`, the same Rust worker as on Linux:
WASAPI microphone capture, Vosk wake detection and Whisper small.en loaded only
after a wake name and released after a minute without a command. The Python
service, tray and settings never import an inference library. This replaced the
0.2.0 Python worker and cut Jarvis' Windows memory substantially; see
[Windows memory](windows-memory.md).

Discord moderation is excluded from the Python service, native controller,
configuration and settings. The removed implementation is preserved only in the
ignored local `build/deferred-discord/snapshot/` folder for future work. Ordinary
“open Discord” app launching remains supported.

## Install and use

Download `Jarvis-0.4.0-Windows-x64-Setup.exe` and run it. Setup installs for the
current Windows account into `%LOCALAPPDATA%\Programs\Jarvis`, needs no
administrator rights, downloads nothing and never restarts Windows. Its pages:
welcome (what Setup will and will not do), MIT license, **components** (Jarvis,
required; *Desktop shortcut*, on by default; *Start Jarvis when I sign in*, off by
default), folder, progress and finish (*Open Jarvis now*, on by default). Start
menu shortcuts and an Apps & features entry with version, publisher, size and
uninstall command are always created. There are no prerequisites to install:
the Python runtime, Visual C++ runtime DLLs, Vosk/MinGW DLLs and models are bundled,
and the Rust helpers use only the Universal C Runtime that ships with Windows 10.

Running Setup over an earlier Jarvis upgrades it in place: Setup asks you to close
an open Settings window (Retry/Cancel), stops the running service through
`JarvisConsole.exe shutdown`, replaces the program folders wholesale (no stale
0.2.0 files remain) and keeps settings, voice names, logs and recordings. If the
service cannot stop, Setup stops before changing anything and says how to quit
Jarvis from the tray. The uninstall entry is written before files are copied, so
an interrupted or cancelled copy can be repaired by running Setup again or
removed from Apps & features. `Setup.exe /S` installs silently with the defaults.

Or extract the entire portable ZIP and open `Jarvis.exe`. Keep
`JarvisConsole.exe`, `_internal/`, `models/` and `tools/` together. Opening Settings does not start capture.
Choose **Start Jarvis**, then review and allow microphone access on **Home** when ready.
The tray menu provides mute/resume, listen once, recording stop, replay save and quit.
Closing Settings leaves the service running.

Account settings and rotating service logs live in `%LOCALAPPDATA%\Jarvis`.
Portable refers to program delivery; it uses the same account settings location.
Recordings default to `~/Videos/Jarvis` and can use a chosen folder. Uninstall
preserves recordings and account settings unless *Also delete my Jarvis settings
and logs* is ticked on its components page; recordings are never deleted.
“Start with Windows” is an explicit per-user Run entry, disabled by default;
Setup's component and `JarvisConsole.exe startup on|off` change the same entry
and setting as Settings.

If Windows blocks the microphone, the voice worker reports it: turn on
Settings > Privacy > Microphone > *Let desktop apps access your microphone*.

`JarvisConsole.exe doctor` runs read-only dependency/model checks.
`JarvisConsole.exe shutdown` stops the service and its capture workers.

## Platform behavior

| Feature | Windows implementation |
| --- | --- |
| Speech and command policy | Shared Python service, Vosk wake detection, int8 Whisper small.en commands |
| Settings and status | Native Tk/ttk window and event-driven notification-area icon |
| Application discovery | Windows Shell installed-app registry; exact aliases and launch words |
| Focus | App user-model identity or verified executable path; no title guessing |
| Desktops | Existing Task View desktops one through ten, via pyvda |
| Fullscreen | Reversible borderless fullscreen for the foreground window |
| Volume | Windows default render endpoint, explicit mute/unmute |
| Recording | DXGI adapter/output discovery and FFmpeg DDA capture |
| System sound and microphone | WASAPI loopback and input, with optional mixing; no virtual cable required |
| Replay | Three reusable private pipes; independent one-second segments in capped RAM or an opt-in Rust disk store |
| Clip playback | Open the output folder and use the Windows default player |

Windows can refuse focus/control of elevated or protected applications. Jarvis
restores fullscreen that it set itself; it does not toggle a game's exclusive
fullscreen mode. Virtual-desktop interfaces vary between Windows builds; failure
is shown as an actionable command error. Create desktops in Task View first.

## Recording choices

Start with **H.264, Automatic GPU, 30 fps, 8 Mbps**. The video budget is about
60 decimal MB/minute, plus sound/container overhead. The FPS slider supports 1–60.
The storage-budget setting controls size independently of frame rate; more frames
at the same bitrate divide the available bits among more frames. Quality mode
offers Medium, High and Very high presets, with variable file size.

Automatic encoding tries the selected adapter's NVIDIA NVENC, AMD AMF or Intel
Quick Sync family, then hardware Media Foundation. A synthetic frame checks the
driver before capture. It never silently switches to CPU encoding. Explicit CPU
compatibility mode uses two-thread ultrafast x264, H.264 SDR only. HEVC and AV1
can reduce bitrate for comparable quality, subject to encoder generation, content
and playback compatibility; this release does not claim equal quality across GPU
vendors or a hardware benchmark it has not run. See the official
[NVIDIA encoding guide](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-video-encoder-api-prog-guide/index.html),
[AMD AMF](https://github.com/GPUOpen-LibrariesAndSDKs/AMF) and
[Intel oneVPL](https://intel.github.io/libvpl/latest/) references.

HDR uses reported DXGI color space and 10-bit HEVC/AV1 with PQ/BT.2020 metadata.
Auto mode refuses an unknown display color state. Known SDR displays cannot be
forced to HDR without enabling HDR in Windows. Final output is verified with
ffprobe before publication; interrupted/failed staging files are retained.

Replay defaults to **RAM**, retaining 10–600 seconds or the configured encoded
history budget (default **256 MiB**), whichever fits first. In RAM mode, video
files are written only after Save. Models, encoder buffers, three in-flight
segments and a concurrently saved snapshot are additional memory. Save waits
for the current segment and can include up to one later second.

Explicit **Disk (continuous writes)** mode sends completed one-second segments
to `jarvis-replay-store.exe`, a Rust helper. `replay.max_mb` (64–16384 MiB) caps
all encoded files it owns, including pending arrivals and files pinned by a save.
It evicts old unpinned history before writing; when a save/pending data occupies
the budget or a disk write fails, capture stops with an error. Snapshots keep
their files through rollover and a concurrent Stop. In-flight named-pipe segments,
encoder buffers and the output clip remain outside this encoded-file cap.
Helper requests have a ten-second deadline covering writes and replies. A stalled
helper is terminated so capture and cleanup can finish; marked crash buffers
are removed on the next replay start. A failed save reports any retained partial
clip's path, including when final HDR/codec verification fails.

`replay.buffer_dir` optionally selects a dedicated directory; the default is
`%LOCALAPPDATA%\Jarvis\replay-buffer`. The adapter applies a protected user-only,
inheritable ACL; Rust also explicitly protects each directory and file before
writing video. The helper refuses reparse points and requires a local NTFS/ReFS
fixed/removable volume. Windows APIs cannot reliably detect third-party RAM disks
that present themselves as fixed NTFS disks; choose a known physical disk.
An exclusive lock protects active buffers, Stop removes their files, and the
next replay start removes marked crash leftovers. Disk mode continuously writes
unsaved screen and selected audio (including microphone audio when enabled),
roughly **1–10 GB/hour** depending on bitrate; 8 Mbps is 3.6 GB/hour plus overhead.
Deletion is not secure erasure; backups/snapshots may preserve data. Saved clips
are separate from the buffer budget. RAM stays available as the privacy default.
Stop replay before changing storage, directory, budget or capture settings.
A quiet system output is represented by timed silence; missing microphone frames
and callback overflows remain errors.

## Build locally

The supported build runs on Linux, either directly or in the
[build container](../packaging/container/README.md):

```sh
python3 scripts/build_windows_release.py --offline --output dist/windows
```

It cross-compiles `jarvis-voice.exe` and `jarvis-replay-store.exe` for
`x86_64-pc-windows-gnu` with MinGW-w64, collects the Rust crates' license files,
freezes the Python service and settings with Windows CPython **3.13.17** and the
hash-pinned [`requirements-lock.txt`](../packaging/windows/requirements-lock.txt)
under an isolated Wine prefix on a private Xvfb display, and packages the result
with NSIS. Inputs are explicit local files: the Wine prefix, NSIS, Xvfb, models,
FFmpeg tools and the FFmpeg corresponding-source archive (`--help` lists the
flags; defaults point at `build/windows/`). No live desktop or microphone is used.

Prepare the pinned FFmpeg/compiler sources in a path without spaces:

```sh
python scripts/prepare_windows_ffmpeg.py --work-dir /tmp/jarvis-ffmpeg-build
python scripts/build_windows_ffmpeg.py --work-dir /tmp/jarvis-ffmpeg-build
```

This builds optimized FFmpeg 8.1 with x264, NVENC, AMF, oneVPL and hardware Media
Foundation support. Network protocols and nonfree components are disabled.
The recipe applies one old-MSVC guard fix to the Intel dispatcher and disables
the unused gfxcapture filter. FFmpeg source is unmodified. The release includes
the corresponding sources, used headers, exact configure command and recipe.
Create the corresponding-source archive with
`python scripts/bundle_windows_sources.py --work-dir <prepared-folder> --output <archive.tar.xz>`.
The additional license texts are in `packaging/windows/licenses/`.

The portable archive includes a file/version/SHA-256 manifest listing only the
Python distributions that are actually bundled. Keep the sibling checksum files
with installer and ZIP when distributing. The build is unsigned; no signing
identity has been supplied, so SmartScreen may warn on first run.

Installer binaries are not committed to Git. Publish them as GitHub release
assets (or another artifact store) together with their `.sha256` files.

## Validation and remaining acceptance

### 0.4.0

This release adds the redesigned settings window and clip editor, dictation,
Arabic support and separate sound tracks. The Linux track list supports up to
eight apps. Windows process loopback separates one program, the rest of system
sound and the microphone, and requires Windows 11 (build 20348 or later).
Arabic is an optional model download; English models are bundled.

The release is built with Windows CPython under Wine on a private display.
Windows CI covers policy, settings and the native voice worker. Frozen-bundle
acceptance covers offline dependency/model discovery, a muted service, dry-run
command dispatch, clean shutdown, microphone-consent refusal and opening and
closing Settings without starting capture. The release notes record the results
for the shipped build.

The 0.3.x VM results below apply to those versions. Windows 11 app-audio capture,
Windows dictation, a real microphone, GPU recording and HDR have not been
validated for 0.4.0 on Windows hardware.

### 0.3.1 on Windows 10 (virtual machine)

`Jarvis-0.3.1-Windows-x64-Setup.exe` (SHA-256
`f67c5c08f5c7891ad6142cfdf39eb5876e527ac5949761bd0dd091d9d900d7ed`, 326 MiB)
bundles both command models and makes Light the default. From a clean
Windows 10 image, the same PowerShell validation installed the published 0.3.0
installer, then upgraded to 0.3.1 while 0.3.0 was running: all 37 checks passed,
including both models installed, kept settings and files, the voice path on the
default Light model, start at sign-in across a reboot, and uninstall. The silent
upgrade took 30 s. [Report](benchmarks/windows-installer-validation-0.3.1-2026-11-01.json);
memory and latency for each model are in [Light command model](light-model.md).

### 0.3.0 on Windows 10 (virtual machine)

The final `Jarvis-0.3.0-Windows-x64-Setup.exe` (SHA-256
`56f9300d5457b38d9d43adc158b914a1f1877a3fb05d9a96e839b8a856b99286`) was checked on
Windows 10 Enterprise Evaluation 22H2 (build 19045) in an isolated KVM guest with
8 GiB RAM and 4 vCPUs, started each time from the same clean disk image: no Jarvis,
no Python on the account's PATH (only the Windows Store aliases), no developer tools
used by the checks. [`scripts/windows/validate-installer.ps1`](../scripts/windows/validate-installer.ps1)
uses only built-in PowerShell. It installs the shipped 0.2.0 installer, starts it,
adds a custom command and user files, then runs 0.3.0 Setup silently while the old
service is running, and checks:

- upgrade exit status, registered version, a complete uninstall entry, removal of the
  0.2.0 Python speech runtime and presence of the Rust worker and Vosk DLLs;
- kept settings, user files and recordings; Start menu and desktop shortcuts;
  start at sign-in off by default;
- `--version`, `doctor` (models, tools, voice worker, Vosk library), Start-menu
  launch opening Settings, Settings alone not starting the service, background start;
- the voice path end to end with the installed Rust worker: paced public fixture,
  wake, command and dry-run dispatch; that no Jarvis process loads a DLL from
  outside Windows or the install folder; shutdown stopping service and worker;
- `startup on`, a real reboot with automatic sign-in and Jarvis started from the
  Run entry, `startup off`, restart after shutdown; silent uninstall removing
  program files, processes, registry entries and shortcuts while keeping settings
  and recordings.

All 36 checks passed on the final installer (and on the build used for the
memory benchmarks). The silent upgrade took 27–29 s. Reports: [installer validation](benchmarks/windows-installer-validation-2026-11-01.json); the memory
benchmark are in [Windows memory](windows-memory.md).

Not covered by these runs: the interactive Setup pages (only silent mode was
driven; the components, Retry/Cancel and finish pages were reviewed in the script
but not clicked through), cancelling mid-copy, a physical microphone, privacy
denial, real apps/desktops/volume, GPU capture and HDR, Windows 11, and physical
hardware timing. The build is unsigned, so SmartScreen may warn.

### Earlier checks (0.2.0, Wine)

Local checks passed: Linux regression tests; 29 Windows policy tests; Windows
structure layouts, account-only IPC ACL, single-instance lock, authenticated
loopback requests, unauthorized-token rejection, muted-service startup/tray/clean
shutdown, named-pipe segment wraparound, generated-video recording and replay
save/finalization, all nine settings pages and friendly choice mapping, verified
window process identity, and Windows Vosk/Whisper transcription of the existing
public recorded fixture. These are Wine results, not Windows hardware results.

### Containers

Jarvis is built and tested in containers; it does not run in one. Linux
containers cannot supply a Windows kernel, and Windows containers lack the
interactive desktop, microphone and GPU that Jarvis needs; see Microsoft's
[container guidance](https://learn.microsoft.com/virtualization/windowscontainers/quick-start/lift-shift-to-containers).
The [build container](../packaging/container/README.md) runs the Linux test suite
and produces the installer; neither establishes Windows behaviour.

### Before public release

Use the available dual boot to check:

- Interactive Setup pages, cancel during copy, and upgrade with Settings open on Windows 10 and 11.
- Microphone permission denial, wake/listen/cancel, mute/resume and worker cleanup with a real microphone.
- Real app discovery/focus, fullscreen, volume, existing desktops.
- SDR GPU capture, quiet system sound, microphone/mixed audio, stop/save and replay.
- Mixed-DPI and multiple adapters/displays; HDR enabled/disabled and output playback.
- Command latency on typical hardware (see the regression in [Windows memory](windows-memory.md)).

This task prepares local artifacts for review and does not publish a release or
claim those remaining checks passed.
