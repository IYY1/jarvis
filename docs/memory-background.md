# Memory and background listening check — 2026-10-21

The working recognition path is Vosk for wake detection and command boundaries,
plus the resident int8 Whisper small.en model through sherpa-onnx 1.13.8 for
command transcription. This preserves the recent recognizer upgrade, app-name
matching, adaptive energy gate and optional denoiser settings. Neither recognition
library runs in the settings process. No model or sensitivity changes were made
in this check.

## Background behavior

The daemon, voice worker and `pw-record` continued running with the settings
window closed. An initial live observation reached Listening, reported an
unmatched utterance, then timed out; the user also confirmed a successful
background command before any changes. This establishes intermittent command
recognition, rather than a requirement for window focus. The exact cause of the
unmatched utterance cannot be recovered: audio and transcript history were not
saved.

There was a separate, reproducible timing defect. When background speech kept
Vosk from finalizing, the worker flushed the collected command at the eight-second
capture deadline. Whisper then decoded it synchronously. The daemon rejected the
result because its own listening deadline had already passed. Even a perfect
transcript from that flush could never execute. Slow decoding could also be
mistaken for three seconds without microphone frames on the next loop iteration.

The worker now sends its monotonic capture deadline and the time capture closed.
The daemon checks that capture ended within that session, then permits up to
15 seconds for result delivery. Late audio, stale results, low confidence,
duplicates and results after cancellation remain rejected. Synchronous decoding
time is excluded from the microphone-stall check. Capture duration, command
matching and noise thresholds retain their existing settings.

After applying the change, the user confirmed **“Jarvis, open Steam” opened
Steam with Jarvis hidden**. The observed service states were Listening →
Processing → Idle. Hyprland reported zero Jarvis windows. This confirms the tested
flow; it does not establish recognition accuracy under every game or room noise.

## Memory change

Whisper's ONNX Runtime CPU allocator retained large inference scratch buffers
between commands. `onnx-cpu.conf` disables the CPU memory arena. Once an utterance
stream has been destroyed, `malloc_trim(0)` returns freed native allocator pages
on glibc. On systems without that function, trimming is skipped. Model weights
stay resident, and the graph settings, precision, thread count, padding and
decoding remain the same. The configuration is included in the built wheel.

The provider configuration syntax and `EnableCpuMemArena` behavior were checked
against the [pinned sherpa-onnx session implementation](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/sherpa-onnx/csrc/session.cc).

### Controlled comparison

This table records the allocator-only phase. Subsequent thread-pool tuning and
the final combined comparison are in [current performance](performance.md).
The benchmark script now measures CPU work as well as memory.

Fresh processes, same Whisper small.en files, eight threads, AMD Ryzen 7 7800X3D,
Python 3.14.8. One public PocketSphinx `goforward.raw` utterance was tested clean,
quieter, with seeded noise, repeated with a pause, silent, truncated and slowed;
seven cases repeated three times. This checks transcript equivalence and memory
retention, not multi-speaker or accent accuracy. No microphone input was used.

| Isolated Whisper process | Original allocator | Updated allocator |
| --- | ---: | ---: |
| RSS after loading | 510.1 MiB | 449.1 MiB |
| RSS after 21 transcriptions | 953.3 MiB | 454.9 MiB |
| Peak RSS | 984.2 MiB | 767.0 MiB |
| Median transcription time | 304.0 ms | 314.8 ms |

All 21 paired transcripts were identical. The median of paired latency
differences was +13.0 ms. Reallocating buffers is a small latency tradeoff for
about 498 MiB less retained memory in this comparison. Transient peak memory
still exceeds idle memory. Full results, fixture hash and platform information
are in [transcriber-memory-benchmark.json](benchmarks/transcriber-memory-benchmark.json).

Reproduce in separate processes; the script reads a supplied fixture and does
not download or save audio:

```sh
.venv/bin/python scripts/benchmark_transcriber.py --fixture /tmp/goforward.raw --baseline
.venv/bin/python scripts/benchmark_transcriber.py --fixture /tmp/goforward.raw
```

Fixture provenance and download instructions are in
[speech-research.md](speech-research.md). `Transcriber(memory_efficient=False)`
retains the original allocator behavior for this comparison.

### Running service

Before the update, the already-used voice worker occupied 1,051.1 MiB RSS;
the daemon plus voice worker plus microphone capture totaled 1,089.6 MiB RSS
(1,050.7 MiB PSS). After restart and successful live commands with the window
closed, the voice worker occupied 620.6 MiB RSS and the three processes totaled
657.1 MiB RSS (628.9 MiB PSS). These live samples have different command histories;
the controlled comparison above isolates the allocator effect.

The systemd user service remains active, unmuted and waiting for the wake phrase
with `vosk+whisper-small`. The configuration SHA-256 was unchanged across restart.
No microphone recordings or transcript logs were created by the diagnostic check.

## Verification

The full 141-test suite passed with real Vosk and Whisper models and the public
fixture enabled, including the new deadline and allocator checks. A worker test
verifies decoding longer than three seconds does not trigger a false
microphone-stall error. Wheel construction passed and its contents include the
ONNX CPU configuration. A source snapshot from before these changes is retained
at `/tmp/jarvis-before-background-fix/source.tar.gz` for this session.
