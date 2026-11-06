# Documentation

Start with the root [README](../README.md) for installation and everyday use.

| Document | Purpose |
| --- | --- |
| [Arabic speech](arabic.md) | Najdi-focused model comparison, chosen engine, Arabic commands and evidence limits |
| [Dictation](dictation.md) | Typing speech into other apps, safety rules and per-platform input methods |
| [Clips editor](clips.md) | Trim/join/export, Fast vs Exact, player findings and the LosslessCut decision |
| [Architecture](architecture.md) | Process ownership, voice path, bounds and trust boundaries |
| [Performance](performance.md) | Current CPU/RAM measurements and reproduction |
| [Memory and background check](memory-background.md) | Allocator comparison and live background verification |
| [Memory-plan measurement](memory-measurement.md) | Process accounting, application baselines, recognition corpus and 24-hour soak gates |
| [Memory-plan progress](memory-plan-progress.md) | How far the six steps have progressed, acceptance gaps, remaining order and timing limits |
| [Local memory activation](local-memory-activation.md) | User-enabled Linux disk replay, live verification, privacy/storage behavior and remaining memory work |
| [Live replay validation](replay-live-validation.md) | Persistent saved clips, configured history duration, local decode and Stop/restart checks |
| [Memory-plan code review](memory-plan-code-review.md) | Full-plan review scope, fixed lifecycle/measurement/recovery bugs and regression results |
| [Memory-plan handoff](memory-plan-handoff.md) | Current state, measured results, local assets and the next Rust KWS experiment |
| [Review handoff](review-ready.md) | Where to review and what remains unverified |
| [Windows release](windows.md) | Windows installer/ZIP, build instructions and hardware acceptance |
| [Light command model](light-model.md) | Whisper tiny.en option: half the peak memory, measured accuracy limits |
| [Windows memory](windows-memory.md) | 0.3.0 Rust voice worker on Windows: method, before/after RAM and latency, open latency regression |
| [Build container](../packaging/container/README.md) | Linux container that tests Jarvis and builds the Windows installer |
| [Feature review](feature-review.md) | Recording settings and deferred Discord work |
| [Local refactor review](refactor-review.md) | October 24 refactor, equivalence checks and measured CPU/RAM results |
| [Voice models](speech-research.md) | Current model path and public test fixture |
| [Rust voice worker](rust-voice.md) | Optional native worker, build/selection and measured memory comparison |
| [Rust denoiser](rust-denoiser.md) | Rust GTCRN computation, standalone binary and waveform verification |
| [Rust Whisper](rust-whisper.md) | Rust recognition computation, cache reconstruction and memory/latency tradeoffs |
| [Voice-worker memory](rust-worker-memory.md) | Process memory audit and lossless Whisper embedding compaction |
| [Model loading memory](rust-memory-loading.md) | Incremental Rust weight loading, cache ownership and startup/peak RAM comparison |
| [On-demand Whisper](whisper-on-demand.md) | Loading Whisper on a wake name, idle release, and measured RAM/latency tradeoff |
| [Vosk decoder history](vosk-decoder-history.md) | Isolated wake, command and free-decoder history measurements; production reset policy unchanged |
| [Vosk utterance finalization](vosk-finalization.md) | Default-disabled trial at real Engine boundaries, with paired memory, recognition and latency checks |
| [Smaller Whisper experiments](whisper-model-experiments.md) | Rust tiny/base/small profiles, paired measurements and the open base.en reference discrepancy |
| [Configurable wake experiment](kws-experiment.md) | Isolated Rust KWS probe, native-backend accounting, threshold controls and remaining integration gates |
| [Quiet gate and buffered handoff](kws-quiet-gate.md) | Optional Rust energy gate, sequential saved-baseline comparison, buffered command loading and combined worker peaks |
| [Model-aligned KWS experiment](kws-alignment.md) | Fixed model-derived stream alignment, expanded phase coverage, timing improvement and the retained missed-wake failure |
| [Owned KWS readers and caches](kws-reader-caches.md) | Joined/cancellable child readers, cache-versus-lifetime attribution and lifecycle/pressure regression controls |
| [KWS allocation and pressure](kws-pressure-allocations.md) | Rust/native allocation ownership, longer floor controls and bounded recovery under real private cgroup limits |
| [Continuous handoff diagnostics](kws-continuous-diagnostics.md) | Multi-window file input, busy/timeout/recovery controls, context timing diagnosis and parent/detector floor attribution |
| [Handoff cycles and endpoints](kws-handoff-cycles.md) | Persistent-detector cycles, released waiting floors, quiet-command failure, candidate endpoint and ten-cycle trend |
| [Vosk fast-path experiment](vosk-fast-path.md) | Exact agreement can avoid Whisper; measured cold fallback regression keeps the experiment disabled |
| [Rust controller](rust-controller.md) | Native service API, process ownership, compatibility checks and controller memory comparison |
| [Rust CLI](rust-cli.md) | Native controls, Waybar, aliases, diagnostics and verified setup |
| [Recording](recording.md) | Permissions, audio sources, encoders and recovery |
| [UI](ui.md) | Native settings interface and accessibility checks |
| [Troubleshooting](troubleshooting.md) | Operational errors and recovery |
| [Verification](verification.md) | Automated and interactive acceptance checks |
| [Dependencies](dependencies.md) | Model provenance and third-party notices |

Raw measurement JSON lives in [benchmarks/](benchmarks/). Earlier engineering
notes are preserved in [history/](history/) and describe their dated versions.
They do not override current setup instructions or performance claims.
