# Experimental KWS quiet gate and buffered handoff

This continues step 6 of the [memory plan](memory-plan.md). Both experiments
are file-only, opt-in Rust probes. The normal Vosk/small.en worker, controllers,
installed models and application settings are unchanged. No real microphone,
screen capture or installed service is started.

## Quiet gate design

The detector accepts an optional `quiet_gate` object in its explicit probe
configuration. Omitting it, or supplying `null`, retains continuous decoding.
The measured candidate uses RMS 0.001, 300 ms of onset padding and 1200 ms of
hangover, with 30 ms input chunks. These are experimental constants, not an
adaptive acoustic calibration. Background noise above the threshold keeps
decoding active. Audio below the threshold for longer than onset padding can
still be lost; passing the supplied low-volume clip does not settle that risk.

[The Rust gate](../native/kws-probe/src/gate.rs) keeps at most ten 480-sample
float frames. It opens on energy, replays the saved onset, and continues through
short pauses. When hangover ends it destroys the native stream, including its
feature history, rather than leaving an unfinished background hypothesis for
the next wake. This follows the existing Vosk gate's onset/hangover behavior
and the [quiet-boundary missed-wake fix](memory-plan.md).
The quiet boundary never forces a final wake hypothesis.

Every trigger's `audio_seconds` is the amount of **original input** available
when it was detected, including skipped quiet. Replayed onset frames become
available at the current input time. `stream_origin_seconds` identifies the
original beginning of each native stream; the nested upstream `result`
retains its native, stream-relative timestamp semantics. Native token times
must not be mistaken for original input trigger times. Long leading quiet and
three background/quiet/wake cycles explicitly check the original timeline.

The probe reports decoded/skipped samples and native stream creation counts.
Silence creates no stream. The bounded ring remains allocated when final
RSS/PSS are sampled; native libraries, stream/allocator state and buffers all
count. Neither comparison arm calls an allocator trim.

## Quiet gate results

[Recorded measurements](benchmarks/kws-quiet-gate-2026-10-26.json), Linux,
three fresh paced workers per arm:

| Measurement | Saved ungated baseline | Optional gate |
| --- | ---: | ---: |
| Silence CPU, percentage of one core | 1.483–1.503% | 0.0132–0.0150% |
| Active reversed-speech CPU, one core | 1.462–1.526% | 1.424–1.463% |
| Ready RSS | 48.60–48.63 MiB | 47.64–48.75 MiB |
| Peak RSS, buffers/native state included | 53.91–54.44 MiB | 53.57–54.59 MiB |
| Final PSS | 49.52–50.04 MiB | 49.18–50.19 MiB |
| Focused unpaced + paced cases | 40/40 | 40/40 |
| Expected wakes detected | 33/33 | 33/33 |
| Extra triggers on these controls | 0 | 0 |

The paced subset is 21/21 expected wakes per arm. The current ungated control
also passes all 19 unpaced cases and matches the saved binary's exact trigger
chunks. Those counts reuse the same recordings; they must not be presented as
new independent speech examples.

The isolated standby CPU gate passes on these fixtures, with roughly 99% less
silence CPU. Memory is effectively unchanged. **Trigger-time nonregression
does not pass:** the low-volume wake arrives 180 ms later in each comparison.
Other measured differences range down to 180 ms earlier; the natural and
soft-onset trigger chunks are unchanged. Creating a new stream changes its
context and frame alignment, so skipping quiet is not timing-equivalent to
continuous decoding. All input trigger positions remain on the original
timeline; the 12-second leading-quiet control does not collapse that gap.

The earlier threshold failures remain 9/24 misses at 0.5 and 12/24 at 0.75.
Neither those failures nor the new timing regression is waived by the
passing label checks. Gate enablement and the full release gate remain false.

## Comparison protocol

[The comparison script](../scripts/benchmark_kws_gate.py) verifies both saved
binary/archive hashes and all eight archived source hashes from
`build/memory-plan-kws/baseline/snapshot.json` before using the baseline.
It runs workers sequentially at the preselected threshold 0.25. A current
ungated control checks that default-disabled behavior still matches the old
binary, including exact input trigger chunks.

The unpaced set contains 19 cases. It includes the original controls, a soft
onset, 12 seconds of leading quiet, and continuous reversed speech for active
CPU measurement. Three fresh workers per measured arm then replay seven
selected cases at real-time speed, including 30 seconds of silence, 10%
amplitude speech, a pause within the wake name, soft onset and repeated
background recovery. All arms use identical PCM and settings. The reference
threshold 0.5/0.75 failures remain linked in the new artifact; they are not
rerun or reclassified as passes.

Repeated recordings are not independent speakers, accents, confusables or a
general false-alarm-rate estimate. Jarvis/Hey Jarvis still have no positive
recordings. Windows, actual capture, GTCRN interaction, pressure, combined
application budgets and full-duration soak are open.

## Separate buffered handoff prototype

[The handoff binary](../native/kws-probe/src/bin/jarvis-kws-handoff-probe.rs)
is separate from the KWS detector and is never selected by normal controllers.
It receives the same supplied PCM at real-time speed while the detector runs
in its own process. A bounded ring retains two seconds before the detector's
original input trigger time, plus 300 ms of bounded event-delivery slack.
On the first trigger it starts the explicit existing `jarvis-voice --engine`
binary. It keeps receiving all input while that worker initializes.

