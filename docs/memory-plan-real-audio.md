# Memory plan: first user-selected command tail

On 27 October 2026 the user made a fresh clip and asked for its final five
seconds to be checked. Only that tail was extracted, as mono 16 kHz s16le PCM
(4.9867 decoded seconds). The source, extracted audio and full transcripts stay
under ignored, private `build/memory-plan-corpus/`; none is included here or
uploaded to a review service. The older recordings contain other people and
remain unlabeled. The recordings reserved before inspection remain held out.

Two local Whisper small.en reference runs, with and without GTCRN, agree on
“Jarvis, clip that.” The unchanged Rust voice engine recognizes the wake and
`clip that` in both configurations. The existing parser maps that phrase to
Save Replay. At measurement time the wording was provisional ASR evidence.
The user later explicitly confirmed the intended phrase as “Jarvis, clip that”; see the
[human label record](benchmarks/user-tail-label-2026-10-27.json). This confirms
one positive command, not full recognition acceptance.

Paced playback through the real worker and a fake capture child gives:

| Command profile | Wakes / commands | Sampled tree RSS peak | Sampled tree PSS peak |
| --- | --- | --- | --- |
| Production small.en | 1 / 1, `clip that` | 543.44 MiB | 522.53 MiB |
| Explicit experimental tiny.en | 1 / 1, `clip that` | 260.65 MiB | 240.87 MiB |

These measurements include voice and fake capture only. They exclude the
controller, real replay, settings and Windows, and establish no latency
nonregression. Tiny.en remains experimental. Each arm has one fresh worker and
one command; this adds one recording, not a representative corpus.

The configurable KWS experiment detects **no wake** at its unchanged 0.25
threshold in any of its three arms: ungated, original quiet gate, or aligned
quiet gate. All three also miss in paced playback with two explicit seconds
of trailing silence. This controls for the file ending before a delayed
detection. No threshold, gain, keyword spelling or pre-roll was tuned to this
clip. The detector remains unsuitable for integration on this evidence.

Two early paced harness attempts stopped before decoding because the private
fake-input configuration lacked consent. A separate summary-printing error
occurred after persisting that attempt's data. Those attempts are preserved;
the corrected, consented runs are separate evidence. No real microphone or
desktop action was used by these tests.

The [sanitized measurement artifact](benchmarks/memory-plan-user-tail-2026-10-27.json)
records hashes, counts, sampled peaks and the false release gates. Full corpus,
combined memory, Windows, pressure and final soak requirements remain open.

An independent [minimal Vosk residency control](benchmarks/vosk-minimal-residency-2026-10-27.json)
also checks a possible alternative to KWS. With the native model loaded, adding
minimal wake, command and free decoders adds only about 3.2 MiB. The diagnostic
process includes Python and its meter, and uses smaller grammars than the
application. It establishes no shipping footprint or recognition result, but
does not support pursuing decoder deferral alone as a way to save the tens of
MiB missing from the waiting budget. No production decoder policy changed.

[Subsequent wake screening](wake-candidate-screening.md) distinguishes tiny
command recognition from wake recognition and records further fixed candidates.
