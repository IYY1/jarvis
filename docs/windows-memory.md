# Windows memory and responsiveness (0.3.0)

0.3.0 replaces the Windows Python voice worker with the Rust worker that Linux
already used. This page records the baseline, what changed, how it was
measured and the before/after results on Windows 10.

## Where the memory was

Earlier native Windows measurements ([component validation](memory-plan-windows-validation.md))
had already isolated the largest cost: the Python voice worker kept Vosk,
ONNX Runtime/sherpa-onnx Whisper small.en and GTCRN resident for as long as the
microphone was on. It measured 549 MiB private working set while waiting for
the wake word and about 704 MiB during a command, against 130 MiB for a
Vosk-only control. The service, tray and settings are small by comparison, and
the replay buffer is an explicit user choice with its own cap.

On Linux the Rust worker already loads Whisper only after Vosk hears a wake
name and frees it after 60 seconds without a command. The riskiest assumption
for Windows, that tract's kernels and the worker cross-compile for
`x86_64-pc-windows-gnu` and run there, was checked first with a Wine smoke
test, then on Windows 10.

## What changed

- `native/voice-worker/src/input.rs` separates capture from the worker loop.
  Windows captures with shared-mode WASAPI and lets Windows convert the device
  mix to 16 kHz mono s16 (`AUTOCONVERTPCM`), so no resampler or Python audio
  stack is involved. Control lines are read on a thread; queued audio is
  bounded to about two seconds.
- The service starts `tools\jarvis-voice.exe` with `tools\libvosk.dll`;
  vosk/PocketSphinx/sherpa-onnx/ONNX Runtime are no longer frozen into the
  Windows bundle, and the Python worker's Windows code path is removed.
- An unrelated bug found by the benchmark: the service could not stop while a
  status subscriber (Settings, Waybar, the benchmark itself) was connected.

## Method

`scripts/benchmark_windows_app.py` runs on Windows against an extracted
portable build. It starts the service (`JarvisConsole.exe --internal daemon`)
with an isolated config and endpoint, consent pre-granted and
`MURMUR_DRY_RUN=1`, so commands are parsed but never dispatched.
`MURMUR_TEST_CAPTURE_FILE` replaces the microphone with a generated timeline:
40 s of silence, then ten repetitions of the public “go forward ten meters”
fixture (SHA-256 `f15c60ec…0e71`) each followed by 12 s of silence, then 300 s
of silence. “go forward” is the wake phrase and “ten meters” a command shortcut.
The 0.2.0-code baseline needs the same input hook in its Python worker; that
measurement-only change is [windows-baseline-capture.patch](benchmarks/windows-baseline-capture.patch)
and is not part of any release.

Every 0.25 s the harness sums the **service process tree** (service, voice
worker and any helper) with `murmur.process_memory`:

- **Private working set**: resident pages that cannot be shared, so DLL code
  mapped by other processes is not counted. This is the primary RAM metric.
- **Private commit**: committed private memory, including pages Windows has
  trimmed or paged out. It shows memory that a working-set number could hide.

The settings window is a separate process and is measured separately for
15 seconds after it opens. Windows reserves shared memory such as system DLLs
once for all processes; it is excluded from both metrics. No container or VM
overhead is part of Jarvis: the Windows guest itself is the test machine.

Windows: 10 Enterprise Evaluation 22H2 (build 19045), KVM guest, 4 vCPUs
of an AMD Ryzen 7 7800X3D, 8 GiB RAM, no GPU acceleration, silent virtual audio
device. Baseline: commit `2442ff4` (0.2.0 code) built with the same pipeline.
Release: 0.3.0. Runs alternate baseline/release, three each.

## Results

Medians of three runs per build; raw samples, events and per-run values are in
[benchmarks/windows-app-2026-11-01/](benchmarks/windows-app-2026-11-01/)
(`summary.json` is the table below). Service tree = service + voice worker
+ their hidden console hosts.

