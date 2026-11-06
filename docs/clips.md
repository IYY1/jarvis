# Clips: watch, trim and join

On Windows, **Clips** has a native file picker and recordings list, per-track
Keep switches, Only this / Keep all / Mute all, silent-track detection,
keep/remove sections, split, join, undo and Fast/Exact export to a new copy.
Start/end numbers mark sections; preview opens the system player. It uses the
bundled FFmpeg tools, with CPU re-encoding for Exact mode. Linux additionally
has embedded playback and the visual timeline described below.

The **Clips** page lists saved replays and recordings (newest first; eight at a
time, **Show all** for the rest). **Open** loads one into a small editor:

- **Player and timeline.** The timeline shows the parts you keep (alternating
  shades), keyframes (ticks), your marked section (gold) and the playhead.
  Click or drag it to seek. Playback skips sections you removed, so the preview
  follows the edit without rendering anything.
- **Edit.** Mark in (I) and Mark out (O), then **Remove section** (Delete) or
  **Keep only section** (K). **Split** (S) cuts at the playhead. **Join at end**
  appends another clip. **Undo** (Ctrl+Z) steps back.
- **Sound tracks.** As in LosslessCut, every track is a row with its name and
  format (“Discord · Opus · stereo · 48 kHz”) and a **Keep** switch; **Only
  this** keeps just that track, and **Keep all** / **Mute all** switch every
  one. Replays saved with separate tracks
  ([recording](recording.md#separate-sound-tracks)) have one per app, for
  example “Everything else”, “Firefox”, “Discord”, “Microphone”. A track with no
  sound in the clip (an app that stayed quiet) is marked and starts off; one
  audio-only pass finds it in a few seconds. Kept tracks can stay separate or be
  mixed into one.
- **Export copy** writes `NAME edit N.mp4` beside the first clip and shows
  progress; **Cancel export** stops it. The original is never changed and an
  existing file is never overwritten.

## Two export modes

| | Fast | Exact |
| --- | --- | --- |
| Video | Copied unchanged | Re-encoded, on the GPU when it can (VA-API; CPU otherwise) |
| Cut points | Each part starts on the keyframe at or before your mark | Exactly where you marked |
| Speed | Seconds, any length | Roughly 2× faster than real time for 1440p60 AV1 here |
| Joining clips | Only clips with the same codec, size, pixel format and audio tracks | Any clips; joined clips are scaled to the first clip's size |
| HDR | Kept (copied) | Kept: 10-bit with PQ/BT.2020 tags |

gpu-screen-recorder writes a keyframe every two seconds, so a Fast part can
start up to two seconds early. The editor shows the Fast length before you
export (“Fast export starts parts on keyframes: 00:17.0 long”). Mixing tracks
re-encodes only the audio.

Sound that was mixed while recording can't be separated afterwards. To mute an
app or your voice later, turn on separate tracks before recording.

## What was wrong with the old player

Measured on 2026-11-05 with real 1440p60 AV1 10-bit HDR replays: GStreamer
chose the RX 9070's VA-API AV1 decoder, kept real time, seeked correctly and
used little CPU. The problems were around it:

- it plays only the first audio track (GStreamer's default), which hides
  separate tracks; the editor says so and applies your choice on export;
- a fixed 360 px player sat above an unbounded list of full-width file names;
- there was no timeline or editing, only “Open in your player”.

HDR display in the preview depends on GTK and compositor color management and
was not verified visually. Exports keep HDR either way.

## LosslessCut

[LosslessCut](https://github.com/mifi/lossless-cut) is GPL-2.0, an Electron app
bundling FFmpeg (~200 MB downloads), with only experimental automation
(`--http-api`, project files). Options considered:

| Option | Result |
| --- | --- |
| Embed or reuse its code | Rejected: Jarvis would have to be distributed under the GPL, and it would bring an Electron runtime into a small native app. |
| Launch it as a separate app | Supported: **Open in LosslessCut** appears when it is installed (binary or Flathub `no.mifi.losslesscut`). Running a GPL program separately doesn't affect Jarvis's MIT license, as with gpu-screen-recorder. |
| Implement the needed workflow on FFmpeg | Chosen: trim, cut, split, join, track selection and both export modes, in `src/jarvis/clipedit.py` (pure Python and FFmpeg). |

Tests: `tests/test_clipedit.py` exports synthetic clips with real FFmpeg
(fast cuts, track selection, mixing, never overwriting, joining mismatched clips
through Exact). Real AV1 HDR replays were also exported in both modes.
