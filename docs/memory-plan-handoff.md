# Memory-plan handoff — 2026-10-27

Continue the existing [memory plan](memory-plan.md) to make Jarvis comfortable
on an ordinary 8 GB Windows laptop. The user prefers Rust for substantial
runtime work. Measurements and recognition quality are hard gates. **The full
plan is not complete, and no Windows or whole-application release gate passes.**

## Checkout and working rules

- Workspace: `/home/silver/Documents/ChatGPT/New project`; canonical directory:
  `/home/silver/Projects/jarvis`. These refer to the same checkout.
- All four memory-plan commits through `30cfab6` were pushed to GitHub branch
  `codex/brightness-wake-controls` at the user's request on 2026-10-27. Inspect
  `git log` and `git status` for newer work and preserve any changes. Current
  execution permits filesystem/network writes without an approval prompt.
  No PR or new chat was created. Earlier authorized live replay validation
  deployed tested controller fixes and restarted the service.
- Use the canonical workspace as the command working directory and a non-login
  shell (`login:false`) so shell startup does not change directories.
- No `AGENTS.md` was found. A later REA validation subagent added replay-save
  regression tests. No recurring jobs were created.
- Use public supplied audio, fake capture and generated video for experiments.
  Do not start a real microphone, desktop capture or installed service merely
  to run a benchmark. Runtime models remain installed as before; experimental
  downloads and diagnostic dependencies stay under ignored `build/` paths.
- The current Vosk/small.en path remains the default. Internal flags
  `native.vosk_fast_path` and `native.vosk_finalize_utterances` default false
  and are omitted by normal controllers. Smaller Whisper profiles require
  explicit experimental files. The separate KWS probe is not integrated.
- Earlier user authorization on 2026-10-27 enabled the implemented disk
  replay mode in the live Linux installation: 120 seconds, existing HDR/audio,
  2048 MiB supervised disk stop threshold. The initial setting change did not
  restart the controller; saving settings reloaded its voice worker. A 125-second live observation
  kept replay on, verified private growing disk files and measured recorder
  RSS about 150 → 115 MiB on this desktop. Total memory also changed with the
  voice reload and is incomplete, so no combined-budget gate passes. See
  [local activation and scope](local-memory-activation.md). Recognition settings
  and experimental defaults remain unchanged. A private full configuration
  backup is under ignored `build/memory-plan-live/2026-10-27/`. Later authorized
  save/decode validation found clips were stored inside the disposable buffer,
  and omitted IPC duration saved all retained backing-file history. Both are
  fixed with regressions, tested controller deployments and preserved failed
  evidence. See [live replay validation](replay-live-validation.md) for the
  final live result and its limits. Clips remain local in the user's Clips folder.

## Current priorities — user direction on 2026-10-27

**Windows work is set aside.** The VM is stopped; preserve its evidence and
private state. **Further optional disk-replay optimization is deferred** because
the user raised continuous SSD write costs. Do not resume strict-quota work
as the next action. At the user's explicit request, local replay was restored
to RAM through the configuration API, changing only `replay.storage`. The
controller remained running; 120 seconds, HDR and both audio sources were
preserved. Replay is on and no disk sessions remain. See the
[current local record](local-memory-activation.md). Do not turn disk replay
back on without a new request.

Focus on everyday voice waiting memory, command peaks, wake reliability and
latency. Keep production Vosk/small.en and failed candidate gates unchanged.
The [fixed PocketSphinx screen](pocketsphinx-wake-screen.md) is now complete:
it detects the original Jarvis tail, but expanded quiet/interrupted controls
fail (45/63 cases, 24/42 intended wakes). Peak RSS is at most 53.22 MiB and
isolated paced silence CPU 0.0557–0.0663% of one core. Do not port/integrate
this failed configuration or tune it to the selected recording. No matched
Vosk comparison or end-to-end latency acceptance is established.

