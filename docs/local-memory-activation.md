# Local memory changes — 2026-10-27

## Current setting: RAM replay

The user asked to restore RAM replay after raising the cost of continuous SSD
writes. The local configuration API stopped replay, changed only
`replay.storage` from `disk` to `ram`, then restarted replay. The controller
remained running. The active recorder arguments confirm `-replay-storage ram`,
`-r 120`, `-k hevc_hdr` and both game and microphone audio. Replay remains on,
and the former disk-buffer root contains no live session directories.

The switch restarted unsaved history; it did not save a clip. All other
recording and recognition settings were preserved. The stored 2048 MiB disk
stop threshold is inactive in RAM mode; Linux RAM history is bounded by time,
not by that byte setting. This is a configuration verification, not a new
memory or recognition benchmark. See the
[restoration record](benchmarks/replay-ram-restored-2026-10-27.json).

The full pre-switch configuration is backed up privately under ignored
`build/memory-plan-live/2026-10-27-ram-restore/config-before.json`. Disk replay
remains an optional setting. Further disk-quota engineering is deferred at the
user's request; current work prioritizes voice memory, reliability and latency.
Windows validation is also set aside for now. Production recognition remains
Vosk/Whisper small.en, with the experiments disabled.

## Earlier disk activation and validation

Earlier on the same day the user enabled the implemented disk-replay mode.
The evidence below describes that historical activation. Repository defaults
remained RAM replay and Vosk/Whisper small.en recognition throughout.

| Setting | Before disk activation | During disk validation |
| --- | --- | --- |
| Replay storage | RAM | Disk |
| Replay history | 120 seconds | 120 seconds |
| Disk stop threshold | Not applicable to Linux RAM replay | 2048 MiB |
| HDR, video quality, monitor and audio tracks | Existing settings | Preserved |
| Voice recognition and denoising settings | Vosk/Whisper small.en with existing settings | Preserved |

The service's configuration API stopped replay, saved the new settings and
started disk replay. Saving settings also reloads the voice worker. The
controller process remained running during that initial switch. The switch
replaces the unsaved RAM history; it does not save a clip.

The buffer is under the private local data directory
`~/.local/share/murmur/replay-buffer/`, using an owned `0700` session directory.
Disk mode continuously writes unsaved video and the enabled game/microphone
tracks. Stop removes the live session; crash leftovers are cleaned at the
next replay start. Normal filesystem deletion is not secure erasure. Encoded
writes and reclaimable OS file cache remain costs of disk buffering. See
[storage behavior and limits](recording.md#replay-storage-and-privacy).

### Historical verification scope

Replay stayed on through 125 seconds of observation, longer than the requested
120-second history window. The recorder's RSS was **149.82–150.02 MiB before**
the switch and **115.27 MiB at the final disk sample**, about **34.66 MiB lower**
than the baseline mean. The live private buffer reached **13.74 MiB**. This
quiet desktop result is not a game-workload saving estimate.

The final readable process sum was **298.02 MiB RSS**. Its coverage is
incomplete and the voice worker had been reloaded, so it does not establish
the 300 MiB combined replay-on budget. Command memory with small.en remains
an unresolved part of the plan.

[Live activation evidence](benchmarks/replay-live-disk-2026-10-27.json) records
the RAM baseline, actual recorder arguments, per-process memory, buffer-file
sizes and permissions, and observations spanning a full replay window after
the switch. It verifies that the real recorder uses `-replay-storage disk`
with private disk buffering. The initial interpretation of `-ro` as the clip
destination was incorrect; only the later save checks verified actual routing.

This is an observational check of the user's live desktop. The before/after
scene is not controlled. Report recorder memory separately: the voice-worker
reload changes its memory floor and cannot be counted as a disk-buffer saving.
One privileged KMS helper's memory is unreadable, and a separately launched
settings window is outside the service tree, so the readable process sum is
a lower bound rather than a complete application total.

That initial observation did not read footage/audio or save a clip. Subsequent
authorized validation saved clips and decoded them locally to null output,
without displaying, playing or uploading their contents. It exposed and fixed
clip-retention and history-duration bugs; see [the live validation report](replay-live-validation.md).
The tested controller was installed and the service restarted for those fixes,
preserving the existing recognition and recording settings. Sustained game
workloads, disk rollover/quota stress and Windows acceptance remain open.
The 2048 MiB setting is a supervised stop threshold with possible overshoot,
not a strict filesystem quota.

## Remaining recognition work

The smaller Whisper profiles, KWS replacement, Vosk fast path and finalization
experiment are not enabled by this activation. Their remaining acoustic,
timing and integration gates still apply. Whisper small.en can still dominate
command memory. All combined-budget, full acoustic, Windows and long-duration
release gates remain open; see [progress and remaining work](memory-plan-progress.md).

The configuration before the original disk activation is backed up privately
under ignored `build/memory-plan-live/2026-10-27/config-before.json`. The newer
RAM restoration used a fresh configuration read to preserve later edits.
