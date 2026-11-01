# Smaller Whisper experiments — 2026-10-26

The Rust recognizer can now load narrowly validated sherpa int8 exports for
English **tiny.en, base.en and small.en**. This is experimental file-based
support. The installed model catalog, settings, controller defaults and model
discovery still select small.en. Models were downloaded only into the benchmark
workspace; no user model installation or service restart was performed.

Tiny.en is promising on the available fixture. Base.en has an unresolved
reference-transcript discrepancy. **Neither smaller model is adopted as the
default**, and the full recognition corpus and Windows/whole-app budgets remain
unverified. Moonshine and keyword-spotter integration remain future work;
an [isolated configurable KWS probe](kws-experiment.md) has now been measured.

## Rust changes and provenance

A fixed profile allowlist defines width, attention heads, layers and separate
encoder/decoder wiring signatures. Tiny.en uses 384/6/4; base.en uses 512/8/6;
small.en retains 768/12/12. Metadata must match that profile, including English
token IDs, 80 mel bins, vocabulary size, context and head counts. Duplicate
metadata, unknown profiles, mismatched graphs and unexpected shapes fail.
Existing bounded imports, lossless embedding storage, Rust kernels, decoding
policy, free Vosk decoder and command matcher remain in place. The fast-path
and utterance-finalization experiments are disabled in these measurements.

Models come from the maintainer repositories linked by the
[official sherpa export documentation](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/whisper/export-onnx.html).
The [provenance record](benchmarks/whisper-model-provenance-2026-10-26.json)
pins repository revisions and records file SHA-256 values, sizes, metadata and
wiring signatures. ONNX downloads were checked against upstream LFS SHA-256
values. Token tables were fetched at the pinned revision and hashed locally.
The signatures check export compatibility, not cryptographic model integrity.

The existing small.en path was checked before and after generalization:
[all 55 saved tensor pairs across seven fixture variants were byte-identical](benchmarks/whisper-profiles-small-equivalence-2026-10-26.json).
No new inference dependency was added to the application.

## Paired measurements

[Raw results](benchmarks/whisper-models-2026-10-26.json) use three fresh,
sequential isolated Rust processes and three fresh paced voice workers per
profile. Isolated workers each decode seven variants of the same public
`goforward.raw` fixture three times, comparing transcripts to sherpa-onnx with
the **same model**. These 63 comparisons per profile are repeated perturbations
of one recording, not 63 speakers or a broad accuracy corpus.

Paced workers use real Vosk, Whisper and GTCRN, with prefetch and a 60-second
idle release. Each receives three wake/commands, for nine commands per profile.
The surrogate wake is “go forward” and “ten meters” maps to a test action.
No actual microphone or desktop action runs. Measurements cover the Linux
voice worker only, excluding capture, controller, replay and settings.

| Measurement | small.en default | base.en experiment | tiny.en experiment |
| --- | ---: | ---: | ---: |
| Exact reference transcripts | 63/63 | 45/63 | 63/63 |
| Correct paced commands | 9/9 | 9/9 | 9/9 |
| Isolated retained RSS | 320.61–321.08 MiB | 135.81–136.35 MiB | 92.13–92.91 MiB |
| Full-worker peak RSS | 548.31–570.70 MiB | 329.23–330.81 MiB | 278.98–282.04 MiB |
| PSS sampled after commands | 478.27–480.97 MiB | 293.60–297.11 MiB | 250.96–254.67 MiB |
| Median command latency | 1029.8 ms | 948.2 ms | 807.4 ms |
| Small-sample p95, nearest rank | 3296.2 ms | 2332.6 ms | 1183.2 ms |

All latency samples are retained. The first group had outliers, including a
3296.2 ms **warm** small.en command and a 2332.6 ms cold base.en command. They
are not evidence of an inherent load penalty or a general p95 speedup. As a
sensitivity check, the last two workers per profile had maximum latencies of
1214.5 ms small.en, 1014.8 ms base.en and 990.9 ms tiny.en. That subset does
not replace the nine-sample primary results. Fresh process loading also does
not imply a cold OS file cache.

