# Changelog

## Unreleased

- Windows: "Primary display" now works. The window offers it, but capture refused
  it with "Choose a connected recording display".
- Windows: recordings and replays now reach the frame rate you choose. Audio that
  started after video made FFmpeg discard frames: 60 fps delivered about 40, and
  165 about 46. Audio is now flowing before capture starts, which also keeps sound
  and picture together. Fast rates improved too: 165 delivers about 150 new frames
  a second instead of about 100, and 240 about 200 instead of about 175.
- Windows: the frame rate is capped at each display's refresh rate, so a 144 Hz
  display no longer spends encoder time and disk on repeated frames.
- Windows: recordings and clips on an HDR display are kept instead of failing
  verification, and all captures label their color correctly. FFmpeg was writing
  the RGB labels that screen capture reports, over the ones Jarvis asked for.
- Windows: HEVC and AV1 work on NVIDIA again. The encoder test used a frame below
  NVENC's minimum size, so both were reported as unavailable. HDR displays were
  affected in every format, because HDR capture needs HEVC or AV1.
- Windows: Store apps such as Notepad and Spotify can now be closed and focused.
  Their windows weren't recognized, so Jarvis said they weren't running.
- "Unmute volume" now unmutes. It used to match the closest phrase, "mute volume".
- Voice (Rust worker): commands can answer about 0.15 seconds after speech stops
  instead of about 0.7, when Vosk and Whisper independently name the same complete
  command. A command that could continue, closing an app, and quitting all still
  wait for the end of speech.
- Voice: after a command it couldn't understand, Jarvis stops waiting about 2.5
  seconds after you stop talking instead of finishing the whole 8-second session,
  and the status keeps what it heard instead of "No command heard".
- Voice (Rust worker): `JARVIS_VOICE_TRACE=<file>` writes what each model heard
  and decided, for diagnosing misheard commands. It records words, never audio.
- Voice (Rust worker): a pause no longer loses a command. Speech right after the
  wake name starts the command instead of being dropped, and "open", a pause,
  then "spotify" now joins into one command. Replies to a command said in one
  breath come about 0.4 seconds sooner. What was heard but not understood is
  now reported when the session ends, not mid-sentence.
- Windows: a tidier window. Home fits on one screen, six sidebar pages use short
  tabs, technical video options fold away, switches replace tick boxes, and the
  title bar matches the window. Memory stays about the same.
- Windows: the Capture page says whether the replay budget holds the chosen
  length, and the app list fills itself when first opened.
- Windows: say so when the replay budget can't hold the chosen length, both
  when replay starts and when a clip comes out shorter than asked.
- Service log: timestamp Windows log lines, and record the exit code and last
  error output of a voice worker that ends unexpectedly.
- Windows: stop logging a traceback when a client disconnects abruptly.
- Build: the installer script now builds on Windows, and the lock file accepts
  the srt archive published on PyPI.

## 0.4.1 — 2026-11-06

- Windows: expose 1–240 fps, 120/240 fps presets, resolution limits, automatic
  bitrate, per-profile video settings for replay, and game folders.
- Windows: implement scaled GPU encoding and verified 10-bit SDR output;
  scaling/conversion use CPU processing, encoding uses the selected GPU.
- Windows: Arabic language/model install controls, dictation start/stop,
  language, quiet timeout, stop phrases and copying held text.
- Windows Clips: keep/remove sections, split, join, undo and preview in player.

## 0.4.0 — 2026-11-06

- **Redesigned settings window** (Linux and Windows): a sidebar with seven
  pages (Home, Voice, Commands, Apps, Capture, Clips, General), one-line
  setting rows with an ⓘ for the details, live status at the top of Home and
  Capture, Mute and a menu in the header, and **Quit Jarvis…** asking first.
  The Apps list is about a third as tall, and Windows shortcuts are a list
  instead of JSON ([details](docs/ui.md)).
- **Settings apply as they change**: no more Save buttons. Switches and lists
  save at once, numbers after a pause, text on Enter or when you leave the
  field; a message confirms each save, errors stay until dismissed.
- **Three color presets** from Sanzo Wada's *A Dictionary of Color
  Combinations*, two dark and one light (General → Colors), each checked for
  WCAG AA contrast.
- **Every app on its own replay track**: with Separate tracks, each app playing
  sound when replay starts, and each app heard before, gets its own track (up to
  eight), so a clip can keep Discord and mute Firefox. Discord's voice shows as
  Discord, not “WEBRTC VoiceEngine”. Linux; Windows separates one program.
- **Windows Clips page**: open saved clips, mute individual tracks, keep only
  one, detect silence, trim start/end and export a new copy using the bundled
  FFmpeg tools. Export and probing do not block settings saves.
- **Clips Sound tracks, LosslessCut-style**: a row per track with its format,
  Keep, **Only this**, Keep all / Mute all; tracks with no sound in the clip are
  marked and start off.
- **Lighter settings window**: closing a clip in the editor now gives its video
  memory back (about 50 MiB for a 1440p clip), and app rows build their
  voice-name fields only when asked.
- **Fixed**: changing Recording sounds (or completing recorder setup) restarted
  a running instant replay and cleared it; those settings no longer restart a
  capture. The voice-model download button also stays hidden now when Arabic
  is on.