The [subsequent wake screens](wake-candidate-screening.md) are complete.
Standalone rolling tiny misses all 18 user-tail variants, with eight same-input
Rust/reference transcripts identical across tiny/small controls. ESPHome's
pinned v2 Hey Jarvis model passes its three public positives but misses the
selected bare name at stock cutoff 0.97. A separate PocketSphinx energy gate
(RMS 0.001, 300 ms pre-roll, 1200 ms hangover, unchanged 1e-20 threshold)
recovers all 18 original VAD-screen failures; 60/60 common case repetitions
pass. It still misses two newly acquired public Hey Jarvis examples; stock
native Vosk also misses those two. All four drivers retain exit 1. No candidate
is adopted, and neither arbitrary custom-name coverage nor full quality passes.

The [fixed combined detector](dual-wake-screen.md) is now rejected. Before
inference, 32 fresh Jarvis recordings and 20 other-keyword negatives were
selected from the pinned public Picovoice benchmark by Git blob hash order.
The combination detects 24/32 positives, native Vosk 31/32; there are eight
paired regressions and one gain, with no extra wakes on the 20 negatives.
All 54 familiar controls pass, including repeated shared-reset sequences.
Peak combined worker RSS is 71.86 MiB, excluding the application. The driver
completes with exit 1. Do not port/integrate or tune this failed candidate to
these newly observed files. The other 352 public Jarvis recordings remain
undownloaded/undecoded; the user's reserved private recordings remain untouched.

**Pending user clarification:** keep the 150 MiB waiting target and continue
wake-engine research, or revise it to 200 MiB while retaining Vosk and focusing
on command memory. Do not treat the recommended/default UI option as an answer;
keep numerical targets unchanged until the user replies. Independent command
quality, retained-memory and instrumentation work can continue. Raw/private
artifacts and drivers are under ignored `build/memory-plan-corpus/`; pinned
model assets and diagnostic wheels are in `build/memory-plan-mww/`.

## Latest evidence and next implementation step

The user subsequently supplied a fresh command recording and authorized an
8 GB Windows VM. The [selected final-five-second test](memory-plan-real-audio.md)
now gives one real Jarvis command example: production small.en and explicit
tiny.en both recognize the command in paced playback; all three KWS arms miss
the wake at the unchanged threshold, including a trailing-silence control.
The user subsequently confirmed the intended wording “Jarvis, clip that”;
see [the label record](benchmarks/user-tail-label-2026-10-27.json). Older group
recordings remain unlabeled, and the reserved recordings remain held out. Private recordings,
PCM and transcripts stay under ignored `build/memory-plan-corpus/`.

The [native Windows report](memory-plan-windows-validation.md) records 51
passing component tests and 12 passing updated accounting tests (overlapping
scopes). A bounded QueryWorkingSet fallback makes private working set available
on the older Windows build and agrees closely with independent PDH accounting.
Three generated-video pairs use about 249 MiB private working set in RAM mode
and 21 MiB in disk mode, excluding capture, voice, controller and settings.
Full Linux Python validation passes 341 tests with 40 skips; CodeRabbit raises
zero issues for the accounting diff. These are component results, not release
acceptance.

The Microsoft-checksum-verified Windows 10 VM now has a normally activated
90-day evaluation, four CPUs and 8192 MiB RAM. Its network is restricted again
after activation. It has no user recordings or host microphone connection.
Ignored `build/memory-plan-windows-vm/` contains its private disk, credentials,
test transport, job/results journal and bootstrap. Never print or commit the
private state or seed files. `run_vm.py` boots the existing disk;
`qmp_control.py` controls only this VM. The startup entry exists only inside
the VM. Job `011-native-voice-baseline` has completed: the production Windows
small.en/GTCRN engine reaches 549.28 MiB private working set while waiting and
703.92 MiB during the public command fixture, already exceeding the combined
budgets. The explicit Vosk-only control reaches 130.25 / 144.04 MiB, with much
higher commit recorded separately; it disables accurate recognition and is not
a proposed default. See the report and saved samples before starting another
run. The selected final architecture, combined workloads,
strict Linux disk cap, pressure and full soaks remain open.

The [full-plan code review](memory-plan-code-review.md) follows the committed
live replay fixes. It tightens soak sample coverage, bounds disk-helper I/O,
reports retained Windows save failures and corrects related tests. Full Python
and six-crate Rust validation passed; this does not clear the release gates.

