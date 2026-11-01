# Configurable keyword-spotter experiment

Step 6 of the [memory plan](memory-plan.md), measured on Linux on 2026-10-26.
The isolated detector is promising for memory, but **has not replaced Vosk**.
It is not connected to the microphone, command matcher, settings or service.

The implementation in [native/kws-probe](../native/kws-probe) uses Rust for
bounded file I/O, pacing, configuration, measurements and native-handle ownership.
Inference uses sherpa-onnx 1.13.8's C API and its ONNX Runtime library. This is
**not a pure Rust inference implementation**. Both libraries and their state
are included in the detector's RSS/PSS. The normal Rust voice worker retains
its existing inference path and packaged binary.

## What was tested

The official English GigaSpeech 3.3M Zipformer model uses a tokenized keyword
file, rather than a trained classifier for each name. Its
[official model instructions](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html#sherpa-onnx-kws-zipformer-gigaspeech-3-3m-2024-01-01-english)
describe custom BPE keywords and detection thresholds. The experiment uses
int8 encoder, decoder and joiner, one CPU thread, four active paths, score 1,
one trailing blank, and thresholds 0.1/0.25/0.5/0.75. The primary paced arm
uses threshold 0.25, chosen before this matrix.

The CLI requires explicit library, model, token and keyword paths. A small
C shim builds the nested config using the pinned upstream header; Rust does
not replicate its ABI layout. The loader checks version 1.13.8 before creating
objects. Rust destroys result JSON, streams and the spotter before unloading
the library. Input PCM is s16le, 16 kHz, mono, read in 30 ms chunks, limited to
120 seconds per request. Each request starts a new stream; repeated phrases
within one request retain stream history. Trailing silence is explicit in
the fixture, and every successful trigger resets the stream as upstream's
examples do. There is no artificial allocator trim.

Custom names were prepared with the model's own SentencePiece BPE file:

```text
GO FORWARD  -> ▁GO ▁FOR WARD
JARVIS      -> ▁JA R VI S
HEY JARVIS  -> ▁HE Y ▁JA R VI S
```

This establishes token coverage, **not detection accuracy for Jarvis**. No
recordings of people saying Jarvis were supplied. `prepare_kws_keywords.py`
is a development helper; SentencePiece is installed only under the ignored
workspace build directory and never runs inside the probe or application.
The probe rejects unknown/blank tokens, duplicate aliases, oversized keyword
lists, and per-keyword score overrides so every matrix arm uses its stated
global settings.

The public `goforward.raw` fixture supplies surrogate wake “go forward” and
command “ten meters.” Controls include wake alone, a 300 ms pause within the
name, 10% amplitude, deterministic additive noise, command without wake,
reversed speech, silence, and three background → quiet → wake repetitions.
Two public speech clips from the upstream archive supply different phrases
(“light up” and “lovely child”) and unrelated-speech controls. Jarvis/Hey Jarvis
are tested only as absent keywords in those recordings. Perturbations and
fresh workers reuse the same recordings and do not add independent speakers.

All runs are sequential. Unpaced cases compare keyword labels and the exact
30 ms input chunk of each trigger against the upstream Python API using the
same model/settings. The reference process exits before each Rust measurement.
Three fresh primary workers replay selected controls at real-time speed.
The fixed Vosk comparison uses the normal Rust worker and fake capture,
three resident Vosk decoders, its existing energy gate, and accurate/noise
suppression disabled to isolate wake behavior. It dispatches no desktop actions.
These are different scopes: a KWS-only process versus a Vosk worker that also
contains command decoding. Their difference is an architectural opportunity,
not a demonstrated saving from replacing a single decoder.

## Evidence and limits

| Primary paced KWS measurement | Three fresh workers |
| --- | --- |
| Ready RSS, before input | 47.60–48.60 MiB |
| Peak RSS, native libraries included | 53.30–54.30 MiB |
| Final PSS | 48.91–49.96 MiB |
| Expected wakes detected | 15/15 |
| Extra triggers on selected controls | 0 |
| Silence CPU, one-core percentage | 1.40–1.42% |

For context, the fixed Vosk workers used 154.95–158.51 MiB RSS
(149.52–153.04 MiB PSS) in the first sampled 0.5–1.2 s ready interval.
Those worker-only samples exclude the fake Python capture producer. The six
Vosk cases detected 5/5 expected wakes and no extra wakes. KWS's figures cover
only the detector; removing Vosk would also remove command-decoding capability
unless a separate recognizer is brought in on demand.

| Unpaced surrogate threshold | Correct wakes | Misses | Extra triggers |
| --- | ---: | ---: | ---: |
| 0.1 | 24/24 | 0 | 0 |
| 0.25 | 24/24 | 0 | 0 |
| 0.5 | 15/24 | 9 | 0 |
| 0.75 | 12/24 | 12 | 0 |

Each row is three fresh processes using the same recordings. Threshold 0.5
misses the natural, wake-only and noisy clips; 0.75 also misses low-volume
speech. These failed cases remain in the artifact. The benchmark deliberately
exits **1** because the full sweep does not pass its expected labels. The
primary 0.25 cases pass, including 51/51 paced/unpaced cases and 39/39 wakes.
The two published phrases pass 6/6 repetitions, and the Jarvis-name negative
controls pass 9/9. All 147 unpaced cases match upstream's keyword labels and
exact trigger chunks, including upstream's misses. API agreement does not
turn those acoustic misses into passes.

Full measurements and every individual outcome are in
[raw results](benchmarks/kws-2026-10-26.json). Model, archive, native-library,
header and development-tokenizer content pins are in
[provenance](benchmarks/kws-provenance-2026-10-26.json). The GitHub release
supplies no publisher digest for this archive; the recorded SHA-256 is a local
content pin, not a publisher checksum. The archive and model stay in the
ignored build directory.

[Validation](benchmarks/kws-validation-2026-10-26.json): 322 offline Python
tests pass, 37 explicitly skip; all four probe model/protocol tests and three
Rust unit tests pass. Formatting, Clippy with warnings denied, source-package
header/license inclusion and recorded binary hashes pass. The full threshold
sweep's expected exit 1 is recorded separately from passing code checks.

The original ungated KWS loop continuously processes silence. Its observed CPU cost
exceeds the plan's roughly 0.1% of one core standby target. It needs an energy
gate that also passes onset, recovery and quiet-name tests. The wake trigger
also arrives after the following command has begun in the one-breath fixture;
a command recognizer started on the trigger needs buffered audio, with its
load time and overlap included in command latency and peak memory.
Specifically, the natural fixture triggers at input time 2.070 s (about
2.075 s wall time). Speech starts after 0.6 s of leading silence, and the
existing fixture's following-command cut is at 1.8 s: about 270 ms of command
audio precedes the trigger even before loading a recognizer. This cut is a
fixture boundary, not a manually annotated phonetic endpoint, and these are
wake timings rather than end-to-end command latency.

Real custom-name positives, confusable names, multiple speakers/accents,
music/game/dialogue negatives, GTCRN interaction, concurrent command-recognizer
loading, full application budgets, Windows native libraries/hardware, and
the full-duration soak remain unverified. The focused fixtures cannot estimate
a reliable false-alarm rate. Zero probe triggers on a negative clip is not a
measurement of false desktop actions: this probe cannot dispatch actions.
The model remains experimental and the full release gate stays false.

The [quiet-gate and buffered-handoff follow-up](kws-quiet-gate.md) is now
implemented and measured separately. An optional Rust gate reduces paced
silence CPU to 0.013–0.015% of one core, with 33/33 focused wakes and no extra
triggers, but delays the low-volume wake by 180 ms. A separate Rust buffered
handoff preserves the existing Vosk/free decoder/matcher and passes all 18
available fixture cases, including loading-delay retention. Combined worker-set
RSS is 594–617 MiB with small.en, so the default recognizer still fails the
command target in that experiment. Neither probe is integrated or promoted.

## Reproduce with already supplied assets

Build the optional probe; it is not part of the application build:

```sh
cargo build --release --locked --manifest-path native/kws-probe/Cargo.toml
cargo test --locked --manifest-path native/kws-probe/Cargo.toml
```

Using an environment with SentencePiece available, prepare separate keyword
files in a workspace directory:

```sh
python scripts/prepare_kws_keywords.py --model-dir "$MODEL" --output "$WORK/surrogate-keywords.txt" "go forward"
python scripts/prepare_kws_keywords.py --model-dir "$MODEL" --output "$WORK/published-keywords.txt" "light up" "lovely child"
python scripts/prepare_kws_keywords.py --model-dir "$MODEL" --output "$WORK/names-keywords.txt" "Jarvis" "hey Jarvis"
python scripts/benchmark_kws.py --model-dir "$MODEL" --library "$SHERPA_C_API" --keywords-dir "$WORK" --fixture "$PUBLIC_PCM" --work-dir "$WORK/matrix" --voice-binary native/voice-worker/target/release/jarvis-voice --output "$WORK/results.json"
```

`MODEL`, `WORK`, `SHERPA_C_API` and `PUBLIC_PCM` designate explicit local
assets. The benchmark never downloads or installs them. Optional integration
tests use `MURMUR_KWS_MODEL`, `MURMUR_KWS_LIBRARY` and
`MURMUR_SPEECH_TEST_RAW`; without those, model tests skip explicitly.
