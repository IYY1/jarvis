# Owned child readers and allocator-cache attribution

This follows [allocation ownership and private pressure recovery](kws-pressure-allocations.md).
Changes remain inside the separate Rust probe and diagnostic scripts. Production
Vosk/small.en, model selection and both experimental Vosk flags are unchanged.
No real capture, action dispatch or installed-service restart was performed.

## Reader ownership and cancellation

The parent now retains each child's stdout-reader handle and joins it during
normal teardown and failure cleanup. It disconnects the bounded event channel
before joining, so a producer blocked on a full queue can leave `send()`.
The reader polls a pipe in bounded intervals and reads fixed chunks, with a
one-MiB line limit and explicit cancellation. It can stop when another writer
keeps a partial line/pipe open; shutdown does not wait indefinitely for a newline.
EOF still accepts a complete final JSON value without a newline.

The new Rust tests exercise cancellation with a live partial-line writer,
backpressure on a full event queue, and final JSON at EOF. These test resource
ownership rather than acoustic recognition. They also cover failure paths that
a normal small worker response would not exercise.

`join_readers` defaults true in this isolated probe. False is an explicit
diagnostic comparison using the same new cancellable reader, not the historical
binary. Per-floor reports expose active/spawned/joined reader counts and OS
thread counts. The joined mode guarantees completed command readers have been
joined before their waiting floor is sampled; the detector reader stays active.

Continuous summaries now separate `successful_workers`, `aborted_windows` and
`worker_failures`. A timeout/EOF cancellation no longer makes
`all_command_workers_succeeded` true merely because there was no unexpected
worker fault. Earlier captures computed that flag from fault count alone;
their explicit release reasons and actual commands remain preserved. Faults
still make the final probe exit 1 even after recovery; expected diagnostic
cancellations retain their earlier explicit control semantics.

## Controlled cache/lifetime comparisons

[New comparison evidence](benchmarks/kws-reader-caches-2026-10-26.json) records
three sequential fresh detector arms, each replaying 128 identical unpaced
requests: default allocator behavior, per-thread malloc cache disabled, and
pthread stack cache disabled. All 384 expected wakes match the same exact
original-input trigger chunks. All arms use the existing Rust allocation
counter, fixed threshold 0.25 and identical detector configuration.

Four fresh parent arms then run eight paced tiny.en command cycles each:
detached readers, joined readers, joined with the parent's malloc cache disabled,
and joined with its stack cache disabled. All 32 commands and source slices
match. To isolate the parent policy, every arm explicitly gives both children
the same default `GLIBC_TUNABLES` value. The override is a diagnostic environment
variable; it is absent from normal probe operation.

