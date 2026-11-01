# Combined wake detector: fresh-recording screen — 2026-10-27

**Rejected.** The fixed PocketSphinx/microWakeWord combination passes all
familiar controls but misses eight new recordings detected by stock Vosk.
No production engine, dependency, model, target or replay setting changed.
The [measurement artifact](benchmarks/dual-wake-screen-2026-10-27.json) contains
every selected input/hash, count, trigger time, protocol and decision.

## Data and protocol

The source is the public
[Picovoice wake-word benchmark](https://github.com/Picovoice/wake-word-benchmark/blob/84919190fb1891bf0936dcb577d9268e467f876f/README.md#data),
pinned to commit `84919190fb1891bf0936dcb577d9268e467f876f`. The publisher
identifies the full collection as crowd-sourced recordings from more than
50 speakers. Our selected files have no speaker IDs; the selected subset is
not claimed to contain that many independent speakers. No proprietary runtime,
access key or Porcupine library is used.

Before decoding, sort each keyword directory by its pinned Git blob SHA1;
select the first 32 Jarvis files and the first four each of Alexa, computer,
smart mirror, snowboy and view glass as Jarvis negatives. The other 352 Jarvis
recordings are not downloaded or decoded. Initial metadata selection excluded
Alexa's FLAC extension; that was corrected before any audio download/decoding.
All intended labels come from the publisher's keyword directories and remain
fixed independently of model output.

Every original download is verified against its Git blob hash and also pinned
with SHA-256. FFmpeg converts the complete input to mono 16 kHz s16le; no trim,
gain or denoising is applied to the fresh inputs. Both arms receive two seconds
of trailing silence. The source material totals 156.84 seconds, excluding the
added silence. Original media, converted PCM, licensing records and drivers
remain under ignored `build/memory-plan-corpus/dual-wake-screen-2026-10-27/`.

PocketSphinx retains sensitivity 0.5 and threshold 1e-20, with RMS 0.001,
300 ms pre-roll, 1200 ms hangover and a ten-second utterance cap. The dedicated
Hey Jarvis model retains cutoff 0.97 and a five-prediction mean. Feed continuous
micro features and energy-gated PocketSphinx in 30 ms input packets. The first
result emits one wake and disarms both outputs until quiet expires; then reset
both searches and the micro feature state. Discard final hypotheses at quiet
and cap boundaries. No command session is armed by this prototype.

Each new file gets one fresh combined worker and one fresh native Vosk
`--engine` worker. Vosk uses its normal wake variants, sensitivity 0.5 and RMS
0.005, with accurate transcription/denoising disabled to isolate wake counts.
Its command decoders remain present. These scopes differ, and their memory
cannot establish a production replacement saving.

Separately, 18 already-seen controls run in three fresh combined workers each:
four positive recordings at gains 1/0.5/0.1, four negatives, and two repeated
wake/reset sequences. Selection and settings were frozen before inference;
nothing was changed after observing a miss.

## Results

| Check | Combined detector | Stock native Vosk |
| --- | ---: | ---: |
| New Jarvis positives detected | **24/32** | **31/32** |
| New other-keyword negatives with false wakes | 0/20 | 0/20 |
| Paired regressions / gains versus Vosk | **8 / 1** | Baseline |
| Familiar control repetitions | 54/54 | Not repeated in this experiment |
| Shared reset sequences | All six repetitions return the expected two or three wakes | Not tested by this one-result CLI control |

The combined process reaches **71.86 MiB peak RSS**, including Python, NumPy,
both native libraries, models and buffers. Controller, capture, replay,
settings and command recognition are excluded. The primary driver completes
and exits **1**. The Vosk baseline's remaining miss is also retained; 31/32 is
not absolute recognition acceptance.

The candidate was chosen using earlier complementary failures. Its familiar
controls pass, while its fresh inputs expose substantial regression. This is
why an inferred union of old successes was insufficient. No timing, standby
CPU or command-handoff campaign is run for a candidate already failing the
predeclared quality gate. Pretrained training-data overlap and speaker-level
independence are unknown, and no general false-alarm rate is inferred from
these short other-keyword negatives.

## Next decision

Keep the current Vosk wake path. The earlier isolated Linux application
baseline was 176.84 MiB RSS with replay off, settings closed and fake capture;
it exceeds the planned 150 MiB waiting target. A smaller wake replacement has
now failed broader direct comparisons. The user has been asked whether to
retain 150 MiB as a hard target and continue wake-engine research, or revise
that waiting target to 200 MiB and prioritize command-memory reduction while
keeping Vosk. **Until an explicit answer, all existing numerical targets remain
unchanged.** Command-model quality, retained memory, whole-application testing
and the final soaks still require work under either choice.