| Service tree | 0.2.0 code | 0.3.0 | Change |
| --- | --- | --- | --- |
| Settled idle, private working set | 599.0 MiB | 165.2 MiB | −433.8 MiB (−72%) |
| Settled idle, private commit | 1003.6 MiB | 565.5 MiB | −438.1 MiB (−44%) |
| During the 10 commands, median private WS | 619.3 MiB | 487.3 MiB | −132.0 MiB (−21%) |
| Peak private working set | 776.5 MiB | 532.8 MiB | −243.7 MiB (−31%) |
| Peak private commit | 1188.6 MiB | 935.0 MiB | −253.5 MiB (−21%) |
| First 60 s idle after the commands | 620.6 MiB | 488.0 MiB | −132.7 MiB (−21%) |
| After 5 min idle, private working set | 620.6 MiB | 175.7 MiB | −444.9 MiB (−72%) |
| After 5 min idle, private commit | 1025.4 MiB | 592.6 MiB | −432.8 MiB (−42%) |
| Settings window (separate process) | 19.1 MiB | 18.9 MiB | unchanged |
| Cold start to service answering | 0.50 s | 0.64 s | +0.15 s |
| Cold start to listening (models loaded) | 2.35 s | 1.22 s | −1.13 s (−48%) |
| Command latency after speech ends, median | 0.53 s | 0.86 s | **+0.33 s (+62%)** |
| Command latency, max (first command) | 1.17 s | 1.52 s | +0.35 s |
| Commands recognized and dispatched | 10 / 10 | 10 / 10 | — |

Per process at settled idle: service 21.3–21.7 MiB in both builds; voice worker
566–567 MiB (Python) versus 132.4 MiB (Rust); two hidden console hosts of
5.7 MiB each in both. Run-to-run spread was under 1 MiB at settled idle and
under 7 MiB at peak. Private commit sits about 400 MiB above the working set in
**both** builds, so it is not new; the commit reduction matches the working-set
reduction. Which process holds that committed-but-not-resident memory was not
attributed (both builds share the service and the Vosk runtime).

What the numbers mean in use: while you are not talking to Jarvis, which is most
of the time, the whole app holds about 165–176 MiB instead of about 600 MiB.
During a burst of commands Whisper is resident (about 320 MiB) and the tree
reaches about 490–530 MiB, still 21–31% below 0.2.0. Whisper is released after
60 s without a command; the 5-minute figure includes the slight growth after
ten load/use cycles also seen on Linux ([on-demand Whisper](whisper-on-demand.md)).

Outcome: a large reduction at idle and a smaller one at peak, no lost commands
(10/10 in every run), faster time to listening, and slower per-command
transcription (next section).

## Latency regression

Each command takes about 0.33 s longer to transcribe than with the old ONNX
Runtime worker. The first command after a wake also waits for Whisper to load
(about 1.5 s total instead of 1.2 s). Investigation so far, timing the same
fixture on the same CPU:

| Transcription, 1 thread | Linux build | Windows build |
| --- | --- | --- |
| tract AVX-512 kernels (default) | 0.40 s | 0.79 s |
| AVX-512 disabled (`TRACT_CPU_ISA=-avx512f`) | 0.70 s | 1.13 s |
| AVX-512 and FMA disabled | 1.70 s | 4.70 s |

The Windows build selects the same kernels but every path, including tract's
generic Rust code, runs 1.6–2.8× slower. It is not the allocator (mimalloc: −4%),
the cache-size probe (`TRACT_LLC_BYTES` override: no change) or thread wake-up
cost (one thread is slower by the same factor). Remaining suspects are
denormal handling and page-fault costs of large per-inference buffers on Windows;
profiling a Windows build (ideally also an MSVC-target build) is the next step.
Two threads were also slower than one for this short command on both
platforms; thread count was left unchanged because a 4-vCPU VM is not
representative of the target hardware.

tract detects cache sizes on Windows by running `wmic`. The worker inherits the
service's hidden console, so nothing appears on screen, but `wmic` is an optional
feature on Windows 11 24H2 and later; without it tract falls back to default
cache sizes.

## Tradeoffs and limits

- Measured in a VM with injected audio, not on physical hardware with a real
  microphone; CPU timings in particular need confirmation on typical laptops.
- The baseline is the 0.2.0 code at `2442ff4` built with the same pipeline,
  plus only the capture hook. The shipped 0.2.0 installer was used for the
  upgrade test, not for these numbers.
- Recording and replay were not exercised; their memory is unchanged by this
  release and is governed by the replay budget (see [Windows release](windows.md)).
- The benchmarked 0.3.0 build predates removal of the unused WMI module from the
  bundle; nothing imports it, so it cannot affect these measurements. The final
  installer was revalidated separately (see [Windows release](windows.md)).

## Reproduce

```powershell
python scripts\benchmark_windows_app.py <Jarvis folder> --fixture goforward.raw --label release-1 --output release-1.json
```

```sh
python3 scripts/summarize_windows_benchmark.py docs/benchmarks/windows-app-2026-11-01/*-?.json
```
