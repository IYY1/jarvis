# Arabic speech (Najdi and Saudi first)

**Voice → Language → Arabic and English** adds a local Arabic recognizer beside
the English one. **Turn on Arabic** / **Turn off Arabic** on Overview switches
in one click, and so does a terminal:

```sh
jarvis language arabic
jarvis language english
jarvis language
```
 Two separate abilities come with it:

- **Arabic commands**: a fixed list of Arabic wordings (Najdi/Saudi first, then
  formal Arabic) that map onto the existing commands. Only these run actions.
  They are listed under **Commands → Arabic**.
- **Arabic dictation**: types whatever Arabic you say into the focused text
  field ([dictation](dictation.md)). Dictated speech never runs a command.

English commands and English dictation keep working unchanged. The wake name
stays as it is (“Jarvis”, said the same way in an Arabic sentence).

Install the model once (about 118 MB, checked by SHA-256):

```sh
jarvis install-model --arabic
```

or use **Download the Arabic model** in Voice settings. It is published as the
[models-arabic-1 release](https://github.com/abdullaho21/jarvis/releases/tag/models-arabic-1);
`--archive FILE` installs a local copy instead.

## What was chosen, and why

The Arabic engine is **Moonshine Streaming Tiny Arabic**
([model card](https://huggingface.co/moonshine-ai/moonshine-streaming-tiny-ar),
MIT, 27 M parameters). It was the most accurate MIT/Apache-licensed model on
Saudi speech in the test below, while being one of the smallest and fastest.
Whisper tiny.en stays the English path.

| Rank | Model | License (checked per model) | Najdi WER | Hijazi | Khaliji | FLEURS (Egyptian) | Arabic+English | Speed (RTF) | Notes |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | **Moonshine Streaming Tiny Arabic** | MIT | **52.2** | **47.9** | 72.3 | 7.9 | 38.0 | 0.022 | Chosen. Pseudo-labelled podcast/video training data. |
| – | Voho Saudi STT small (Whisper-small fine-tune) | **CC BY-NC-SA 4.0** | 35.1 | 40.2 | 41.2 | 29.7 | 51.1 | 0.40 | Best on Saudi speech, but non-commercial and ~1 GB; not shipped. |
| – | Moonshine base Arabic (sherpa-onnx package) | **Moonshine Community License (non-commercial)** | 48.0 | 56.5 | 72.9 | n/a | n/a | 0.015 | Returns nothing for clips over ~9 s in sherpa-onnx 1.13.8. |
| 2 | Qwen3-ASR 0.6B int8 | Apache-2.0 (upstream) | 58.4 | 56.0 | 60.6 | 23.4 | 45.0 | 0.16 | ~950 MB download, ~2.4 GB RSS: too heavy. |
| 3 | Omnilingual ASR 300M CTC int8 | Apache-2.0 | 64.2 | 58.9 | 62.4 | 16.0 | 40.3 | 0.064 | 1,600 languages; ~1 GB RSS. |
| 4 | Vosk Arabic MGB-2 0.4 | Apache-2.0 | 68.7 | 67.5 | 76.5 | 15.8 | 42.1 | 0.36 | Broadcast MSA; static graph, so no command grammar. |
| 5 | Zipformer streaming ar/en/… (2025-02-10) | Apache-2.0 | 70.6 | 71.8 | 76.5 | 13.9* | 28.3 | 0.067 | True streaming; *FLEURS likely in its training data. |
| 6 | Whisper small (multilingual) | MIT | 74.0 | 66.0 | 80.6 | 26.8 | 43.7 | 0.23 | |
| 7 | Whisper base (multilingual) | MIT | 85.1 | 84.2 | 84.1 | 51.4 | 74.4 | 0.073 | |
| 8 | Whisper tiny (multilingual) | MIT | 93.1 | 91.9 | 102.9 | 67.5 | 80.3 | 0.042 | |

WER is word error rate in percent (lower is better). NVIDIA's Arabic
FastConformer (`stt_ar_fastconformer_hybrid_large_pcd_v1.0`, CC-BY-4.0,
~115 M parameters, 459 MB `.nemo`) was not benchmarked: it needs the NeMo/torch
export toolchain and has no ready ONNX package. On the
[Open Universal Arabic ASR leaderboard](https://huggingface.co/spaces/elmresearchcenter/open_universal_arabic_asr_leaderboard),
NVIDIA's larger Arabic conformer scores 44.5–47.3 WER on SADA, Qwen3-ASR-0.6B
53.8 and Whisper-small 78.0, consistent with the order above.

Raw numbers, settings and every caveat: [benchmark record](benchmarks/arabic-asr-2026-11-05.json).
Reproduce with `scripts/benchmark_arabic_asr.py` (it never downloads).

### Limits of this evidence

- The Saudi clips are 90 seeded samples of TV speech from the SADA test split
  (60 Najdi, 15 Hijazi, 15 Khaliji). That is a small sample of one domain:
  television, not your microphone or room.
- **Arabic command accuracy with a real voice has not been measured.** The only
  command-shaped test audio was synthetic (Piper Arabic voices). Every model,
  including the Saudi-tuned one, understood just 2–8 of 60 such clips, which
  shows the synthetic voices are unintelligible, not that commands work or fail.
  Record your own commands and run the benchmark script on them to know.
- English words inside Arabic speech usually come out in Arabic script, and
  count as errors. Mixed Arabic–English speech is supported only to that extent.
- Timings ran on a desktop with the live service and a game running.

## How it runs

The upstream checkpoint is exported to three ONNX graphs by
`scripts/export_moonshine_arabic.py` (maintainer tool; needs torch). The export
is refused unless its output matches transformers' `generate()` on every test
clip. Jarvis runs the graphs with its own Rust/tract recognizer
(`native/moonshine-recognizer`), with no ONNX Runtime. On all 135 benchmark
clips the Rust recognizer wrote exactly the same text as transformers. It loads
in about 0.25 s and peaked at ~237 MiB in isolation (fp32 weights).

The model loads together with Whisper when the wake name is heard and is
released with it after a minute without a command, so waiting for the wake word
costs no extra memory.

For each command, the Arabic model transcribes first. Its text is accepted only
when it is one of the supported Arabic commands; otherwise the English path runs
exactly as before. English speech, which the Arabic model writes as Arabic-script
sounds, therefore cannot become an Arabic command.

## Arabic commands

The wording table is `src/jarvis/arabic_commands.json`, shared by the Rust and
Python parsers (`tests/fixtures/arabic_command_cases.json` keeps both in step).
Transcripts are folded first: no diacritics or tatweel, one alef, ة→ه, ى→ي, and
Arabic-Indic digits as Western digits. Then:

| Action | Examples |
| --- | --- |
| Open / switch / close apps | افتح فايرفوكس · شغّل ستيم · روح لفايرفوكس · سكّر ديسكورد · اقفل ستيم |
| Volume | علّ الصوت · وطّ الصوت · اكتم الصوت · رجّع الصوت |
| Workspaces | روح لمساحة العمل الثالثة · انقل الشباك لمساحة العمل ٢ |
| Fullscreen | ملء الشاشة · اطلع من ملء الشاشة |
| Recording and replay | ابدأ التسجيل · وقّف التسجيل · كلّبها · احفظ آخر ثلاثين ثانية · قص آخر دقيقتين |
| Dictation and control | اسمعني · خلاص · وقّف الاستماع |

Courtesy words (تكفى، لو سمحت، الله يخليك) and a leading “يا جارفس” are ignored.
App names can be said in English letters or in Arabic for common apps
(فايرفوكس، ديسكورد، ستيم…); for others, add an Arabic voice name in **Apps**.
The configured close words apply: with close words “quit”, سكّر فايرفوكس means
“quit firefox”.