- **Arabic commands and dictation** (Voice → Language → Arabic and English),
  using Moonshine Streaming Tiny Arabic (MIT) on Jarvis's Rust/tract runtime.
  Ten models were compared on Saudi TV speech; it was the most accurate
  permissively licensed one (Najdi WER 52% vs 85–93% for multilingual Whisper
  base/tiny). Install with `jarvis install-model --arabic` ([details](docs/arabic.md)).
- **Dictation**: “Jarvis, listen to me”, `jarvis dictate`, Ctrl+D or the new
  buttons type speech into the focused field through the Wayland input method
  (password fields refused) with a virtual-keyboard fallback; a top-of-screen
  bar has Stop/Cancel; focus changes stop typing ([details](docs/dictation.md)).
- **Clips editor**: trim, cut, split, join, choose or mix audio tracks, Fast
  (keyframe) or Exact (re-encoded, HDR kept) export to a new file
  ([details](docs/clips.md)).
- **Separate replay sound tracks**: `replay.audio_tracks: "separate"` keeps the
  game, each app in `replay.separate_apps` and the microphone on their own
  tracks, verified on saved replays.
- **Video formats**: Recommended (AV1, else HEVC) / Compatible (H.264 GPU),
  each tested on the selected display's GPU with synthetic frames. CPU H.264
  and legacy VA-API remain only for existing configurations.
- **Murmur is now Jarvis everywhere**: the Python package (`python -m jarvis`),
  settings (`~/.config/jarvis`), models and data (`~/.local/share/jarvis`), the
  default videos folder (`~/Videos/Jarvis`), `Jarvis_…` file names, `JARVIS_…`
  environment variables, the `jarvis.service` user service and the
  `org.jarvis.Controller` launcher. The first `jarvis` command or service start
  moves the old folders (a rename, nothing is copied), updates settings that
  name them, and replaces the old launcher, service and its overrides;
  existing recordings keep their names.
- **Folder for each game** (on by default): clips and recordings of a focused
  Steam, Heroic, Lutris or Wine game go in a folder named after it.
- **Fixed bitrate by default, matched to resolution and frame rate** until
  you set your own; size control and quality live under Advanced.
- **Commands & apps** is one page: a short “Say this” list on top, every
  phrase and your shortcuts folded away.
- **Close Jarvis** by voice (“close yourself”, “quit Jarvis”, سكّر نفسك) or the
  Quit Jarvis button; a running recording is saved first.
- **Clips: mute tracks** with one switch per track.
- **Dictation stops 3 seconds after you stop talking** (was 20).
- Recorder setup moved to Advanced; Start recording completes it.
- **Settings survive updates**: values a new version rejects are reset one by
  one instead of stopping the service; the original and the previous
  version's settings are kept in `backups/`. **Export/import** with
  `jarvis settings` or Overview → Your settings.
- **Replay's own video settings** (optional; same as recording by default),
  and **Save applies capture changes at once**: a running recording is saved
  and restarted, a running replay restarts, instead of “stop it first”.
- **Capture presets**: Recommended (AV1 where supported), High quality (AV1
  10-bit at the new Maximum quality), High frame rate · 120 fps, Small files ·
  1080p and Compatible · H.264, with format, resolution (display's own, 4K, 2K,
  1080p, 720p), frame rate (up to 240 fps) and bitrate (up to 500 Mbps) under
  Advanced.
- **Arabic switch**: **Turn on Arabic** on Overview and `jarvis language`.
- **Dictation language follows the phrase**: اسمعني dictates Arabic, “listen”
  English; the Start dictation button no longer locks onto Jarvis's own window.
- **Dictation**: “Jarvis, listen” starts it too, and text that can't be typed
  (no text field, focus moved, an app without text input) is put on the
  clipboard so Ctrl+V pastes it; password-field text never is.
- **Separate replay tracks on Windows 11**: the game, one program and the
  microphone on their own tracks through Windows process loopback. Not yet run
  on Windows; Windows 10 is refused with a message.
- **Add programs**: `jarvis add-app` (Linux and Windows) and **Add program…**
  on Windows, which suggests portable programs it finds and hides installers.
- **AV1 10-bit and HEVC 10-bit for SDR** (Choose a format…): no banding in dark
  scenes and gradients, at nearly the size of 8-bit; saved files are verified
  as 10-bit ([details](docs/recording.md)).
- **Apps**: Snap apps are discovered; installers and uninstallers are hidden;
  duplicate native/Flatpak entries show once; AppImages and portable programs
  can be added as normal launcher entries.
- **Settings window**: practical Overview (voice, dictation, capture state and
  the latest action), direct page names, and advanced capture settings folded away.

- **Clip a length**: “clip the last 25 seconds”, “clip last 2 minutes” or
  “save the last minute” saves only the end of the replay buffer (1–59
  seconds or 1–30 minutes); “clip that” still saves all of it.
- **Longer replays**: the buffer can now keep up to 30 minutes (was 10).
  RAM replays hold the whole buffer in memory; use disk storage for long ones.
- **Close words**: Settings → Applications → Close words chooses what you say
  to close an app (“quit Firefox”); several words can be listed.
- **Lower standby memory**: Vosk now finalizes each utterance, freeing decoder
  feature history that grew by about 45 MiB while waiting
  ([measurements](docs/vosk-finalization.md)).
- **No transparent huge pages for the voice worker**: on systems that enable
  them for every program (CachyOS), the worker's idle floor was 16 MiB higher
  at the same speed ([measurements](docs/benchmarks/voice-worker-thp-2026-11-05.json)).

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