The host reports GNU libc 2.44. GNU documents that
[`glibc.malloc.tcache_count=0`](https://sourceware.org/glibc/manual/latest/html_node/Memory-Allocation-Tunables.html)
disables the per-thread malloc cache. Its
[`glibc.pthread.stack_cache_size`](https://sourceware.org/glibc/manual/latest/html_node/POSIX-Thread-Tunables.html)
control bounds retained unused thread stacks. The experiments set each selected
cache count/size to zero at process startup. They request no extra heap trim,
change no global environment, and adopt no allocator setting for Jarvis. The
existing command-worker allocator policy stays fixed across parent comparisons.

| Detector arm, requests 1 → 128 | glibc in-use growth | Rust live bytes / blocks | RSS growth |
| --- | ---: | ---: | ---: |
| Default | +15,568 bytes | 40,744 / 41, unchanged | 0 |
| Malloc cache disabled | **0 bytes** | 40,744 / 41, unchanged | 0 |
| Stack cache disabled | +14,320 bytes | 40,744 / 41, unchanged | 0 |

| Parent arm, cycles 1 → 8 | Parent glibc in-use growth | Parent RSS growth | Rust live blocks |
| --- | ---: | ---: | ---: |
| Detached, default caches | +19,760 bytes | +0.207 MiB | 1,078, unchanged |
| Joined, default caches | +20,848 bytes | +0.207 MiB | 1,078, unchanged |
| Joined, malloc cache disabled | **0 bytes** | +0.121 MiB | 1,078, unchanged |
| Joined, stack cache disabled | +20,624 bytes | +0.207 MiB | 1,078, unchanged |

Requested Rust live bytes are constant within each parent arm; absolute values
differ with diagnostic paths/environment/report contents. All parent floors
have three OS threads and one active detector reader. Joined reader counts
increase by exactly one per completed command, while the detached control's
joined count remains zero. There is no accumulating live-reader/thread count.
One parent arm's resident detector RSS warms by 0.066 MiB; other detector RSS
floors are flat. Treat these small fresh-process differences as observations,
not a cache-induced memory optimization.

The controls support malloc-cache occupancy as a major explanation of the
small glibc in-use trend, rather than accumulating Rust objects or unfinished
child readers. Joining readers does not remove that trend; disabling the malloc
cache removes it in both isolated processes, while disabling the stack cache
does not. This is causal evidence about the selected cache setting, not an
allocation-stack attribution of every byte inside native libraries. Source
stacks would be needed to distinguish cache entries originating from Rust frees
from entries originating in native code; requested-live counters exclude both
kinds of freed-but-cached memory.

Even without the malloc cache, the parent RSS warms by 0.121 MiB. Stable in-use
counts do not imply all resident pages are reclaimed, and this short comparison
cannot establish a long-run plateau, leak freedom, or the three 24-hour soaks.
No CPU/latency/platform acceptance supports adopting these tunables. The normal
allocator policy remains unchanged.

## Regression controls and remaining gates

[Continuous regression evidence](benchmarks/kws-reader-continuous-2026-10-26.json)
rechecks the eight lifecycle controls after changing the stdout transport and
teardown. The summary now checks successful/aborted/fault counts and exact
reader joins, as well as commands, source slices, timeout recovery and overflow.

[Pressure regression evidence](benchmarks/kws-reader-pressure-2026-10-26.json)
rechecks the three private cgroup controls because worker OOM now follows the
new join path. Raw exits and recognition failures in OOM controls stay explicit;
passing cleanup does not make a missed command a passing recognition gate.
All temporary units are collected and no installed service is changed.

[Validation](benchmarks/kws-reader-cache-validation-2026-10-26.json) records
15 passing Rust tests in the normal build and the same 15 in the diagnostic
build (overlapping counts), nine passing explicit Python tests with no skips,
formatting, all-feature/all-target Clippy with warnings denied, compilation,
source-package inclusion/target exclusion and exact hashes. All 42 submitted
cache/lifecycle/pressure buffers were rechecked. All experiment processes exited
and all three temporary pressure units were collected. The saved candidate is
`build/memory-plan-kws/reader-cache-candidate/snapshot.json`; earlier binary/source
snapshots and all failed timing/endpoint/small.en memory evidence remain intact.
The full Python suite was not rerun for this isolated change.

Reproduce with the cached toolchain and existing assets from
[the handoff](memory-plan-handoff.md), using new paths:

```sh
cargo build --release --offline --locked --features allocation-diagnostics --target-dir build/memory-plan-kws/allocation-build --manifest-path native/kws-probe/Cargo.toml
.venv/bin/python scripts/benchmark_kws_reader_caches.py --config "$WORK/endpoint-controls/natural-rms-0.001/config.json" --fixture "$WORK/cycles/tiny.en-gated-natural/input.raw" --work-dir "$NEW_CACHE_WORK" --output "$NEW_CACHE_RESULT"
```

Do not use these diagnostic cache settings to hide a default-policy trend or
claim a budget win. The detector timing failure (up to +270 ms), RMS 0.005
low-volume endpoint failure and small.en command-memory failure remain open.
Actual Jarvis-name positives, confusables/background negatives, whole-application
budgets, arbitrary-victim pressure recovery, Windows and full-duration soaks
remain required before integration. Further work should prioritize the timing
architecture and missing acoustic/platform evidence; exact native allocation
stacks are useful if a continuing default-policy trend appears in longer runs.
