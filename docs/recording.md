# Recording without leaving a game

Jarvis uses `gpu-screen-recorder` for GPU H.264, HEVC and AV1 recording, HDR and instant replay. Existing H.264 CPU/legacy VA-API settings continue to use `wf-recorder` for SDR. Opening recording settings and starting capture are separate actions. The start action launches no window, portal, region selector, or terminal; it names the configured monitor explicitly and disconnects standard input. A compositor permission prompt is still possible if permission has not been established. Complete and test setup outside a game first.

The desktop controls are the normal route. For integration checks with the daemon already running, use `murmur ui --page recording` to open setup, `murmur record start` to capture, and `murmur record stop` to save. `murmur config` shows the current settings. In a source checkout, prefix the equivalent invocations with `PYTHONPATH=src .venv/bin/python -m murmur`.

## Backend choice and limits

| Approach | License and maintenance evidence checked 2026-10-20 | Resource and permission tradeoff | Decision |
| --- | --- | --- | --- |
| [wf-recorder](https://github.com/ammen99/wf-recorder) | MIT; upstream commits visible in 2026; installed version 0.6.0 | Direct Wayland screencopy; CPU encoding or optional VA-API. No portal source picker when an output is named, but compositor permission rules apply. | Implemented: already installed, small subprocess interface, no new privileged helper. |
| [GPU Screen Recorder](https://git.dec05eba.com/gpu-screen-recorder/about/) | GPL-3.0-only; maintained upstream with current CLI, signals and Unix-socket control | Hardware capture/encoding suits gaming, but monitor capture uses a privileged KMS helper. Portal capture avoids that helper and may require source selection. Its low-overhead claims are upstream claims, not Jarvis measurements. | Used for GPU recording, HDR and [instant replay](#instant-replay) (optional package). |
| [ScreenCast portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html) | Interface provided by the desktop's installed portal implementation | Normally presents source/permission UI. Persistence depends on the backend and permission grant; restore tokens rotate after each successful session. Missing sources or revoked permission can cause another prompt. | No generic promise of unattended capture; no portal backend in this release. |

The capture backend performs video encoding; it is independent of speech recognition. Choosing VA-API deliberately uses the GPU's media engine and may also use graphics copies/conversion. The default voice path still performs no GPU inference.

The installed wf-recorder help/man page and [upstream usage](https://github.com/ammen99/wf-recorder#usage) establish the fixed-output, codec-parameter, explicit-audio and Ctrl+C stop interfaces used by the adapter. It accepts one audio capture source. Jarvis does not create hidden audio routing or pretend that checking two boxes mixes microphone and desktop audio.

## First-run setup

1. Open Recording settings and select a connected monitor. The source list comes from `hyprctl -j monitors`. A disconnected source causes a clear error; Jarvis does not silently record a different screen.
2. Choose an output folder with sufficient space. Jarvis creates it when capture starts, reserves each new file with mode `0600`, and does not overwrite recordings.
3. Select no audio, microphone, or system audio. Audio is off by default. For microphone, select the Pulse source name of the input. For system audio, choose the output sink's `.monitor` source. PipeWire systems normally provide these through `pipewire-pulse` and `pactl`.
4. To record both, create/review your own combined audio source before gaming, select it explicitly, and set `combined_audio_source: true`. That routing is outside this initial release. Selecting both without this acknowledgment fails safely; it does not silently omit either input.
5. Choose a format, size control and frame rate under **Picture & storage**. Start with 30 fps. Hardware AV1 is an efficient option on supported recent GPUs; choose H.264 for broad sharing compatibility. Test the chosen GPU format and driver before gaming.
6. Check compositor capture permission, make a short test clip, and play it back. Confirm that it shows the intended screen and audio, produces no permission prompt on a second capture, and leaves focus on the game. Then mark recording setup complete (`configured: true`). This flag records your setup acknowledgment; it does not grant permissions or prove future sessions will remain permitted.

[Hyprland's permission documentation](https://wiki.hypr.land/configuring/core/advanced-configuration/permissions/) describes screencopy allow/ask/deny rules. In current Lua configuration, an explicit grant looks like:

```lua
hl.permission({ binary = "/usr/bin/wf-recorder", type = "screencopy", mode = "allow" })
```

Review that grant before adding it: it allows the named recorder to capture without asking. Use the actual installed executable path. Permission rule changes require a Hyprland restart; a normal config reload is insufficient. Older releases use different syntax; consult that installed version's wiki. Jarvis does not edit your Hyprland configuration or change enforcement. An enforced denial can yield a valid black video containing a permission message, which structural file validation cannot distinguish from ordinary content. Visual first-run verification is necessary.

## Simple picture and storage controls

The Recorder page provides a 1–60 FPS slider, five encoder paths, three GPU quality levels and an optional video bitrate budget (1–100 Mbps). These settings also apply to instant replay. There are no profile, GOP, B-frame or lookahead controls to configure.

| Format | Use when | Backend |
| --- | --- | --- |
| AV1 GPU | Smaller files at comparable quality, with a recent GPU and compatible player/editor | GPU Screen Recorder |
| HEVC GPU | Efficient storage with HEVC-aware tools | GPU Screen Recorder |
| H.264 GPU | Easy sharing and editing across devices | GPU Screen Recorder |
| H.264 CPU | Keep the existing compatible software path | wf-recorder, two x264 threads, ultrafast |
| H.264 VA-API (legacy) | Keep an existing tested render-device setup | wf-recorder |

The [OBS format guide](https://obsproject.com/kb/audio-video-formats-guide) ranks available hardware encoders AV1, then HEVC, then H.264 for high-quality local recording and explains the compatibility tradeoffs. That is a practical starting point, not a guarantee for every GPU generation or scene. Software AV1 can be CPU intensive, so Jarvis offers GPU AV1 and never silently falls back to CPU encoding. An FFmpeg codec listing does not prove hardware support. New GPU choices are checked against the codec reported by `ffprobe` before a file is reported saved.

**Consistent quality** lets bitrate and file size vary with content. GPU Compact / Balanced / Sharp map to the backend's `medium` / `high` / `very_high` QP presets. The legacy numeric H.264 value remains under advanced tuning. Existing configurations retain their encoder, CRF/QP and default GPU quality behavior.

**Storage budget** sets a video bitrate, including for the RAM replay buffer. GPU Screen Recorder uses `-bm cbr -q <kbps>`, following its [manual's replay guidance](https://man.archlinux.org/man/gpu-screen-recorder.1.en). Legacy wf-recorder paths use FFmpeg bitrate, maxrate and a two-second encoder buffer. These are rate-control targets, not strict file-size or total-memory caps.

| UI preset (1080p starting points) | Encoder | FPS | Video budget | Estimated video size |
| --- | --- | --- | --- | --- |
| Small | AV1 GPU | 30 | 4 Mbps | 30 MB/min, 1.8 GB/hour |
| Balanced | AV1 GPU | 30 | 8 Mbps | 60 MB/min, 3.6 GB/hour |
| Smooth | AV1 GPU | 60 | 16 Mbps | 120 MB/min, 7.2 GB/hour |

Audio, muxing, driver buffers and rate-control variation add overhead. The estimate is decimal MB/GB: `Mbps × seconds / 8`. At 8 Mbps, a 120-second replay contains roughly 120 MB of video, plus that overhead. Higher resolutions and busy scenes may need more bitrate. At a fixed bitrate, raising FPS shares the same budget among more frames; it does not automatically double file size. In quality mode, higher FPS can raise storage use. FPS also affects capture/encoding work; 30 is the conservative starting point.

Example: `recording.codec: "av1"`, `fps: 30`, `rate_control: "bitrate"`, `bitrate_mbps: 8`. GPU Screen Recorder chooses the actual hardware encoder for the selected GPU. An unsupported GPU/driver produces an error; Jarvis does not substitute a slower software AV1 encoder. Codec choice, quality and bitrate are sampled at start; restart recording/replay after changing settings.

## Practical presets

```json
{
  "recording": {
    "configured": false,
    "monitor": "DP-5",
    "output_dir": "~/Videos/Murmur",
    "system_audio": false,
    "microphone": false,
    "audio_device": "",
    "fps": 30,
    "codec": "libx264",
    "quality": 23
  }
}
```

The CPU preset uses H.264, `preset=ultrafast`, two encoder threads, 30 fps, CRF 23 and Matroska. These are engineering choices to limit encoding work, not measured promises of gaming impact. It records the selected monitor at its native dimensions. `quality` is CRF 0–51 for libx264; lower values increase quality and file size. `ultrafast` can produce large files.

The optional SDR hardware preset uses `codec: "h264_vaapi"`, `vaapi_device: "/dev/dri/renderD128"`, 30 fps, and `quality` as the encoder's QP value. Choose the correct render device; a multi-GPU machine may expose several. Device visibility and an FFmpeg encoder listing do not prove the driver can encode successfully. Perform a test clip before gaming. HDR uses the separate path below.

`--framerate 30` asks wf-recorder for constant-frame-rate output. Its default compositor damage handling remains enabled; no extra rendering timer runs in the voice daemon. Measure both the recorder child and the compositor/game, since a frame-rate setting alone does not establish capture cost.

## HDR recording and replay

**Color / HDR** in Recorder applies to both recording and instant replay, through `recording.color_mode`:

- **Automatic** (`auto`, the default for existing configurations): read the selected monitor's `colorManagementPreset` from `hyprctl -j monitors` at capture start. `hdr` and `hdredid` select HDR; SDR and missing presets retain the SDR path. A 10-bit pixel format or HDR-capable display alone does not establish active HDR.
- **Preserve HDR** (`hdr`): explicitly request HDR, including on older Hyprland versions without the preset field or when fullscreen HDR passthrough is in use. Enable HDR on the display first.
- **SDR** (`sdr`): use SDR capture. If the monitor reports an HDR preset, starting fails with instructions to select HDR or disable display HDR. Jarvis does not tone map direct monitor capture into SDR.

HDR uses GPU Screen Recorder's `-k av1_hdr` when AV1 is selected, otherwise `-k hevc_hdr`, with limited color range and no CPU fallback. It honors the selected GPU quality preset or bitrate budget (the existing default remains `very_high`). The backend handles pixel conversion and HDR metadata; changing only output color tags would not preserve HDR. Recordings remain Matroska with private staging files, SIGINT finalization and recovery; saved replays remain MP4; RAM is the default and disk buffering is an explicit choice below. The legacy numeric software/VA-API quality and render-device settings apply to SDR recording only. HDR recording still uses the explicitly selected recording audio source; replay uses its own game/microphone switches.

`ffprobe` checks HDR output for a valid 10-bit video stream with PQ (`smpte2084`), BT.2020 primaries and matrix, and limited range before reporting success. An unsupported encoder, missing metadata or silent SDR fallback is an error; recording partials and replay files are retained for inspection. Structural verification does not prove visual color accuracy or permission success.

Restart recording/replay after changing display color mode. Detection is a snapshot at start; it does not follow fullscreen or display HDR changes during a running buffer. Use Preserve HDR before starting a session intended for fullscreen HDR. HDR needs a GPU/driver with the selected HEVC or AV1 10-bit encoding, a compatible monitor-capture backend and GPU Screen Recorder's installed KMS helper. Both recording and replay can run simultaneously, but use separate encoder sessions.

The built-in GTK player may not support HDR output or tone mapping. Use **Open in your player** with an HDR-aware player/display, or enable the player's HDR-to-SDR tone mapping for an SDR display. A washed-out preview alone does not establish that the saved video lost HDR.

The [GPU Screen Recorder documentation](https://git.dec05eba.com/gpu-screen-recorder/about/) and installed man page document HEVC HDR capture and its Wayland monitor requirements. [Hyprland's color documentation](https://wiki.hypr.land/configuring/core/monitors/colors/) distinguishes HDR PQ presets from wide-gamut SDR and describes fullscreen HDR switching.

Verified on 2026-10-21: automatic mode on DP-3 (`colorManagementPreset: hdr`) produced both a 2.90-second recording and a 2.87-second replay at 2560×1440. Both finalized successfully and passed `ffprobe` as HEVC, `yuv420p10le`, limited range, `bt2020nc` / `smpte2084` / `bt2020`. Audio was disabled and both temporary videos were removed. This establishes capture and color metadata integration, not visual HDR accuracy, audio synchronization or gaming performance.

## Ownership, stop and recovery

An idle recorder has no worker thread or timer. While recording, one monitor thread blocks on the owned child's stderr/process exit and keeps only the last 16 KiB of diagnostics in RAM. Repeated start commands return the existing state. It never signals unrelated recorder processes.

The status changes to `recording` once the backend has started; the message accurately says that the backend is running. This is not a guarantee that a first frame was captured. Early failure asynchronously changes it to `error`. Keep an always-visible recording indicator enabled in your status surface when the settings window is closed.

Stop sends SIGINT to the owned child, waits for completion, verifies a nonempty video stream using `ffprobe`, flushes the file, then atomically publishes a final `.mkv` name without overwriting another file. Only then does it report **Saved recording**. This is structural validation, not an exhaustive decode or content/privacy check. If the backend hangs, bounded termination follows and the partial file stays marked as partial. Keep the daemon's normal shutdown handler and user service enabled so owned recordings receive a graceful stop. A forced process or machine shutdown cannot guarantee a final trailer.

Crashes and failed finalization retain nonempty hidden `.Murmur_*.part.mkv` files and show the path. Empty partials are removed. Jarvis does not automatically publish them as successful clips or restart capture after a crash. To inspect/recover a retained partial, preserve the original and try:

```sh
ffprobe -v error -show_streams '/absolute/path/.Murmur_example.part.mkv'
ffmpeg -n -i '/absolute/path/.Murmur_example.part.mkv' -map 0 -c copy '/absolute/path/recovered.mkv'
```

Matroska often permits recovery of completed clusters, but recovery is not guaranteed. `-n` prevents an existing recovery file being overwritten. Play the recovery and check the end of the video before deleting the partial. Opening settings does not recover or delete files.

## Verification record and remaining checks

Read-only inspection found wf-recorder 0.6.0 and FFmpeg with libx264/h264_vaapi encoders. The sandbox cannot connect to Hyprland or PulseAudio; host session probes performed by the main implementation task established Hyprland 0.56.2, PipeWire 1.6.9, an AMD RX 9070 plus Raphael GPU, and two 2560×1440 outputs (`DP-3` HDR at 180 Hz and `DP-5` SDR at 240 Hz). Do not treat those encoder listings as successful hardware-capture tests.

Automated recorder unit tests use a fake child process and no screen/audio access. They cover first-run/source/dependency validation, source typing, duplicate starts, SIGINT ownership, private staging, saved-file validation, crash retention, empty output, spawn failure and a disconnected UI callback.

Run in this repository:

```sh
PYTHONPATH=src python -m unittest discover -s tests -p test_recorder.py -v
hyprctl -j monitors
pactl --format=json list sources
wf-recorder --version
ffmpeg -hide_banner -encoders
```

The synthetic encoder benchmark uses generated pixels and discards output. It never accesses a screen or microphone. Its fresh worker measures the FFmpeg child's CPU time and peak RSS directly, including synthetic input generation. Reproduce the checked-in [sample result](benchmarks/encoder-benchmark.json):

```sh
python scripts/bench_recording.py --seconds 10 --size 1280x720 --fps 30 --codec libx264 --output docs/encoder-benchmark.json
```

It is paced toward real time; FFmpeg startup/initial input buffering means wall time may differ from the requested synthetic duration. Optionally test the media engine with `--codec both --vaapi-device /dev/dri/renderD128`, using the actual render device. This explicitly runs hardware encoding. A successful synthetic run does not validate screen capture or a game. Use `--unthrottled` only for throughput comparisons, not as idle/gameplay CPU evidence.

Actual capture and GUI checks, when run, are recorded separately in the project's verification/performance results. Before relying on gaming capture, verify these procedures on the user session:

- Capture the selected SDR source twice after setup. Check playback and ensure the second start shows no picker/permission prompt and preserves game focus. Repeat after logout/reboot and after a monitor hotplug.
- Record the intended system audio source and microphone separately, check playback/synchronization, then verify your explicitly combined source if using both.
- Start a clip, stop it, and inspect with `ffprobe`; verify the final name appears only after a clean stop. Terminate the test backend unexpectedly and verify the daemon shows an error and retains a partial. Do not kill unrelated recorders.
- Compare no recording, CPU preset and VA-API in a reproducible game scene; report child CPU/RSS, compositor CPU, encoder/graphics usage, disk rate, game average FPS and frame-time percentiles. Capture cost is separate from idle/waiting speech cost.
- Check full disk, disconnected monitor, unavailable audio server, inaccessible render device, denied screencopy permission and suspend/resume. Observe the visible indicator in fullscreen. These failure cases require a real desktop; fake-process tests do not establish their behavior.

Actual short no-audio capture checks completed on DP-5. Both CPU and VA-API
outputs finalized and passed ffprobe, then the temporary test videos were
deleted. The repeated CPU test and the VA-API test preserved focus; an earlier
CPU test observed a focus change whose cause was not established. Three-second
lifecycle measurements were about 196% of one logical CPU for libx264 and 6.47%
for VA-API on renderD128. These are separate from voice costs and do not measure
compositor/game impact. See [performance results](performance.md) and the raw
[VA-API check](benchmarks/desktop-vaapi-integration.json). No post-reboot unattended-capture,
visual-content, audio-sync or game frame-time guarantee is established.

## Instant replay

Instant replay keeps the last seconds of the selected display, plus game sound and your microphone. RAM is the default; disk buffering is an explicit setting explained below. It follows [Kairos](https://github.com/BasilForge/Kairos)'s instant-replay idea on Linux; no Kairos code is used. It needs the optional `gpu-screen-recorder` package (`sudo pacman -S gpu-screen-recorder`); Jarvis runs it as a separate program, so Jarvis's own MIT license is unaffected.

- **Off until you start it:** the Recorder page's **Start replay**, “start replay”, or `jarvis replay start`. **Start with Jarvis** turns it on whenever the service starts. The header shows **Replay on** and Waybar shows ⟲ while the buffer runs.
- **Save:** “clip that”, “save that”, “save replay”, the **Save replay** button or `jarvis replay save` writes the buffered seconds to the recordings folder as an MP4 and checks it with `ffprobe`. RAM mode writes no video before Save; disk mode writes unsaved buffers continuously.
- **Stop:** “stop replay”, the button, `jarvis replay stop`, service shutdown or a service crash (parent-death SIGINT) end the child and discard the buffer.
- **Sound:** **Game sound** records the default output (everything you hear) and **Microphone** the default input; both are on by default and mixed into one track. They follow your PipeWire default devices.
- **Clip sound:** a short rising chirp confirms each saved clip and a low tone reports a failed save; **Clip sound** turns it off.
- **Settings:** replay length is 10–600 seconds (default 120) and uses the Recorder display, color mode and folder. Stop replay before changing capture settings, storage, buffer directory or budget; they apply when the buffer starts. Clip sound and Start with Jarvis can change while replay runs.
- **Watch:** the **Clips** page lists saved replays and recordings, newest first, and plays them in the window. Playing MP4 there needs `gst-plugins-good` (`sudo pacman -S gst-plugins-good`); **Open in your player** works without it. The player is created on the first Play and released when you close it or leave the page.
- **Cost (measured 2026-10-21, gpu-screen-recorder 6.1.4, 2560×1440 at 30 fps, H.264 on the GPU):** the recorder used 48 MiB PSS (115 MiB RSS) and 0.7% of one CPU core while buffering a mostly still desktop; a save took 0.09 s. The buffered video adds to that and grows with motion, resolution and frame rate: the still desktop produced about 0.33 MB/s (≈40 MB per two minutes); fast game footage needs several times more.
- **Permissions:** monitor capture goes through gpu-screen-recorder's privileged KMS helper (`gsr-kms-server`) installed by the package. Review that before enabling replay.

### Replay storage and privacy

Choose **RAM** or explicitly select **Disk (continuous writes)** in Recording settings.
RAM remains the default for existing configurations. Disk mode writes unsaved
screen and selected audio, including microphone audio when enabled, continuously.
It removes live buffers after Stop; a crash can leave them until the next replay
start. Deletion is ordinary filesystem deletion, not secure erasure, and backups,
snapshots and SSD behavior can retain data. A deleted buffer is not a saved clip.

`replay.storage` is `ram` or `disk`; `replay.max_mb` is a 64–16384 MiB budget
(default 256; Linux disk mode needs at least 512, see below); `replay.buffer_dir`
is an optional **dedicated private directory**.
The default Linux path is `$XDG_DATA_HOME/murmur/replay-buffer` or
`~/.local/share/murmur/replay-buffer`. Linux requires an owned private directory
and creates files under a `0700` session directory with a private child umask.
It refuses tmpfs and ramfs, including this development machine's `/tmp`.
Choose an actual disk; moving video into RAM-backed temporary files saves no RAM.
An exclusive lock prevents another session from deleting a live buffer. Only
marked session directories in that root are removed during crash cleanup.

Linux disk mode requires the Rust controller and GSR 6.1.5 or newer. GSR's
[`replay_buffer_disk.c`](https://git.dec05eba.com/gpu-screen-recorder/tree/src/replay_buffer/replay_buffer_disk.c?h=6.1.5)
writes one file until it reaches 256 MiB, then starts the next, and removes the
oldest file only once the next one covers the whole replay length. The buffer
therefore always reaches at least 256 MiB, plus the replay length at the current
bitrate, whatever the replay length. Jarvis refuses to start disk replay with a
budget below **512 MiB**; long or high-bitrate replays need about 256 MiB plus
`seconds × Mbps ÷ 8` MB. Files needed by an in-progress save are retained.
Jarvis uses `-replay-storage disk` and a private `-o` buffer directory.
GSR also writes completed replays there; `-ro` controls regular recording
during replay, not saved replay clips. Jarvis publishes each completed clip
into the normal Clips directory before verification or buffer cleanup, without
overwriting existing clips. Cross-filesystem destinations use a private staged
copy. If that destination is unavailable, a recovery file is retained outside
the disposable session and its path is reported. Jarvis explicitly requests
the configured number of seconds when saving: omitting it asks GSR to save
the entire buffer, which can contain older footage because of file granularity.
It never evicts backend-owned files during a save.
GSR provides a time window but no configurable total-byte cap. Jarvis checks
buffer size and available space every **250 ms**, stops on a reached budget or
less than 16 MiB free, and recognizes buffer-write failures from stderr.
**This is a supervised stop threshold, not a filesystem quota: brief overshoot
and writes during shutdown are possible.** Strict Linux byte-quota acceptance
remains open.

Linux RAM mode is bounded by the replay length only; `replay.max_mb` does not
apply to it. GSR has no RAM byte cap, and its process RSS includes encoder and
allocator overhead (about 240 MiB measured live for a 120 s HDR desktop replay,
310–355 MB in games), so a fixed RSS limit would end replay instead of
shortening it. Use disk mode to reduce recorder RAM. The legacy Python
controller supports RAM mode and rejects disk mode with setup instructions.

Windows uses the shared Rust segment store with a pre-write encoded-byte cap;
see [Windows replay limits](windows.md). Encoder, driver and in-flight capture
memory are additional on both platforms. A higher bitrate can shorten history
or stop capture at the chosen budget. Disk-full and permission errors stop
capture with an error rather than restarting in a loop.

Continuous writes equal the encoded bitrate plus overhead: around 1 GB/hour
for a quiet desktop and around 10 GB/hour for a busy high-quality game are
illustrative, not fixed rates. At 8 Mbps the video alone is **3.6 GB/hour**.
This writes to the SSD even if no clip is saved. Saved clips are additional
disk usage and are not part of the rolling-buffer budget.