The command buffer reserves a fixed 320000-byte cap (ten seconds of PCM).
Overflow or a trigger delivered too late for the ring fails explicitly;
neither condition silently drops the beginning of a word. Buffered input is
saved unchanged under the ignored experiment directory and checked byte for
byte against the original fixture slice. Child processes are owned, stopped
and reaped on failure as well as successful completion.

The command worker replays the retained **wake and command** through its
existing wake decoder, command decoder, free Vosk decoder and matcher. No
replacement matcher or exact-command shortcut is introduced. The whole worker
is loaded on demand; its configured Whisper is loaded immediately inside that
child so cold loading is counted. Vosk-only, default small.en and an explicitly
configured experimental tiny.en are separate arms. No production model choice
changes. The existing worker's allocator policy is retained for every arm;
the probe requests no extra trim.

The prototype uses the explicit file boundary as its endpoint, then submits
the retained clip to `--engine`, which also adds its normal offline endpoint
padding. It does not demonstrate live streaming command endpointing. Reported
latency includes the supplied 1.5-second tail, loading, replay and recognition,
relative to the end of the public source PCM (not a manually annotated phonetic
endpoint). The natural KWS trigger is 2.070 seconds into the input, whereas the
existing fixture command cut is 1.800 seconds: the buffer must preserve about
270 ms of already-arrived command even before recognizer loading begins.

The parent samples its own process, detector and recognizer together every
20 ms. Peak summed RSS and PSS include the rings, retained PCM and simultaneous
models. A separate sum of process RSS high-water marks is a conservative upper
bound, not a simultaneous peak. Sampling can miss shorter peaks; neither
worker-set figure establishes the budget for the entire application.

[The handoff benchmark](../scripts/benchmark_kws_handoff.py) uses three fresh
cold worker sets per profile for the natural clip. Focused controls add a
three-second configuration delay during which audio must remain intact,
command-without-wake, and wake-without-command. These are surrogate fixture
checks; they cannot establish recognition or action safety on actual custom
names, nor Windows acceptance.

## Buffered handoff results

[Recorded measurements](benchmarks/kws-buffered-handoff-2026-10-26.json),
three fresh process loads per profile for the natural clip. Filesystem caches
were not evicted. Figures include the Rust parent, detector and recognizer:

| Command recognizer | Peak combined RSS | Peak combined PSS | RSS HWM sum upper bound | Result after source PCM end | Worker load |
| --- | ---: | ---: | ---: | ---: | ---: |
| Vosk with free decoder | 228.28–231.47 MiB | 216.27–219.46 MiB | 228.28–231.47 MiB | 1815–1817 ms | 218–220 ms |
| Vosk + default small.en | 593.62–617.21 MiB | 581.55–605.10 MiB | 593.62–617.21 MiB | 2007–2039 ms | 1790–1820 ms |
| Vosk + experimental tiny.en | 320.07–325.60 MiB | 310.27–313.65 MiB | 325.54–331.27 MiB | 1872–1891 ms | 533–546 ms |

All 18 cases pass: each profile has three natural-command repetitions plus
the late-loader, command-without-wake and wake-without-command controls. All
15 triggered buffer files match the original input slices byte for byte.
The natural window starts at input 0.060 s, preserving the 270 ms of command
before the 2.070 s trigger and all subsequent input while loading. No command
worker starts on command-without-wake; wake-without-command returns no command.

The three-second configuration-delay controls also retain the complete clip;
they return the expected command after about 2.25 s (Vosk), 4.13 s (small.en)
and 2.61 s (tiny.en), relative to the source PCM end. They exercise retention
through a delayed load, not real-time speech endpointing.

Default small.en fails the 400 MiB command target even for this reduced worker
set. The other two profiles are below that number in this fixture experiment,
but controller/UI/capture/replay/GTCRN, pressure and full recognition acceptance
are absent, so **no whole-application budget gate passes**. The Vosk-only arm
also removes Whisper's second pass; this small corpus cannot justify that
quality tradeoff. Tiny remains experimental. The explicit file tail dominates
these latency figures; they cannot establish latency nonregression against
the live default worker.

The [subsequent repeated-cycle and endpoint experiment](kws-handoff-cycles.md)
now measures command-worker teardown, waiting floors, timeouts and overflow.
It also records a new low-volume endpoint failure and a separate candidate
configuration. The detector timing regression and all full acoustic, Windows,
combined-budget and soak gates remain open.

## Reproduce with existing local assets

Use the cached toolchain described in [the handoff](memory-plan-handoff.md)
and build `native/kws-probe/Cargo.toml` offline. `cargo test` includes both
binaries; `cargo clippy --all-targets -- -D warnings` checks them together.

```sh
python scripts/benchmark_kws_gate.py --baseline-dir "$WORK/baseline" --fixture "$PUBLIC_PCM" --model-dir "$MODEL" --library "$SHERPA_C_API" --keywords-dir "$WORK" --work-dir "$WORK/gate-paced" --output "$NEW_GATE_RESULT"
python scripts/benchmark_kws_handoff.py --fixture "$PUBLIC_PCM" --model-dir "$MODEL" --library "$SHERPA_C_API" --keywords-dir "$WORK" --tiny "$TINY_MODEL" --work-dir "$WORK/handoff" --output "$NEW_HANDOFF_RESULT"
```

All variables identify explicit, already-supplied local assets. Both scripts
require a fresh output path and run no downloads, installed service or actions.
