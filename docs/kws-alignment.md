# Model-aligned KWS stream experiment — 2026-10-27

The fixed aligned-onset candidate is **not accepted**. It removes later
triggers relative to the ungated baseline wherever both detect a wake, but
misses one wake caught by the original quiet gate. The larger phase grid also
finds 35 misses in the saved ungated baseline. Production Vosk/small.en and
experimental flags remain unchanged; neither KWS gate is integrated.

## Why test alignment

The previous [36-case diagnosis](kws-continuous-diagnostics.md) showed that
starting an ungated stream at the gate's cropped origin reproduces every gated
trigger chunk. That implicated stream context/start timing rather than delayed
timestamp delivery. It did not identify a complete timing fix.

The installed library reports sherpa-onnx 1.13.8. Its matching upstream tag's
[KWS implementation](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/sherpa-onnx/csrc/keyword-spotter-transducer-impl.h)
advances processed frames by the model's chunk shift. The
[Zipformer2 model interface](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/sherpa-onnx/csrc/online-zipformer2-transducer-model.h)
reads that shift from `decode_chunk_len`. The actual pinned encoder metadata
has `T=45`, `decode_chunk_len=32` and 10 ms feature frames: a 320 ms inference
shift, compared with our 30 ms input chunks. Arbitrary stream origins therefore
change the inference timing grid. This is the source-based rationale for one
alignment candidate, not evidence that a fresh stream has equivalent feature
or encoder state. Exact upstream build-byte equivalence is unverified. See
[source and metadata provenance](benchmarks/kws-alignment-provenance-2026-10-27.json).

The optional experimental `quiet_gate.align_chunk_ms=320` floors the requested
300 ms pre-roll origin to that grid in the original input timeline. It retains
at most 21 original 30 ms frames (630 ms), including recent active context so
immediate reopenings remain bounded. The first retained frame is sliced at the
exact origin. There is no synthetic silence, acoustic-threshold search or
model change. Labels are emitted at the current original input availability
time, never backdated to when buffered audio was recorded.

Omitted alignment preserves the original gate. Validation requires the
alignment to be 10–1000 ms in 10 ms steps, with pre-roll plus alignment no
longer than hangover. Accounting separates unique accepted input from replayed
overlapping quiet context, so decoded plus skipped equals original input;
accepted equals decoded plus replayed. The ring is transient per request.

## Fixed protocol and results

Before measurement, `build/memory-plan-kws/alignment/protocol.json` fixed RMS
0.001, pre-roll 300 ms, hangover 1200 ms, alignment 320 ms and threshold 0.25.
Three supplied recordings each use gains 0.1/0.3/1.0 and leading quiet
600–1530 ms in 30 ms steps. Those 32 steps span the complete relative
320/30 ms phase cycle. Reuse adds timing coverage, not speakers or independent
acoustic evidence. The expected label is one source keyword per case, no extras.

[Raw evidence](benchmarks/kws-alignment-v2-2026-10-27.json) retains every run,
input hash, config, hit and miss; the runner exits **1** for failed acceptance.
The first attempt's [incomplete artifact](benchmarks/kws-alignment-2026-10-27.json)
is preserved: its analysis expected accounting fields absent from the older
ungated binary. The restarted driver accepts that older protocol and still
checks counters in versions that report them. No runtime change was required.

| Phase arm | Intended wakes detected | Extra triggers | Later than ungated, among paired detected wakes |
| --- | ---: | ---: | ---: |
| Saved ungated baseline | 253/288 | 0 | Reference |
| Saved original quiet gate | 288/288 | 0 | 122/253, up to 300 ms |
| Model-aligned candidate | 287/288 | 0 | 0/253; 252 exact, one 300 ms earlier |

The candidate gains 34 wakes missed by ungated and loses none that ungated
detects in this grid. Both miss `go-forward-0.1-1110`; the original quiet gate
detects that wake at input 2.76 s. Its stream starts at 1.32 s, compared with
the aligned candidate's 1.28 s. Thus alignment is a useful timing control but
fails the intended-label gate and regresses one detection against the prior
gate. Do not hide the miss behind the improved aggregate or treat unmatched
cases as successful timing comparisons. The raw runner's strict label-identity
timing gate is also false, including cases where alignment gains a wake.

