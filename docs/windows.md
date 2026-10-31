# Windows 0.2.0 release candidate

Target: **Windows 10 22H2 and Windows 11, x64**. The release includes a per-user
installer and a portable ZIP. Both carry the same application, offline Vosk and
Whisper models, optional denoising model, FFmpeg/ffprobe and license notices.
Normal first use does not download models or require an API key.

Discord moderation is excluded from the Python service, native controller,
configuration and settings. The removed implementation is preserved only in the
ignored local `build/deferred-discord/snapshot/` folder for future work. Ordinary
“open Discord” app launching remains supported.

## Install and use

Run the installer for your account, or extract the entire portable ZIP and open
`Jarvis.exe`. Keep `JarvisConsole.exe`, `_internal/`, `models/` and `tools/` together.
No administrator privileges are required. Opening Settings does not start capture.
Choose **Start service**, review **Privacy**, then save microphone consent when ready.
The tray menu provides mute/resume, listen once, recording stop, replay save and quit.
Closing Settings leaves the service running.

Account settings and rotating service logs live in `%LOCALAPPDATA%\Jarvis`.
Portable refers to program delivery; it uses the same account settings location.
Recordings default to `~/Videos/Jarvis` and can use a chosen folder. Uninstall
preserves recordings and account settings. “Start with Windows” is an explicit
per-user Run entry, disabled by default.

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

Use Windows x64 CPython **3.13.17** and the hash-pinned
[`requirements-lock.txt`](../packaging/windows/requirements-lock.txt). PyInstaller
must run under Windows Python; this task used an isolated Wine prefix with a private
Xvfb display on Linux. No live host desktop or microphone was used.

On Linux, prepare the pinned FFmpeg/compiler sources in a path without spaces:

```sh
python scripts/prepare_windows_ffmpeg.py --work-dir /tmp/jarvis-ffmpeg-build
python scripts/build_windows_ffmpeg.py --work-dir /tmp/jarvis-ffmpeg-build
```

This builds optimized FFmpeg 8.1 with x264, NVENC, AMF, oneVPL and hardware Media
Foundation support. Network protocols and nonfree components are disabled.
The recipe applies one old-MSVC guard fix to the Intel dispatcher and disables
the unused gfxcapture filter. FFmpeg source is unmodified. The release includes
the corresponding sources, used headers, exact configure command and recipe.

With the offline models, tools and corresponding-source archive prepared, run
from Windows Python with the checkout installed or `src/` on `PYTHONPATH`:

```powershell
python -m pip install --require-hashes -r packaging/windows/requirements-lock.txt
python scripts/build_replay_store.py --test
python scripts/build_windows.py --models C:\assets\models --tools C:\assets\ffmpeg\bin --ffmpeg-source C:\assets\FFmpeg-Corresponding-Source.tar.xz --extra-licenses C:\assets\licenses --output dist\windows
makensis /DBUNDLE=dist\windows\frozen\Jarvis /DOUTPUT=dist\windows\Jarvis-0.2.0-Windows-x64-Setup.exe packaging\windows\installer.nsi
```

Create the corresponding-source archive with
`python scripts/bundle_windows_sources.py --work-dir <prepared-folder> --output <archive.tar.xz>`.
The additional license texts are in `packaging/windows/licenses/`.

Use absolute NSIS input/output paths when invoking it from another directory.
The portable archive includes a file/version/SHA-256 manifest. Keep the sibling
checksum files with installer and ZIP when distributing. The build is unsigned;
no signing identity has been supplied.

## Validation and remaining acceptance

Local checks passed: Linux regression tests; 29 Windows policy tests; Windows
structure layouts, account-only IPC ACL, single-instance lock, authenticated
loopback requests, unauthorized-token rejection, muted-service startup/tray/clean
shutdown, named-pipe segment wraparound, generated-video recording and replay
save/finalization, all nine settings pages and friendly choice mapping, verified
window process identity, and Windows Vosk/Whisper transcription of the existing
public recorded fixture. The frozen portable app and installed app both pass
model/tool discovery, muted startup, dry-run dispatch, consent refusal, native
settings and wait-for-exit shutdown. Silent installation verifies all 1,182
manifest file hashes; uninstall removes program files and preserves an unrelated
user file. A temporary test loader also successfully transcribes the saved fixture
using the shipped Python archive, DLLs and bundled Vosk/Whisper models; that loader
is excluded from the release. These are Wine results, not Windows hardware results.

Linux containers cannot supply a Windows kernel. Windows containers also lack
the interactive desktop needed for these hardware checks; see Microsoft's
[container guidance](https://learn.microsoft.com/virtualization/windowscontainers/quick-start/lift-shift-to-containers).
Wine isolates local compatibility tests, but cannot establish GPU, WASAPI,
monitor-control or Windows Shell behavior on the shipping OS.

Before public release, use the available dual boot to check:

- Installer, portable launch, update/uninstall and startup registration on Windows 10 and 11.
- Microphone permission denial, wake/listen/cancel, mute/resume and worker cleanup.
- Real app discovery/focus, fullscreen, volume, existing desktops.
- SDR GPU capture, quiet system sound, microphone/mixed audio, stop/save and replay.
- Mixed-DPI and multiple adapters/displays; HDR enabled/disabled and output playback.

This task prepares local artifacts for review and does not publish a release or
claim those remaining checks passed.
