# Light command model (Whisper tiny.en)

Jarvis' peak memory comes from Whisper, which loads when you say the wake name
and unloads a minute after your last command. Since 0.3.1 the default command
model is **Light** (Whisper tiny.en): it halves the peak and answers faster.
**Settings → Voice → Command model → Small** switches back to Whisper small.en,
which is better on unusual words. If only one of the two models is installed
(for example an install updated without re-running the model download),
Jarvis uses the installed one instead of dropping to the small Vosk model.

`jarvis install-model` (and the Arch installer) downloads both models, and the
Windows installer bundles both, so you can switch without a download. The
tiny.en archive is the official sherpa-onnx release
(`sherpa-onnx-whisper-tiny.en.tar.bz2`, SHA-256 `2bd6cf96…05dd`); its int8
files match the hashes pinned in
[the model provenance record](benchmarks/whisper-model-provenance-2026-10-26.json).
The Rust recognizer already supported the tiny.en export
([experiments](whisper-model-experiments.md)); this change makes it selectable.

## Measured on Linux (2026-11-01)

Rust voice worker with noise suppression, Ryzen 7 7800X3D, paced injected audio:
five wake + command cycles of the public “go forward ten meters” fixture per
run, two runs per model, RSS/PSS sampled every 50 ms.

| | small.en | tiny.en (Light) |
| --- | ---: | ---: |
| Waiting for the wake word | 157 MiB | 158 MiB |
| **Peak RSS** | **546 MiB** | **280 MiB (−49%)** |
| Peak PSS | 538 MiB | 271 MiB |
| Command time after speech ends, median | 0.33 s | 0.16 s |
| Commands correct | 10/10 | 10/10 |

Waiting memory is unchanged because Whisper is not loaded then. Windows was not
re-measured; the same model saves the same memory there, so its 533 MiB peak
should fall by roughly 250 MiB, but that is an expectation, not a measurement.

## Accuracy evidence and limits

- The user's confirmed recording of “Jarvis, clip that”: both models recognize
  the command in the paced worker (peak 543 vs 261 MiB in that run).
- 305 ten-second chunks of the user's own screen recordings (about 47 minutes,
  mostly game audio and conversation), transcribed by both models. Seven chunks
  contain a command for either model: same decision on 3, tiny.en right where
  small.en was wrong on 2 (for example “open Steam”), small.en right where
  tiny.en was wrong on 2 (for example tiny.en heard “Alfred, clip that” as
  “I'll thread clip that”). Median transcription time per chunk: 0.96 s vs 0.42 s.
- The two transcripts often differ on background conversation, where neither
  model is ground truth; that does not affect commands, which must match the
  command list.
- This is one speaker and a handful of commands, not a broad accuracy study.
  Try Light with your own voice; switch back if commands are misheard.
  Transcripts of the private recordings are not stored in the repository.
