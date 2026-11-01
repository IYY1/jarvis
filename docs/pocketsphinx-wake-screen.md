# PocketSphinx wake screening — 2026-10-27

After the user deferred further disk-replay optimization and Windows work,
this bounded experiment checked the existing PocketSphinx fallback as a
smaller wake candidate. **It is not accepted or integrated.** Production
Vosk/Whisper small.en and all experimental flags remain unchanged.

The [measurement record](benchmarks/pocketsphinx-wake-screen-2026-10-27.json)
contains per-case counts, input trigger times, memory, settings and evidence
hashes. Private audio, full events and drivers remain under ignored
`build/memory-plan-corpus/pocketsphinx-triage-2026-10-27/`.

## Fixed protocol

Use the existing `murmur.speech.SpeechEngine` at sensitivity 0.5, with its
unchanged native VAD and keyword threshold 1e-20. Feed raw 16 kHz mono s16le
PCM in 30 ms frames; no external energy gate or denoising. The Jarvis search
uses primary `jarvis` plus alias `hey jarvis`, which enables the adapter's
existing wake-variant keyword file. This differs from the fallback's default
single `hey jarvis` search; no default configuration was changed. The public
surrogate search uses `go forward`. Every case runs in three fresh processes,
with two seconds of explicit trailing silence and re-arming after each wake.

Phase one was declared before decoding: the selected user tail, a public
positive, a public absent-Jarvis control and silence. After those passed,
phase two was declared separately before execution: reuse all eleven existing
public surrogate controls from `scripts/benchmark_kws.py`, three public
absent-Jarvis controls, the user tail at gains 1/0.5/0.1 and leading quiet
0/300 ms, and three paced silent workers. No setting was adjusted after a miss.
Input is unpaced except the explicitly paced silent runs.

## Result

| Check | Observed | Interpretation |
| --- | --- | --- |
| Initial screen | 12/12 cases pass | Includes three detections of the original user tail at input time 3.66 s |
| Expanded screen | 45/63 cases pass; 24/42 intended wakes, no extras | Recognition gate fails; the driver exits 1 as required |
| Failing public controls | Wake-only, pause inside name, 10% volume and uniform noise: 0/3 detections each | Do not replace expected labels with recognizer output |
| User-tail volume controls | Gains 1 and 0.5 pass all twelve repetitions; gain 0.1 misses all six | Reuses one speaker and recording, not broader corpus coverage |
| Fresh-process peak RSS | At most 53.22 MiB in the expanded screen | Python adapter, native acoustic model and VAD only; no application budget passes |
| Paced silence CPU | 0.0557–0.0663% of one core | Three 17.01-second runs, including the declared two-second tail; isolated standby check only |

The shorter public wake cut and perturbed inputs retain their intended
positive labels. These results do not establish a regression relative to a
matched Vosk run, an end-to-end latency comparison or a general false-alarm
rate. No command was dispatched. The user selected the real recording; its
wording was an ASR draft during this screen and was subsequently
[confirmed by the user](benchmarks/user-tail-label-2026-10-27.json). No older group recording or
held-out audio was decoded for this experiment.

The small footprint and successful original Jarvis example justify reporting
the candidate, but its failed fixed recognition gate prevents promotion.
Do not port or integrate this configuration or tune it to one recording.
The next voice work needs representative labeled acoustic evidence and a
candidate that passes reliability as well as waiting/command memory gates.

A [subsequent fixed VAD/input-path diagnostic](wake-candidate-screening.md)
recovers these original misses but retains failures on new public positives.
It is a separate candidate and does not rewrite this failed result.
