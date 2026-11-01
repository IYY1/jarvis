# Voice models and test fixtures

The default installed voice path uses Vosk 0.3.45 with
`vosk-model-small-en-us-0.15` for wake detection and command boundaries, then
sherpa-onnx 1.13.8 with int8 Whisper small.en for command transcription.
PocketSphinx 5.1.1 is the fallback when the Vosk model is unavailable.
All recognition uses the CPU. Optional GTCRN enhancement remains off by default.

`./run-jarvis install-model` downloads the configured artifacts, verifies their
SHA-256 values and extracts only required files. Model specifications, URLs,
sizes and hashes are in `src/murmur/models.py`. Models are not vendored.

The Python worker keeps Whisper resident so commands do not reload hundreds of
megabytes of weights. The Rust worker instead loads it when a wake name is heard
and releases it after a minute without a command; see
[on-demand Whisper](whisper-on-demand.md) for the memory/latency tradeoff.
ONNX allocation and thread-pool tuning reduce idle memory and CPU work
without replacing the recognizer. See [performance](performance.md) and
[memory/background verification](memory-background.md) for measured limits.

The [smaller-model experiments](whisper-model-experiments.md) add explicit
Rust file-based tiny.en/base.en profiles for measurement. The installed model
catalog still selects small.en. Tiny matched the available fixture comparison;
base has an open reference discrepancy, and neither is a default-model change.

## Reproducible public fixture

The fixture used for allocator/thread comparisons is the upstream PocketSphinx
`goforward.raw`, mono 16 kHz signed 16-bit little-endian PCM, 2.78625 seconds:

```sh
curl --fail --location https://raw.githubusercontent.com/cmusphinx/pocketsphinx/main/test/data/goforward.raw -o /tmp/goforward.raw
sha256sum /tmp/goforward.raw
# f15c60ec54059d8b66e410d0064945a0b0a04ea56e1ddca1958e493c0cf70e71
.venv/bin/python scripts/benchmark_transcriber.py --fixture /tmp/goforward.raw --baseline
.venv/bin/python scripts/benchmark_transcriber.py --fixture /tmp/goforward.raw
```

Tests and benchmarks never download this fixture automatically. Fixture
perturbations test resource use and transcript equivalence; they do not prove
accuracy across speakers, accents or background dialogue. The earlier synthetic
voice comparison is retained in [development history](history/development-notes.md#accurate-recognition-upgrade-october-2026).
Initial backend research is in [the historical record](history/speech-research-2026-10-20.md).

The [configurable keyword-spotter experiment](kws-experiment.md) measures an
isolated Rust probe backed by native sherpa-onnx/ONNX Runtime. It has not
changed the default wake path or established acoustic quality for Jarvis.
