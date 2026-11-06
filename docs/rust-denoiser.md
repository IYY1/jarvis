# Rust GTCRN denoiser

The denoising computation now runs in Rust. This is separate from the earlier
worker port, which called sherpa-onnx's existing C++ denoiser through its C API.
That earlier version did **not** implement the denoiser in Rust.

`native/gtcrn-denoiser/src/lib.rs` implements spectral analysis, recurrent-cache
handling, inverse transforms, overlap-add and normalization. Neural-network
operators execute through tract's Rust CPU implementation. RustFFT implements
the Fourier transforms. The existing `gtcrn_simple.onnx` supplies the trained
graph and weights; it is a data file here. This implementation does not use
the ONNX Runtime inference engine or call a Python/C++ denoiser.

The standalone `jarvis-denoise` binary links only the platform C runtime,
math library and compiler support runtime on the tested Linux machine. `ldd`
shows no Python, sherpa-onnx, ONNX Runtime or C++ runtime dependency. The voice
worker now also uses the [Rust Whisper recognizer](rust-whisper.md), and no
longer links sherpa-onnx/ONNX Runtime. GTCRN calls the Rust library directly.
Vosk wake detection and boundary decoding still use the existing native library.

## Behavior and scope

The implementation accepts 16 kHz mono audio and the installed version-1
GTCRN model. It uses 512-point transforms, a 256-sample hop, a square-root
periodic Hann window, centered reflection padding, and the model's three
explicit recurrent cache tensors. Caches reset between commands. Internal
GRU/Scan state resets for each model invocation; only the explicit caches
survive between spectral frames. This distinction is covered by repeated
waveform comparisons.

Like the existing backend, output length is `floor(input_samples / 256) * 256`.
Command processing is bounded to ten seconds. No resampling, model retraining,
new weights, speaker extraction or speech recognition algorithm is introduced.
GTCRN suppresses noise; it does not select one speaker from competing voices.

## Build and use

Use Rust 1.91 or later and the existing local models. Build both the standalone
denoiser and voice worker, including their unit tests:

```sh
.venv/bin/python scripts/build_voice_worker.py --test
```

The standalone denoiser also builds independently, without sherpa-onnx or Python:

```sh
cargo build --release --locked --manifest-path native/gtcrn-denoiser/Cargo.toml
native/gtcrn-denoiser/target/release/jarvis-denoise \
  /path/to/gtcrn_simple.onnx /path/to/input.s16le /path/to/new-output.f32le
```

The input is raw 16 kHz mono signed 16-bit little-endian PCM. The output is
raw 32-bit little-endian floating-point audio and must be a new path.
`--batch MODEL` accepts input/output path pairs separated by a tab on stdin
and retains the model between commands. `--spectra` is an offline numerical
diagnostic that bypasses the audio transforms.

For application integration, select `JARVIS_VOICE_WORKER=rust` on the daemon
and enable the existing noise-suppression setting. See [worker selection](rust-voice.md#build-and-select).
The native worker's `inference.rs` now calls `jarvis_gtcrn::Denoiser::process`;
it no longer creates a sherpa-onnx speech denoiser. Python remains an available
reference path, and the existing user settings and running service are not
changed by building this code.

## Verification

The check compares actual floating-point audio output with the existing
sherpa-onnx denoiser in separate fresh processes. It exercises seven variants
of the public PocketSphinx fixture, repeated three times, and verifies that
repeating a command produces identical output after resetting state. It also
checks the standalone binary's linked dependencies.

```sh
.venv/bin/python scripts/check_rust_denoiser.py \
  --fixture /tmp/goforward.raw --output /tmp/rust-gtcrn-comparison.json
```

The script uses only the explicitly supplied fixture; it does not open a
microphone, download a model or execute desktop actions. Acceptance limits are
maximum absolute sample error of `1e-4` and relative RMS error of `1e-3`.
These allow floating-point differences between independent FFT/neural kernels;
matching transcripts alone would not establish equivalent denoising.

On 2026-10-22, all 21 waveform pairs passed. The largest absolute sample error
was `3.67e-7`; the largest relative RMS error was `5.34e-7`. Repeated commands
produced identical Rust output, confirming state reset. In fresh standalone
processes, retained RSS was **64.0 MiB** for the Python/native reference and
**24.5 MiB** for Rust; peak RSS was 67.8 and 25.8 MiB. Median denoising time
was 92.8 and 67.6 ms on this small fixture set. The reference used its existing
eight-thread setting; tract used its default CPU execution.

Before the Whisper rewrite, with native Whisper also loaded, retained RSS was **453.5 MiB** for the Python path
and **437.3 MiB** for the Rust path, a much smaller 16.2 MiB reduction.
All 21 paired transcripts matched. Those timings do not establish general
performance or denoising quality across speakers and environments.
The current fully Rust Whisper/GTCRN measurement is documented in
[Rust Whisper](rust-whisper.md); the preceding 16.2 MiB figure is historical.

Raw results: [standalone waveform/memory comparison](benchmarks/rust-gtcrn.json)
and [Whisper plus actual Rust GTCRN](benchmarks/rust-transcriber-native-gtcrn.json).

Source references used to match the current model's conventions:

- [sherpa-onnx 1.13.8 GTCRN pipeline](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/sherpa-onnx/csrc/offline-speech-denoiser-gtcrn-impl.h)
- [kaldi-native-fbank 1.22.3 STFT](https://github.com/csukuangfj/kaldi-native-fbank/blob/v1.22.3/kaldi-native-fbank/csrc/stft.cc)
- [kaldi-native-fbank 1.22.3 inverse STFT](https://github.com/csukuangfj/kaldi-native-fbank/blob/v1.22.3/kaldi-native-fbank/csrc/istft.cc)
- [tract Rust CPU inference](https://github.com/sonos/tract)

The first worker-wrapper benchmarks remain historical measurements. They must
not be presented as measurements of this Rust neural denoiser. Standalone
memory also includes the Python interpreter and native runtime in the reference
process; it does not predict the same savings when ONNX Runtime is already
resident for Whisper. Public fixture checks do not establish quality across
speakers, room noise or microphones.
