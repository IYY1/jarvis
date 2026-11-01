Jarvis 0.3.1 — Windows x64 release candidate

Windows 10 22H2 and Windows 11 x64. Run Jarvis-0.3.1-Windows-x64-Setup.exe:
it installs for your account only, needs no administrator rights, downloads
nothing and does not restart Windows. Setup upgrades an earlier Jarvis in
place and keeps settings and recordings. Optional: desktop shortcut (on by
default) and start at sign-in (off by default).

Portable ZIP: extract the whole folder before opening Jarvis.exe. Keep
JarvisConsole.exe, _internal, models and tools beside it.

Open Jarvis.exe, choose Start service, and review Privacy before enabling your
microphone. Models are included; normal use needs no downloads or paid service.
The notification-area icon remains while the service runs. Its menu can mute,
listen once, save replay, stop recording or quit the service.

Recording: select a display and folder, review system/microphone switches,
then save settings. Start with H.264, auto encoder, 30 fps and 8 Mbps. The
video budget is about 60 MB per minute plus sound/container overhead. HEVC
and AV1 usually offer better compression but need compatible graphics hardware
and playback software. CPU H.264 is an explicit compatibility option.

Replay defaults to RAM, capped by history and the encoded-video budget
(256 MiB by default). Other process and in-flight/save memory is additional.
Explicit Disk mode continuously writes unsaved screen and selected audio to
a private rolling buffer, including microphone audio when enabled. Writes
follow bitrate (roughly 1–10 GB/hour; 8 Mbps is 3.6 GB/hour plus overhead).
Stop deletes buffers; crash leftovers are cleaned on next replay start.
Deletion is not secure erasure. Disk mode needs the included Rust helper and
a local NTFS/ReFS disk; its byte cap includes pending and save-pinned files.
Saving completes the current second and may include one later second.

Settings/logs: %LOCALAPPDATA%\Jarvis
Recordings: the folder you selected (default %USERPROFILE%\Videos\Jarvis).
Portable mode keeps account settings in the same location as the installer.
Uninstall preserves account settings and recordings unless you tick
"Also delete my Jarvis settings and logs". Recordings are never deleted.

Speech recognition runs in tools\jarvis-voice.exe (native, offline). If
Windows blocks the microphone, turn on Settings > Privacy > Microphone >
"Let desktop apps access your microphone".

Console examples:
  JarvisConsole.exe --version
  JarvisConsole.exe startup on|off
  JarvisConsole.exe doctor
  JarvisConsole.exe status
  JarvisConsole.exe mute
  JarvisConsole.exe shutdown

No Discord moderation is included. The same Windows app can still be opened
by name like other installed applications.

Validation: install, upgrade from 0.2.0, first launch, the voice path with
injected test audio, start at sign-in, restart and uninstall were checked on
Windows 10 22H2 in a virtual machine (see docs/windows.md). Physical hardware
acceptance is still required before public release:
microphone consent/mute, app focus, virtual desktops,
GPU capture, system/microphone sound, mixed-DPI displays and HDR.
Wine and containers cannot establish those hardware results. This build is
unsigned and has not been publicly published by this task.

License: Jarvis source is MIT. See THIRD-PARTY-NOTICES.txt and licenses/.
The separate FFmpeg executables are GPLv3-or-later; their complete corresponding
source, headers and build recipe accompany this release in
FFmpeg-Corresponding-Source.tar.xz.
