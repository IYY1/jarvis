# Persistent KWS, command-worker release and quiet endpoints

This follows the [initial quiet-gate and file-handoff experiment](kws-quiet-gate.md).
All work remains inside the separate Rust probe. Production Vosk/small.en,
controllers, settings and installed models are unchanged. There is no real
capture or action dispatch.

## What changed

The Rust parent can now run 1–32 bounded requests with the same KWS process
and loaded spotter. Each trigger gets a fresh existing `jarvis-voice --engine`
process. After a result, the parent closes and reaps that worker, releases
its audio buffers, waits one second without trimming, and samples the parent
and detector together. RSS/PSS include both remaining processes and the
measurement thread. Each file request creates a new KWS stream: this measures
persistent model ownership and repeated child loading, not uninterrupted
multi-command behavior inside one continuous detector stream.

The default is still one cycle and a file endpoint. The optional `quiet_endpoint`
has explicit RMS, minimum speech, quiet duration and command timeout. The
comparison uses RMS 0.005, 240 ms of post-trigger speech, 600 ms of quiet and
a five-second timeout. These parameters mirror the current worker's energy
level and minimum-speech/quiet durations, but applying them after an independent
KWS trigger is a new policy and requires its own recognition checks.

When quiet closes a command, the parent freezes the retained audio and sends
that bounded file to the unchanged command engine as soon as configuration
has been delivered. Input replay continues while the worker loads, decodes
and exits. Timeout aborts the window without submitting a truncated command;
buffer overflow explicitly fails. Further triggers in the same request are
ignored: the probe still has one command window per file.

The existing `--engine` adds its offline padding and replays wake+command
through the original wake decoder, command decoder, free decoder and matcher.
This is a measured file-replay endpoint prototype, not live streaming command
decoding or a replacement policy for the application.

## Repeated-cycle comparison

[Raw matrix](benchmarks/kws-handoff-cycles-2026-10-26.json). One fresh process
set per profile/arm runs three cycles, sequentially. The ungated and gated
detectors use identical input and the same quiet endpoint. Filesystem caches
are not evicted. The natural fixture closes at input time 3.570 s in every
arm; the last above-threshold input frame ends at 2.970 s.

| Profile | Ungated / gated peak combined RSS | Ungated / gated median result after last energy frame | Warm waiting RSS, all cycles |
| --- | ---: | ---: | ---: |
| Vosk/free | 233.66 / 231.94 MiB | 930 / 930 ms | 56.68–56.91 MiB |
| Vosk + small.en | 622.14 / 621.01 MiB | 1470 / 1440 ms | 57.09–57.71 MiB |
| Vosk + tiny.en | 329.53 / 333.89 MiB | 960 / 960 ms | 57.94–58.20 MiB |

All 18 natural commands and all 18 detector trigger counts match. Every cycle
returns to exactly two processes. Combined PSS peaks are 220.97–222.64 MiB
for Vosk/free, 609.94–611.20 MiB for small.en, and 318.51–322.95 MiB for tiny.en.
The three-cycle RSS high-water sums are at most 233.66, 622.14 and 333.89 MiB
respectively. Small.en still exceeds 400 MiB at this reduced worker-set scope.

Initial parent/detector RSS is 50.78–51.86 MiB; the first decoded cycle warms
additional pages. From the first released cycle to the third, RSS grows
0.191–0.199 MiB. These short trends cannot establish leak freedom or long-run
memory stability. The parent also retains a bounded set of JSON cycle reports.
The waiting figures exclude capture, controller, UI, replay and GTCRN, so they
do not pass the full application's 150 MiB waiting target.

Timing is observed by a loop receiving 30 ms input frames and a 20 ms memory
sampler. Three repetitions of one recording cannot establish precise latency
nonregression. These energy-frame timings also differ from the prior report's
source-file-end timings; do not compare those columns as if their origins were
identical. The file endpoint compatibility check still returns the expected
command with the default endpoint omitted.

## Deliberate failed endpoint gate and diagnosis

