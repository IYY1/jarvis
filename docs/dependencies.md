# Dependencies, model provenance and release decisions

Jarvis source uses the [MIT license](../LICENSE). Third-party libraries and
models retain their own terms. This repository has not been published yet.

| Component | Role | Upstream terms / packaging notes |
| --- | --- | --- |
| Python 3.11+ | Service and UI client logic | Python Software Foundation license; distro runtime |
| Vosk 0.3.45 | Default wake detection and command boundaries | Apache-2.0; downloaded small English model has separate notices |
| sherpa-onnx 1.13.8 | CPU Whisper inference and optional enhancement | Apache-2.0; models downloaded separately |
| Whisper small.en | Accurate English command transcription | MIT upstream model; int8 ONNX conversion from sherpa-onnx releases |
| PocketSphinx 5.1.1 | Fallback CPU VAD/KWS/finite command grammar | BSD-style notices; pinned optional voice dependency |
| PocketSphinx bundled en-US acoustic model | US English recognition | Alpha Cephei BSD-style model terms; preserve its README/notices in a binary distribution |
| CMUDict | Pronunciations | CMU redistribution conditions; preserve notice |
| GTK 4 / PyGObject / GLib-GIO | Native UI and desktop launching | LGPL-family components; use distro shared libraries and preserve applicable notices |
| PipeWire / WirePlumber | Input capture and volume | MIT-family projects; distro components |
| wf-recorder | Optional capture backend | MIT; invoked as a separate installed program |
| GPU Screen Recorder | Optional instant replay and HDR recording backend | GPL-3.0-only; invoked as a separate installed program; monitor capture uses its installed privileged KMS helper |
| FFmpeg | Encoder/backend linkage and validation | Exact build may be LGPL or GPL depending on enabled codecs; libx264 is GPL. Review the actual build before redistributing binaries |
| psutil | Optional benchmark tooling only | BSD-3-Clause; never imported by the service |

This is an inventory, not a combined license grant. Linux distributions may
apply additional packaging notices. No models or dictionaries are bundled in
this repository. `pip install -e '.[voice]'` obtains the pinned upstream
PocketSphinx package, whose wheel contains its own assets and notices. The
general language model shipped upstream is present on disk but never loaded by
Jarvis. Packaging source does not imply permission to strip notices or relicense
those assets. The upstream model terms permit redistribution subject to their
conditions; any future bundled installer must include the actual notices.

Evidence and model sizes are linked in [speech research](speech-research.md).
Official dependency license sources include [PocketSphinx](https://github.com/cmusphinx/pocketsphinx/blob/main/LICENSE),
[model notice](https://github.com/cmusphinx/pocketsphinx/blob/main/model/en-us/en-us/README),
[CMUDict](https://github.com/cmusphinx/cmudict/blob/master/LICENSE),
[GTK](https://gitlab.gnome.org/GNOME/gtk/-/blob/main/COPYING),
[PyGObject](https://gitlab.gnome.org/GNOME/pygobject/-/blob/main/COPYING),
[wf-recorder](https://github.com/ammen99/wf-recorder/blob/master/LICENSE), and
[FFmpeg legal information](https://ffmpeg.org/legal.html).

sherpa-onnx 1.13.8 (Apache-2.0) runs the optional accurate command model,
OpenAI Whisper small.en (MIT) converted to int8 ONNX by the sherpa-onnx project,
and the optional GTCRN speech-enhancement model; both are downloaded from the
sherpa-onnx GitHub releases with pinned SHA-256 values, never bundled.
Vosk 0.3.45 (Apache-2.0) is part of the `[voice]` extra. Its small English model
(`vosk-model-small-en-us-0.15`, Apache-2.0, 40 MB) is not bundled: `jarvis
install-model` downloads it from alphacephei.com and checks a pinned SHA-256.
openWakeWord was researched but is not installed. Its pretrained model terms
are separate from its code license.

The optional Rust Whisper and GTCRN implementations use tract-onnx 0.23.9 and RustFFT 6.4.1
(each MIT OR Apache-2.0). It reads the same separately installed model weights.
Their exact transitive dependencies are recorded in the Cargo lockfiles.
The Python reference path continues to use sherpa-onnx/ONNX Runtime; the Rust
recognizer and denoiser's neural and spectral computations do not use those libraries.
Whisper also uses Rayon 1.12.0 (MIT OR Apache-2.0) for its bounded CPU pool and
base64 0.22.1 (MIT OR Apache-2.0) for the existing token table. Its full-precision
embedding and int8 linear weights retain the model's existing precision.

The optional [Rust controller](rust-controller.md) uses Tokio 1.54.1 for its
current-thread event loop, Unix sockets, subprocesses and bounded channels.
serde/serde_json, regex, Unicode normalization/case folding, shlex and libc
support the existing configuration, parser and desktop integration rules.
Its exact dependency versions are recorded in `native/controller/Cargo.lock`.
It links neither Python/GTK nor a speech inference runtime.

The optional [Rust CLI](rust-cli.md) shares the controller library and uses
ureq 3.5.0 with rustls for HTTPS, sha2 for checksums, zip 9.0.5, tar and
bzip2-rs for streaming model extraction. It does not invoke Python, curl,
unzip or tar for setup. `native/cli/Cargo.lock` records transitive versions.
The existing settings window still requires Python/GTK.

Before a public release: complete the engineers' review, validate a representative
voice/noise corpus, review distribution notices, and exercise the gaming/audio/scaling checks in
[verification](verification.md). No remote repository was created or published.