Read [the model-alignment experiment](kws-alignment.md), then the earlier
[reader/cache](kws-reader-caches.md), [pressure](kws-pressure-allocations.md) and
[continuous timing](kws-continuous-diagnostics.md) reports. Do not repeat old
matrices without a new change or unresolved question.

1. A single optional Rust `align_chunk_ms=320` candidate derives its grid from
   the pinned encoder's native 32 × 10 ms chunk shift. RMS 0.001, pre-roll
   300 ms and hangover 1200 ms remain fixed. A bounded 21-frame original-audio
   ring floors stream starts to that grid, slices its first frame exactly and
   reports unique versus replayed context. Omitted alignment preserves the
   previous gate. This changes only the isolated detector.
2. A predeclared 288-case phase grid spans three supplied recordings, three
   gains and 32 leading-quiet offsets. Saved ungated detects 253/288, original
   gate 288/288 and aligned 287/288; no extras. Among 253 jointly detected
   baseline/candidate wakes, alignment is exact in 252 and 300 ms earlier in
   one. The original gate is later in 122/253, up to 300 ms. These perturbed
   public files add timing coverage, not independent speakers.
3. Alignment gains 34 ungated misses and loses no ungated successes, but loses
   one wake caught by the original gate: `go-forward-0.1-1110`. Thus its
   intended-label gate fails; the matrix exits 1. A separate whole/1.28 s/
   1.32 s cropped diagnostic reproduces miss/miss/hit in both saved/current
   ungated binaries. The intended labels stay positive and their recognition
   summaries stay failed. Stream-start context sensitivity is reproduced
   without gate execution; the precise native component is not attributed.
4. Nineteen focused controls pass in all five arms, with 12 wakes and exact
   aligned baseline trigger chunks. Both current unchanged-behavior controls
   match saved ungated/original-gate chunks. Three fresh paced workers per arm
   give aligned silence CPU 0.0148–0.0168% of one core and peak RSS
   53.90–54.48 MiB. The original low-volume control returns to 2.07 s from
   the old gate's 2.25 s, but that improvement does not excuse the new miss.
5. The first driver attempt's incomplete evidence is preserved; it assumed
   accounting fields absent from the old ungated protocol. A new-path restart
   corrects that analysis compatibility and completes every fixed case.
   Validation passes 18 Rust tests per normal/diagnostic variant (overlapping),
   ten explicit Python tests and format/Clippy/package/compilation/diff checks.
   Runtime source and binaries are preserved under
   `build/memory-plan-kws/alignment-candidate/snapshot.json`. All earlier
   snapshots stay intact; the parent binary remains byte-identical.
6. Prior reader/cache and private cgroup findings remain valid at their stated
   scope. Joining bounds reader lifetime but does not remove the glibc trend;
   diagnostic malloc-cache disabling removes short in-use growth, with residual
   RSS warming and no production tunable adopted. OOM raw exits 0/1/1 and
   recognition failures remain explicit. No parent/pressure matrix is repeated
   solely for a detector candidate already failing recognition acceptance.

**Next:** design a context-preserving detector path or another wake candidate
with a declared protocol, then measure matched intended labels, input trigger
times, silence CPU and memory. Chunk alignment alone is insufficient; do not
tune pre-roll/gain/thresholds to the failed file. Keep the new miss, expanded
ungated misses, prior timing failures, RMS 0.005 endpoint failure and small.en
memory failure visible. Acquire the missing acoustic/platform evidence before
choosing and integrating an architecture. Longer default-policy trends may
justify native allocation stacks; no leak-freedom/plateau claim exists.