The RMS 0.005 matrix **exits 1**. The wake detector accepts the 10% amplitude
recording, but there is insufficient above-threshold command speech to satisfy
the new minimum. The window times out and misses the expected command.
The failed case remains in the complete matrix with `passed: false`.

The delayed-loader and 300 ms command-pause controls pass, command-without-wake
starts no worker, wake-without-command and long continuous speech time out,
and the undersized-buffer control explicitly exits 1 without a panic or a
truncated command submission. Every submitted slice is byte-identical to its
original input interval. Expected overflow is a passing failure-handling
control; the low-volume missed command is an acoustic failure.

[Focused diagnostic](benchmarks/kws-endpoint-controls-2026-10-26.json): the
same quiet recording passes through the file endpoint, isolating the miss to
the new endpoint policy rather than the command decoder. A separate endpoint
RMS 0.001 configuration then passes seven focused cases: low-volume command,
natural command, command pause, wake-only timeout, command without wake,
long-command timeout and the low-volume file control. This is a candidate
configuration, not a production default or a waiver of the earlier failure.

With RMS 0.001, the quiet command closes at input 3.450 s and returns at about
3.780 s; the natural command closes at 3.930 s and returns at about 4.290 s.
Soft speech and background noise make a single energy threshold unreliable;
this narrow fixture result does not calibrate an acoustic endpoint for actual
Jarvis names, speakers or noisy rooms.

The separate detector's **180 ms low-volume wake-trigger regression** also
remains unresolved. Changing the command endpoint does not repair it.

## Ten-cycle waiting-floor check

[Ten-cycle evidence](benchmarks/kws-handoff-floor-2026-10-26.json) uses the
RMS 0.001 endpoint, the same persistent detector, and ten fresh tiny.en command
workers. All ten commands and retained slices match, and every release leaves
two processes. Initial waiting RSS is 51.86 MiB; after the first decoded cycle
it is 57.37 MiB, reaching 57.79 MiB after cycle ten. First-to-last released-floor
growth is **0.418 MiB**, including 0.180 MiB across the last five samples.
Released PSS is 50.79–51.20 MiB. Combined command peaks are 333.67 MiB RSS and
322.52 MiB PSS.

There is a first-use increase and a smaller continuing trend. This does not
demonstrate a stable plateau, a leak, or leak freedom. Native state and the
parent's bounded report allocations have not been separated. The short run
does not replace memory-pressure testing or the required three 24-hour soaks.

## Subsequent continuous input and attribution

The [next isolated experiment](kws-continuous-diagnostics.md) now adds multiple
command windows inside one paced file request and attributes the short released
RSS trend to the parent. It also reproduces gate timing changes with cropped
ungated streams across three supplied recordings. Timing nonregression still
fails, and residual in-use allocation trends remain unresolved. The historical
failures and measurements above are preserved.

## Reproduction and limits

Use the cached toolchain and explicit local assets in
[the current handoff](memory-plan-handoff.md). The scripts reject existing
result paths:

```sh
python scripts/benchmark_kws_cycles.py --fixture "$PUBLIC_PCM" --model-dir "$MODEL" --library "$SHERPA_C_API" --keywords-dir "$WORK" --tiny "$TINY_MODEL" --work-dir "$WORK/cycles" --output "$NEW_CYCLE_RESULT"
python scripts/benchmark_kws_endpoint_controls.py --cycle-work-dir "$WORK/cycles" --cycle-result "$NEW_CYCLE_RESULT" --work-dir "$WORK/endpoint-controls" --output "$NEW_ENDPOINT_RESULT"
python scripts/benchmark_kws_floor.py --config "$WORK/endpoint-controls/natural-rms-0.001/config.json" --fixture "$WORK/cycles/tiny.en-gated-natural/input.raw" --work-dir "$WORK/floor" --output "$NEW_FLOOR_RESULT"
```

The first script's nonzero acoustic gate must be preserved. Later focused
candidate passes do not turn that earlier matrix into a pass. All examples
reuse the supplied recordings. Full acoustic acceptance, actual custom-name
positives, GTCRN/capture interactions, Windows, pressure, whole-application
budgets and the three sequential 24-hour soaks remain open.
