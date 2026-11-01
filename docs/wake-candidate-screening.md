# Wake candidate screening — 2026-10-27

No candidate in this screen is adopted. Production Vosk/Whisper small.en,
RAM replay and the running installation are unchanged. Windows and optional
disk optimization remain deferred by the user.

The user has now explicitly confirmed **“Jarvis, clip that”** as the intended
phrase in the previously selected final-five-second clip. The
[label record](benchmarks/user-tail-label-2026-10-27.json) ties this confirmation
to the existing PCM hash. Earlier artifacts truthfully recorded the label as
provisional when measured; the new confirmation applies only to this clip.
Older group recordings remain unlabeled and the reserved recordings untouched.

## Fixed experiments and results

The [measurement record](benchmarks/wake-candidate-screening-2026-10-27.json)
contains the declared protocols, individual counts, hashes, package/model
provenance and exit codes. Full outputs and private audio stay under ignored
`build/memory-plan-corpus/`. No recording or complete transcript is published.

| Candidate | Result | Decision |
| --- | --- | --- |
| One resident tiny.en, rolling wake windows | 42/60 cases; 24/42 intended wakes; no extras; up to 128.02 MiB process RSS peak | All 18 user-tail variants miss Jarvis; keep tiny as a command-only candidate |
| ESPHome v2 Hey Jarvis microWakeWord, publisher defaults | 30/33 cases; 9/12 intended wakes; no extras; up to 38.45 MiB peak RSS including Python/NumPy/native libraries | All public Hey Jarvis positives pass, but the selected bare-Jarvis tail misses in all three fresh processes |
| PocketSphinx with bounded energy gating instead of native VAD | 72/78 cases; 45/51 intended wakes; no extras; up to 53.36 MiB process RSS peak | Recovers all earlier VAD-screen failures, but misses two new public Hey Jarvis recordings |
| Stock native Vosk, new-recording control | 15/21 cases; 6/12 intended wakes; no extras | Misses those same two public recordings; correctly detects the user's clip |

All four recognition drivers complete and exit **1**, preserving the failed
intended labels. The scopes differ and these memory figures are not a matched
application-wide saving. No new command latency, standby CPU, pressure,
long-duration or combined-budget gate passes.

### Single tiny recognizer

The fixed window is at most 4.98 seconds, with 960 ms minimum input and 480 ms
stride. It uses only audio already available, RMS 0.001, 300 ms pre-roll and
1200 ms hangover. After a wake it disarms until quiet; there is no keyword bias
or fuzzy name matching. Three fresh recognizers pass all 42 public case
repetitions but miss all 18 user-tail gain/leading-quiet variants.

A separate same-input control compares Rust and sherpa-onnx for tiny.en and
small.en on the whole user tail, the tail plus two seconds of silence, its
first 3.84 seconds, and the public goforward utterance. **All eight transcripts
are identical across implementations.** Tiny omits Jarvis in all three user
inputs; small includes it. This does not establish full numerical equivalence,
but does not support attributing these wake misses to a Rust-only discrepancy.
The earlier Vosk-plus-tiny command result remains valid: Vosk detects the wake,
then tiny transcribes the command. It never proved tiny could detect Jarvis.

### Dedicated model

The model and manifest are pinned to ESPHome commit
`40ff33f57f8fc6ad71a75ef085abbf742495b225`. Its defaults are probability cutoff
0.97, a mean over five predictions and 10 ms feature steps; no extra gate,
gain, denoising or threshold change is applied. The stock mean probability on
the selected tail peaks at 0.003125 in every run. This is a model trained for
**Hey Jarvis**, not arbitrary configurable wake names; see the
[publisher's manifest](https://github.com/esphome/micro-wake-word-models/blob/40ff33f57f8fc6ad71a75ef085abbf742495b225/models/v2/hey_jarvis.json).
The publisher explicitly licenses its model weights and manifests under
Apache 2.0 in its
[license statement](https://github.com/esphome/micro-wake-word-models/blob/40ff33f57f8fc6ad71a75ef085abbf742495b225/README.md#license).

The model, six public upstream WAV fixtures and two diagnostic wheels remain
under ignored `build/memory-plan-mww/`. Downloads were checked against PyPI
SHA-256 values or the pinned Git blob hashes as applicable. The installed
Python environment and application dependencies are unchanged. Fixture origins
are the upstream
[Hey Jarvis tests](https://github.com/OHF-Voice/pymicro-wakeword/tree/333da550c54b95192af472cfe37d98dab2f2086e/tests/hey_jarvis)
and
[Alexa tests](https://github.com/OHF-Voice/pymicro-wakeword/tree/333da550c54b95192af472cfe37d98dab2f2086e/tests/alexa).
File counts do not establish independent speakers or accents.

### PocketSphinx VAD attribution

This is a separate candidate from the [original screen](pocketsphinx-wake-screen.md).
The keyword model, sensitivity 0.5 and threshold 1e-20 stay fixed. The native
VAD is bypassed; an already-used RMS 0.001 gate retains 300 ms pre-roll and
1200 ms hangover, with a ten-second utterance cap. Exhausted quiet and the cap
finalize and discard hypotheses. A detected wake disarms until quiet.

All 60 shared case repetitions now pass, recovering the earlier 18 misses
without a keyword-threshold or gain change. This shows those particular
failures depend on the input/VAD path; it does not prove every PocketSphinx
failure has that cause. Of the six new public Hey Jarvis/Alexa recordings,
two Hey Jarvis files still miss in all repetitions. The stock Rust Vosk
control misses those same two, while microWakeWord detects them. Neither
baseline failures nor improvements on other cases clear the absolute quality
gate.

## Next bounded action

The two smaller detectors have complementary failures on these recordings.
A fixed two-detector combination is a hypothesis, selected using this evidence,
not a validated solution. Before any production integration, declare one shared
wake/disarm/reset policy and measure fresh positive/negative recordings,
trigger timing, silence CPU, complete memory and subsequent command handoff.
Do not count a union of these already-seen outcomes as independent validation.
Keep the original model thresholds fixed and retain all failures. Arbitrary
custom names, missing corpus categories, whole-application budgets and final
soaks remain open.

The [subsequent fixed combination](dual-wake-screen.md) is now measured and
rejected on fresh public inputs, despite passing all familiar controls.