The [current six-step status](memory-plan.md#current-position--2026-10-27)
separates implemented foundations from acceptance. Significant work remains:
full corpus, regression resolution and candidate choice, voice integration,
GTCRN/real capture, combined Linux workloads and pressure, and three sequential
24-hour soaks. Strict Linux replay byte bounds and Windows/8 GB acceptance
remain unpassed and deferred by the user. Final soaks alone require at
least 72 hours after candidate readiness; there is no honest overall percentage
or ETA while these gates remain open.

The first selected custom-name command and native Windows component results
above do not complete the required corpus, confusables, speakers/accents,
music/game/dialogue negatives, GTCRN/capture interaction, hardware, pressure,
combined budgets or full-duration soak. Repeated fixtures do not add speakers
or establish a general false-alarm rate. Do not integrate or promote either
probe on the strength of these results.

## Work completed so far

| Area | Implemented and measured | Limit or adoption state |
| --- | --- | --- |
| Process accounting | Linux RSS/PSS and Windows private working set/commit on explicit status/doctor; application sampler, recognition gate and soak runner | Isolated Linux whole-app baseline 176.84 MiB RSS exceeds the 150 MiB replay-off target; Windows and complete combined workloads remain open |
| Missed wake | Rust/Python wake decoder reset when background hangover exhausts; repeated background/quiet/wake fixture checks pass | Full real acoustic corpus still required |
| Disk replay | Rust bounded private replay store, opt-in disk mode, Windows RAM cap, Linux/Windows adapters and packaging; synthetic retained-video buffer layer about 266 → 27.5 MiB RSS | Excludes real capture/voice/controller/UI; Linux recorder disk monitor can overshoot its quota between polls; strict Linux cap remains open. 2026-10-27: Linux RSS stop removed (256 MiB default was below the live recorder's RSS); Linux disk budgets below 512 MiB refused (GSR's 256 MiB files) |
| Vosk history attribution | Separate wake/command/free probes with all three decoders resident; finalizing before reset reduces retained history | Does not explain the entire prior live RSS floor or prove a leak |
| Vosk fast path | Exact, complete-confidence grammar/free agreement can skip Whisper | Default false: forced cold fallback p95 regressed to about 2696 ms despite much lower exact-command peaks |
| Vosk finalization | Default-disabled real Engine experiment at utterance boundaries; 32-cycle floor growth about 45 → 2.7 MiB; 384/384 probe commands | Full corpus, platform and soak gates open; command peaks still over 500 MiB with small.en |
| Smaller Rust Whisper | Strict tiny/base/small profiles, generic caches and graph validation; small's 55 saved tensors remain byte-identical | Small remains default. Tiny: 63/63 reference transcripts, 9/9 paced commands, 279–282 MiB worker peak. Base: 45/63 parity, unresolved noisy/truncated encoder numerical differences; not validated |
| Configurable KWS | Rust replay/ownership/pacing, pinned sherpa-onnx C ABI and optional bounded quiet gate; no Python in inference | Optional gate: 53.57–54.59 MiB RSS, 33/33 focused wakes, 0.0132–0.0150% silence CPU; low-volume trigger delayed 180 ms; no integration |
| Model-aligned KWS | Optional bounded original-audio alignment, 288 fixed phase cases, five-arm focused controls and whole/cropped miss reproduction | 287/288 wakes versus original gate's 288/288; matched ungated triggers no later, silence CPU passes; missed wake blocks acceptance |
| Buffered handoff | Separate Rust parent retains audio through detector/command-worker overlap; existing Vosk/free/matcher policy preserved; 18/18 fixture cases, byte-identical buffers | Explicit file endpoint only; small.en combined worker peak 594–617 MiB fails target; tiny/Vosk alternatives do not establish full acoustic, application or Windows acceptance |
| Reader/cache attribution | Owned cancellable readers, full-queue/partial-line tests, 384 detector requests and four matched eight-cycle cache/lifetime arms | Malloc cache controls remove short in-use growth but RSS warms; no tunables adopted, no full plateau/leak freedom or release sign-off |
| Allocation and pressure recovery | Opt-in Rust allocator accounting, 128-request/32-cycle attribution and private cgroup OOM recovery, with raw failures retained | Later cache controls explain short in-use growth; native source stacks, arbitrary/app-wide/Windows pressure and full soak open |
| Continuous input and attribution | Multiple windows in one file, bounded busy/timeout/recovery, 36 exact cropped timing reproductions and per-process/report-retention floor controls | Timing remains failed; residual in-use trends, acoustic, pressure, application, Windows and soak gates open |
| Repeated handoff/endpoint | Resident detector, fresh command-worker teardown, matched quiet endpoints, byte-identical slices, timeout/overflow checks and ten-cycle floor evidence | RMS 0.005 low-volume command fails; RMS 0.001 candidate passes seven controls; ten-cycle floor grows 0.418 MiB; later continuous controls do not establish full soak or application acceptance |

Detailed evidence: [measurement gates](memory-measurement.md),
[recording/storage](recording.md), [history attribution](vosk-decoder-history.md),
[fast path](vosk-fast-path.md), [finalization](vosk-finalization.md),
[Whisper profiles](whisper-model-experiments.md), [KWS](kws-experiment.md),
[quiet gate and handoff](kws-quiet-gate.md), [cycles and endpoints](kws-handoff-cycles.md).
Raw artifacts are under `docs/benchmarks/`.

The combined targets are replay-off waiting ≤150 MiB, replay-on waiting
≤300 MiB and commands ≤400 MiB for **all** Jarvis processes, with no recognition
regression. The soak requirement is three sequential full 24-hour runs; no
such campaign has completed. Do not infer these gates from worker-only RSS.

## Latest KWS result and deliberate failures

- Model-derived alignment: original gate detects 288/288 phase wakes; aligned
  detects 287/288 and ungated 253/288. No extras. All 253 jointly detected
  ungated/aligned wakes are no later, but one wake caught by the original gate
  is lost. Both ungated binaries reproduce miss/miss/hit for whole input and
  crops at 1.28/1.32 s. The fixed matrix exits 1; the diagnostic's exit 0 is
  context reproduction only. No recognition or architecture sign-off follows.
- Current omitted-alignment and ungated controls match 19 saved focused cases
  exactly; aligned also passes them, including the formerly delayed low-volume
  wake. Three paced repetitions retain low silence CPU. The failing expanded
  phase grid takes precedence over that narrow focused success.

- Reader/cache follow-up: all 384 detector wakes/trigger chunks and 32 parent
  commands/buffers match across controlled arms. Joining does not remove the
  glibc in-use trend; disabling the malloc tcache removes that short trend
  in both processes, while stack-cache disabling does not. Residual RSS warms
  and all full-duration/native-origin/release claims remain unproven.
- Teardown regression controls: 8/8 continuous and 3/3 private cgroup controls
  pass with exact join counts and byte-identical source slices. Raw OOM failures
  and the corrected successful/aborted/fault summaries remain visible.

- Allocation ownership: 256 detector requests match exact trigger chunks across
  normal/counter arms; 32/32 parent commands and buffers pass. Requested Rust
  live bytes/blocks show no accumulating-object trend; glibc in-use still rises.
- Private cgroup pressure: 3/3 lifecycle controls, kernel OOM kills 0/1/2 and
  raw exits 0/1/1; three submitted slices match. Both OOM recognition gates
  remain false. No arbitrary-victim/full-app/Windows pressure sign-off.

- Continuous-file extension: 8/8 lifecycle controls, seven expected commands
  across 11 accepted windows and 7/7 byte-identical submitted slices. The
  three-window tiny.en control peaks at 335.93 MiB for the reduced worker set.
- Timing diagnosis: labels match in 36/36 fixed variants across three supplied
  recordings, but 18 have later triggers (up to 270 ms); exit 1 is preserved.
  Cropped ungated controls reproduce 36/36 exact gated trigger chunks.
- Floor attribution: 16/16 handoff commands and eight detector-only requests
  pass. All released RSS growth is in the parent in these controls; residual
  parent/detector in-use heap increases remain unresolved.

- Quiet-gate follow-up at 0.25: 40/40 focused cases and 33/33 wakes in each
  baseline/gated arm; the paced subset is 21/21 wakes per arm. No extra
  triggers. Current ungated code matches saved-baseline trigger chunks exactly.
  Standby CPU passes, but low-volume trigger-time nonregression fails (+180 ms).
- Buffered handoff: 18/18 cases, 15/15 byte-identical triggered buffers.
  Combined small.en RSS fails 400 MiB; every whole-application gate stays false.
- Original ungated primary threshold 0.25: 51/51 focused paced/unpaced cases, 39/39 expected
  wakes, no extra triggers. The paced subset is three fresh workers, 15 wakes.
- All 147 unpaced cases match upstream Python API labels and exact input
  trigger chunks, **including upstream's misses**.
- At threshold 0.5, 9/24 intended wakes are missed; at 0.75, 12/24 are missed.
  The complete matrix correctly exits **1**. This is not an unexplained code
  test failure and must not be normalized into a pass.
- Native C API/ONNX Runtime inference is not a pure Rust neural-network
  implementation. Rust owns configuration, streaming, pacing and resource
  lifetimes; the small C shim uses the unmodified pinned header for ABI layout.
- Jarvis and Hey Jarvis tokenize successfully, but have only absent-keyword
  negative controls. **There are no positive recordings of those names.**
- The official archive has no publisher digest. Provenance records a local
  SHA-256 content pin, not a publisher-verified checksum.

## Build, models and diagnostic environment

Run from the workspace with the existing cached toolchain:

```sh
export PATH="$PWD/build/refactor-review/rustup/toolchains/1.99.0-x86_64-unknown-linux-gnu/bin:$PATH"
export CARGO_HOME="$PWD/build/refactor-review/cargo"
export RUSTUP_HOME="$PWD/build/refactor-review/rustup"
cargo test --offline --locked --manifest-path native/kws-probe/Cargo.toml
cargo clippy --offline --locked --all-targets --manifest-path native/kws-probe/Cargo.toml -- -D warnings
```

Python is `.venv/bin/python` (3.14). Useful existing assets:

| Asset | Location |
| --- | --- |
| Public s16le/16 kHz mono fixture | `build/refactor-review/goforward.raw` |
| KWS English int8 model, BPE, upstream public WAVs | `build/memory-plan-kws/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01/` |
| Native KWS C API and adjacent ONNX Runtime | `.venv/lib/python3.14/site-packages/sherpa_onnx/lib/libsherpa-onnx-c-api.so` |
| KWS configs, keyword files and generated cases | `build/memory-plan-kws/` and its `matrix/` directory |
| Development-only SentencePiece | `build/memory-plan-kws/python/`; use `PYTHONPATH` only for keyword preparation |
| Vosk small English | `/home/silver/.local/share/murmur/models/vosk-model-small-en-us-0.15` |
| Whisper small English | `/home/silver/.local/share/murmur/models/sherpa-onnx-whisper-small.en` |
| Experimental Whisper tiny/base | `build/memory-plan-models/models/sherpa-onnx-whisper-tiny.en/` and `sherpa-onnx-whisper-base.en/` |
| Workspace-only ORT 1.31 numerical diagnostics | `build/memory-plan-models/python/`; not the KWS inference library or a production dependency |

All relative paths above are relative to this checkout. The public fixture
SHA-256 is
`f15c60ec54059d8b66e410d0064945a0b0a04ea56e1ddca1958e493c0cf70e71`.
`/tmp` is tmpfs on this host; disk replay experiments use the Btrfs workspace.

Latest binary SHA-256 values:

```text
saved ungated jarvis-kws-probe baseline:
3bead979c897d0c14b9ce2c3394239ec77dc929b0f43ca8ea1eb76d531dfd657
jarvis-kws-probe (prior quiet gate, preserved gate-candidate copy):
d6b2ddef281e3edc464271f9d01499c07465e7efb3bbb554be1e54504637716f
jarvis-kws-handoff-probe (initial file-endpoint experiment, saved handoff-candidate):
42d2a534cacca9f2d383e37b41c6285945e9c74518e1f0a956a5d48b554d5dc7
jarvis-kws-handoff-probe (prior cycles, saved handoff-cycles-preserved):
dcc3191ad0b0c38f9f22f04ae44bdcef833ecd6d4d69849c0a7a562efe1be9f6
jarvis-kws-probe (prior heap diagnostic; continuous-candidate copy):
a233211113d7a5465b04589746f95f5673e6f7bf1343168bf750d40369ffd7de
jarvis-kws-handoff-probe (prior continuous input, continuous-candidate copy):
9b2e4402b6ebb95b9e403127da98ff39d61e9b0355af4e5e6e3ad183177b74d2
jarvis-kws-probe (prior reader/cache normal build, saved reader-cache-candidate):
df6d5df9c716c5f93ae2c02367546616b2d1d093f108965fc5be51a9aee331c2
jarvis-kws-probe (current optional alignment build, saved alignment-candidate):
3f0030314b153a89bac0c96163e668ef838850b93daf22f322929d96623bbe51
jarvis-kws-handoff-probe (prior fault recovery, pressure-allocation-candidate):
8691d2bef0fad8d59e5a8772f6e0ba01721ea0afffa9e3d777350c876c802ecf
allocation-build/release/jarvis-kws-probe (prior reader/cache counter; not rebuilt for alignment):
3422f23742f3328637dc6cb2ee76041e143c8b06e57a77ee481fc4e9c90b659f
jarvis-kws-handoff-probe (prior Rust counter, pressure-allocation-candidate):
423bb8bf96d2789352a3cc066a0eb6cc3b4ad9812127b081adf4dcce26235e84
jarvis-kws-handoff-probe (current owned reader, normal build):
2f1843d9a6c0224219354c0b39364b9b08b553b33aabd972fad51f5fe3fa4791
allocation-build/release/jarvis-kws-handoff-probe (current owned reader/counter):
132fb2b719b9cf1ccb38121a2b1d91c5df0f9baeb72b1a8ca68b89baa497e884
src/murmur/bin/jarvis-voice (also build/memory-plan-models/jarvis-voice):
212ff8655e303b55f0a70f902753d0ab6f87031aa3c71cd40306ddf5cf8d9844
native/whisper-recognizer/target/release/jarvis-whisper:
8915a1cfb58d7faec35f4409dd7cb1da17b55191f38f511129caf53e06c087b1
```

The KWS probe lives at `native/kws-probe/target/release/jarvis-kws-probe`.
Keep measured hashes tied to the actual executables used; rebuilding after
changing code must not silently replace the benchmark baseline.

## Validation at handoff

[Alignment validation](benchmarks/kws-alignment-validation-2026-10-27.json)
records 18 passing Rust tests per build variant (overlapping), ten passing
explicit Python tests without skips, and passing format, all-feature/all-target
Clippy, compilation, packaging and diff checks. It verifies the source archive,
both normal binaries, 971 raw input-hash uses and 313 aligned accounting reports.
The matrix's exit 1 and intended-label/strict timing-identity failures remain
explicit. The six-row cropped diagnostic retains positive labels and failed
recognition while reproducing the predicted context behavior. No full Python
suite, Windows, parent/pressure matrix or full-duration soak is newly claimed.

[Reader/cache validation](benchmarks/kws-reader-cache-validation-2026-10-26.json)
records 15 passing Rust tests per build variant (same overlapping tests), nine
passing explicit Python tests with no skips, and passing formatting, all-feature/
all-target Clippy, compilation, package and diff checks. All 42 submitted
buffers and saved source/binary hashes were rechecked. Cache comparisons,
eight continuous controls and three confined cgroup controls pass. OOM exits
and recognition failures remain explicit. All experiment processes and transient
units exited; the full Python suite was not rerun.


[Allocation/pressure validation](benchmarks/kws-pressure-allocation-validation-2026-10-26.json)
records 12 passing Rust tests in each build variant (same tests, not additive),
nine passing explicit Python tests with no skips, and passing formatting,
all-feature/all-target Clippy, compilation, package and diff checks. All 35
allocation/pressure buffers and saved binary/source hashes were rechecked.
Three synthetic fault scenarios and three actual cgroup lifecycle controls
pass. OOM raw exits 1 and failed recognition gates remain deliberate. The full
Python suite was not rerun. All experiment processes and transient units exited.


[Continuous-input validation](benchmarks/kws-continuous-validation-2026-10-26.json)
records 12 passing Rust tests and eight passing explicit Python tests (no
skips), passing formatting/Clippy/package/compilation checks; eight lifecycle controls; 16 handoff floor cycles and eight detector-only
requests. Timing diagnosis deliberately exits 1 (18 later gated variants),
with all 36 cropped controls reproducing exact gated trigger chunks. The full
Python suite was not rerun for these isolated changes. The previous acoustic
and small.en memory failures remain unchanged.


[Cycles/endpoint validation](benchmarks/kws-handoff-cycles-validation-2026-10-26.json)
records ten Rust unit tests, seven explicit Python tests, passing format/Clippy/
package/compilation checks, the matrix's expected acoustic exit 1, seven passing
focused diagnostic controls and ten passing floor cycles. The full offline
suite was not rerun. Exact binary/source and raw-evidence hashes are included.

[Quiet-gate/handoff validation](benchmarks/kws-quiet-gate-validation-2026-10-26.json):

- Eight Rust unit tests and seven explicit Python probe/model/protocol tests
  pass. Formatting, all-target Clippy with warnings denied, offline locked
  release builds, package inclusion/exclusion, Python compilation and diff
  checks pass. Every saved handoff buffer and measured executable/source hash
  was rechecked. The old full suite was not rerun for these isolated changes.
- The quiet-gate script exits 0 for label/default-trigger equivalence; the
  separate low-volume trigger-time gate is false. The handoff script exits 0
  for its 18 fixture cases; its small.en memory target is false. No whole-app,
  full acoustic, Windows or soak gate passes.

[Prior full-suite validation](benchmarks/kws-validation-2026-10-26.json):

- Full offline Python suite: **359 total, 322 passed, 37 explicitly skipped**.
- Explicit KWS model/protocol suite: **4 passed**, including native library
  maps confirming sherpa/ORT are loaded and Python is not.
- New probe Rust unit tests: **3 passed**. Format check and all-target Clippy
  with warnings denied pass. Header/license/Cargo source-package inclusion,
  probe target exclusion, Python compilation and diff checks pass.
- The prior Whisper phase records **60 passing selected voice/model tests**
  and **34 Rust tests** separately in its validation artifact. These were not
  all rerun for the KWS-only change; do not add overlapping counts together.
- The default sandbox blocks local Unix sockets used by existing daemon and
  controller tests. The initial restricted run was interrupted; rerunning
  with temporary local-socket access passed in about 12 seconds. Long paced
  benchmarks also used approved extended execution to avoid premature
  launcher termination. No automatic approval rejection remains unresolved.
- No experiment or test session is left running by this campaign.

Reproduction commands for the model matrix and model-test environment are in
[the KWS report](kws-experiment.md#reproduce-with-already-supplied-assets).
Do not repeat large benchmarks after passing checks without a new change or
an unresolved question. Preserve expected nonzero acoustic-gate results.

## Resume prompt

> Continue the Jarvis memory plan from docs/memory-plan-handoff.md. Respect
> the user's current priorities: Windows is set aside, further optional disk
> optimization is deferred, and the live installation is back on RAM replay.
> Focus on voice waiting/command memory, wake reliability and latency. Preserve
> production defaults, private recordings and saved failed baselines. All KWS
> arms miss the selected real Jarvis tail; model-derived alignment also retains
> a failed recognition gate. Read docs/wake-candidate-screening.md for subsequent
> tiny, dedicated-model and PocketSphinx energy-gate results. The user has now
> confirmed the selected phrase. Read docs/dual-wake-screen.md: the fixed
> combined candidate passes known controls but has eight paired regressions on
> fresh public recordings. Do not port or tune it to those failures. Resolve
> the pending waiting-budget question from a real user reply before changing
> any target; keep working on independent command/retention evidence meanwhile.
> Continue missing corpus evidence, architecture selection, voice integration,
> whole-application Linux/pressure and three sequential 24-hour soaks. Deferred
> Windows and strict-disk-quota gates remain unpassed. Do not start real capture,
> repeat unchanged matrices, or infer full acceptance from public-file controls.
