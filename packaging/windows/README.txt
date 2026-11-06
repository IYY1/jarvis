Jarvis 0.4.0 — Windows x64

Windows 10 22H2 and Windows 11 x64. Run Jarvis-0.4.0-Windows-x64-Setup.exe:
it installs for your account only, needs no administrator rights, downloads
nothing and does not restart Windows. Setup upgrades an earlier Jarvis in
place and keeps settings and recordings. Optional: desktop shortcut (on by
default) and start at sign-in (off by default).

Portable ZIP: extract the whole folder before opening Jarvis.exe. Keep
JarvisConsole.exe, _internal, models and tools beside it.

Open Jarvis.exe, choose Start Jarvis, and review Home before allowing your
microphone. Models are included; normal use needs no downloads or paid service.
The notification-area icon remains while the service runs. Its menu can mute,
listen once, save replay, stop recording or quit the service.

Recording: select a display and folder, review system/microphone switches,
then start capture. Settings apply as they change. Start with Compatible
(H.264) if you need broad playback support. The
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

New in 0.4.0: sidebar settings, apply-as-you-change controls, three color
presets, Arabic speech support, dictation, the Clips editor and track mutes.
Separate app tracks on Windows require Windows 11 and support one program
plus the rest of system sound and the microphone. Linux supports up to eight
apps. Arabic uses an optional model download; English models are bundled.

Validation: policy and native-worker tests run on Windows in CI. The release
is built with Windows CPython under Wine; frozen-app checks use a private
test display. Earlier 0.3.x installers passed install/upgrade/reboot/uninstall
checks in a Windows 10 virtual machine. Those earlier results do not establish
0.4.0 behavior. Real microphone input, Windows 11 separate tracks, dictation
on Windows, GPU capture and HDR still require validation on Windows hardware.
This build is unsigned; Windows may show a SmartScreen warning.

License: Jarvis source is MIT. See THIRD-PARTY-NOTICES.txt and licenses/.
The separate FFmpeg executables are GPLv3-or-later; their complete corresponding
source, headers and build recipe accompany this release in
FFmpeg-Corresponding-Source.tar.xz.