A separate [cropped-context diagnostic](benchmarks/kws-alignment-context-2026-10-27.json)
feeds the whole failing input and exact crops at 1.28 s and 1.32 s to both
saved and current ungated binaries. Both miss the whole input and 1.28 s crop,
and detect the 1.32 s crop. The intended label remains positive in every row;
their recognition summaries remain failed. Diagnostic exit 0 means the
predeclared context prediction reproduced, not that recognition passed. This
reproduces the miss without gate execution or its sample-accounting path.
It supports stream-start context sensitivity, without attributing the precise
effect to an individual feature/encoder/decoder component.

Nineteen additional focused controls pass in each of five arms: saved ungated,
saved original gate, current ungated, current omitted-alignment gate and aligned.
These include long leading quiet, soft onset, pauses, repeated quiet recovery,
continuous reversed speech and absent-name negatives. The candidate detects
12 expected wakes, with exact baseline trigger chunks and no extras; both
unchanged-behavior arms match their saved controls exactly. Jarvis-name
positives and representative confusable/music/game/dialogue negatives are
still missing.

Three fresh sequential workers per arm each process 30 s paced silence and
the low-volume control. Silence CPU is 0.0147–0.0177% of one core for the old
gate and 0.0148–0.0168% for alignment; all pass the 0.1% target. Aligned peak
RSS is 53.90–54.48 MiB and final PSS 49.57–50.09 MiB, including native libraries
and the ring. These process variations do not establish an RSS saving over the
old gate. The low-volume control triggers at 2.07 s in all aligned runs versus
2.25 s for the old gate, restoring its saved ungated input trigger time.

[Derived counts and resource results](benchmarks/kws-alignment-analysis-2026-10-27.json)
distinguish detected pairs from label mismatches. They recheck 971 input-hash
uses and 313 aligned accounting reports. [Code/package validation](benchmarks/kws-alignment-validation-2026-10-27.json)
records 18 passing Rust tests per normal/diagnostic build (overlapping counts),
ten passing explicit Python tests with no skips, formatting, all-target/
all-feature Clippy with warnings denied, Python compilation, package and diff
checks. New tests exercise bounded origins across input phases, overlapped
quiet accounting, invalid settings and a real native stream reopening after a
quiet boundary with partial final PCM. The source and both normal binaries
are preserved in `build/memory-plan-kws/alignment-candidate/snapshot.json`.

The scope remains the isolated detector. No new parent/pressure matrix or
command-worker architecture was introduced for this change; the detector
fails recognition acceptance before any adoption decision. The parent's
normal executable is byte-identical to the previous owned-reader binary.

## Decision and next work

Keep this candidate explicit and experimental. Preserve the original failed
timing measurements, this missed wake and the expanded ungated failures.
Do not tune pre-roll, gain or thresholds to the failing public recording.
Any next context-preserving design needs a separately declared protocol and
matched recognition, original-input timing, silence CPU and memory controls.
Chunk-grid alignment alone does not establish equivalent feature/encoder state.

The surrounding [memory plan](memory-plan.md#current-position--2026-10-27)
still needs real acoustic evidence, a validated command model, strict Linux
replay byte bounds, whole-application/capture/GTCRN measurements, Windows and
broader pressure tests. Three sequential 24-hour soaks remain required after a
candidate is ready. All whole-application, full acoustic, Windows and release
gates remain false; short fixture runs cannot replace those requirements.

## Reproduce with existing assets

Use the cached toolchain/model/library paths from [the handoff](memory-plan-handoff.md).
The source snapshots pin measured binaries; keep all future work/output paths
new. The predeclared protocol is hashed in provenance and the raw result.

```sh
.venv/bin/python scripts/benchmark_kws_alignment.py --config build/memory-plan-kws/cycles/tiny.en-ungated-natural/detector.json --fixture build/refactor-review/goforward.raw --model-dir build/memory-plan-kws/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01 --baseline-dir build/memory-plan-kws/baseline --protocol build/memory-plan-kws/alignment/protocol.json --work-dir "$NEW_ALIGNMENT_WORK" --output "$NEW_ALIGNMENT_RESULT"
.venv/bin/python scripts/benchmark_kws_alignment_context.py --matrix docs/benchmarks/kws-alignment-v2-2026-10-27.json --baseline-dir build/memory-plan-kws/baseline --work-dir "$NEW_CONTEXT_WORK" --output "$NEW_CONTEXT_RESULT"
```

The first command retains exit 1 for the quality failure. The second diagnoses
the one saved failing case and retains intended positive labels even where a
miss is reproduced. Neither command authorizes capture, actions or integration.
