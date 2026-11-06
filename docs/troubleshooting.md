# Troubleshooting

Run `./run-jarvis doctor` inside the Hyprland session. It performs read-only
dependency checks, never opens input and never starts recording. A binary's
presence is not proof of capture permission or working recognition.

| Symptom | What to check |
| --- | --- |
| No service connection | Choose Start service or start the installed user service. Check `systemctl --user status jarvis.service` and `journalctl --user -u jarvis.service -n 50`. A lock prevents two daemons. |
| “IPC directory must be owned by you” | The runtime `jarvis` directory must be owned by your user with 0700 mode. Do not use a shared directory or run as root. Tests can set `JARVIS_SOCKET` to a private temporary directory. |
| Hyprland/PipeWire unavailable | Run in the actual graphical session, not a restricted build sandbox. Import the session environment to systemd as shown in README. Check `hyprctl version`, `wpctl status`, `pw-dump`. |
| Everything broke after a system update | A Python minor upgrade orphans the venv. `./run-jarvis` prints the rebuild command; see README “One-time setup”. |
| Waybar shows “off” | The service is not running. The module reconnects by itself once it starts. |
| GTK/PyGObject missing | Install distro packages, then recreate/use a `--system-site-packages` venv. `jarvis daemon` does not require GTK. |
| PocketSphinx missing | Use `.venv/bin/python -m pip install -e '.[voice]'`. A source build may need a compiler, CMake and Ninja. No GPU SDK is required. |
| Microphone paused | Complete consent; choose Resume/Unmute. Push-to-talk standby intentionally has closed input until Listen once. Stop listening mutes voice until explicitly resumed; a restart follows saved consent/mode. |
| No level movement | Refresh/select the input node, save, then explicitly start the 15 second input test. Check PipeWire source mute/permissions. A failed stream enters error; use Resume after repairing it. |
| Missed wake | Check the Voice page says “Recognizer: Vosk (improved)”; if not, run `./run-jarvis install-model`. Say “hey Jarvis” (or just “Jarvis”) at the start. Run the microphone test: speech should clearly exceed the energy threshold (default 0.005); lower it if your mic is quiet. |
| Command rejected | Check dictionary/alias warning, command timeout and score threshold. A score is an uncalibrated posterior. Reduce it cautiously after testing; high score alone does not prevent false matches. |
| App name never works | Add a pronounceable exact alias, e.g. “stream video” for Stremio. New apps are discovered on service start, settings save, or when the settings window opens/reconnects. The app must have a visible XDG desktop entry. |
| Ambiguous app | Select a distinct alias mapped to its desktop ID. Repeated windows of one app are focused by most recent focus history. |
| Recorder refuses to start | Complete source/output/audio/permission setup and mark it reviewed. Reconnect a missing monitor or choose its new name. Require wf-recorder and ffprobe. |
| No/incorrect audio | Select an explicit Pulse input or sink-monitor source. Both audio flags require your own combined source; Jarvis does not mix sources. Stop recording before changing capture settings. |
| Black video / permission message | Review Hyprland screencopy policy. A structurally valid stream can still contain a permission-denial screen. Verify playback; do not assume `configured=true` grants permission. |
| Recording CPU too high | Native-resolution CPU encoding can consume multiple cores. Use the tested VA-API preset if your GPU supports it, or reduce resolution outside this initial fixed-monitor backend. Voice standby CPU is separate. |
| VA-API fails | Verify render device, permissions and driver. An FFmpeg encoder listing does not prove driver support. The adapter leaves pixel format selection to wf-recorder for hardware frames; no forced software yuv420p. |
| Partial recording | Preserve the hidden `.part.mkv`; read recovery instructions in recording.md. Crashes never get a successful saved-file message. |
| No indicator over game | Manually add the optional Waybar module and verify your fullscreen bar policy. Voice commands do not spawn/focus an indicator window. A bar hidden by the game cannot promise visibility. |

Configuration errors are reported instead of silently replacing settings.
Back up the user config before manual edits. There is no transcript/debug audio
logging option in this release. Recorder stderr is bounded in memory and used
for actionable errors; logs should still be reviewed before sharing publicly.