The savings do not establish the combined 150/300/400 MiB application budgets,
Windows Private Working Set/commit, standby CPU, pressure behavior or a soak.
The always-resident Vosk path is unchanged. Disk replay and the settings window
must still be included in application-wide acceptance.

## Base.en reference discrepancy

Base.en drops final punctuation on the noisy variant and repeats “go” on the
one-second truncated variant, where sherpa-onnx returns “Go Forward.” Those
two differences recur in every repeat and worker. The benchmark correctly
exits nonzero after saving the complete comparison; its reference gate fails
for base.en even though all full-worker fixture commands match.

A [separate numerical diagnostic](benchmarks/whisper-base-numerical-diagnostic-2026-10-26.json)
compares the original ONNX graphs with the Rust path using ONNX Runtime 1.31.0
installed only in the workspace's diagnostic directory. Decoder argmax agrees
at every sampled step when supplied the same Rust encoder tensors and token
history. Encoder key relative RMS differences are about 3.0–3.3%, and value
differences about 5.3–6.1%, on the two problem variants. This points to encoder
numerical differences as a source of the divergent decoding, but does not
identify a particular operator or establish full numerical equivalence.
Base.en remains unvalidated; the discrepancy is not normalized away or counted
as a transcript pass. Tests retain full-utterance/reference and cache-reset
checks, while the strict benchmark continues to flag the two problem variants.

## Focused command controls

All three profiles passed the same five paced controls: natural command-tail
speech without a wake, reversed speech without a wake, a two-second pause after
the wake, a 300 ms pause inside the surrogate wake name, and three repeated
background/quiet/wake cycles without added noise. Each arm returned all five
expected commands/wakes with **zero false actions and extra wakes**.

Raw controls: [small.en](benchmarks/whisper-small.en-controls-2026-10-26.json),
[base.en](benchmarks/whisper-base.en-controls-2026-10-26.json),
[tiny.en](benchmarks/whisper-tiny.en-controls-2026-10-26.json). They are variants
of the same recording, and all retain `passed: false` with expected exit 1:
recorded real commands, confusables, music and game audio are missing. Passing
these controls does not override base.en's separate failed transcript gate.

The [validation record](benchmarks/whisper-models-validation-2026-10-26.json)
records 320 passing offline tests (35 skipped), 60 passing selected voice/model
tests, 34 passing Rust tests, format checks and clean all-target Clippy with
warnings denied. Coverage includes profile metadata and shape rejection,
encoder/decoder mismatch rejection, cache resets, the existing small.en path,
and the absence of a foreign inference runtime in Rust binary mappings. Both
rebuilt binaries match the measured hashes. The matrix's expected nonzero exit
and base.en's failed reference gate remain separately recorded.

## Reproduce

Supply the existing local models and public fixture; the benchmark never
downloads or installs them:

```sh
python scripts/build_voice_worker.py --test
python scripts/benchmark_whisper_models.py \
  --fixture build/refactor-review/goforward.raw \
  --small /path/to/sherpa-onnx-whisper-small.en \
  --base /path/to/sherpa-onnx-whisper-base.en \
  --tiny /path/to/sherpa-onnx-whisper-tiny.en \
  --output docs/benchmarks/whisper-models.local.json
```

The standalone Rust CLI accepts `--model-name tiny.en|base.en|small.en`, default
small.en, with `--batch` or `--diagnose`. The acceptance runner accepts explicit
`--whisper-model-dir` and `--whisper-model-name` arguments without changing
service configuration. Missing corpus categories still fail that gate.

Next decisions need recorded commands across speakers/accents, confusables,
unrelated speech, music/game audio, real-machine memory/latency and the open
base.en numerical investigation. Fixture-only savings cannot authorize a
recognition-model switch.
