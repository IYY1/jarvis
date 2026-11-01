# Memory plan: native Windows component validation

On 27 October 2026, Windows 10 Enterprise Evaluation 22H2, build 19045.2006,
was installed in an isolated KVM VM with 8192 MiB configured RAM and four
virtual CPUs. Windows reports 8,589,389,824 bytes of usable RAM. The official
Microsoft ISO's SHA-256 matches Microsoft's published checksum PDF. Private
VM credentials, transport tokens, disks and setup files remain under ignored
`build/memory-plan-windows-vm/`.

The VM contains prepared source, Windows Python 3.13.17, models, the native
Windows replay-store helper and FFmpeg. It contains no user recordings, and
its audio device has no host microphone connection. Component video tests use
generated frames. The host desktop and installed Jarvis service were not used
by these Windows tests.

| Check | Result |
| --- | --- |
| Native Windows memory, storage and Windows adapter suites | 56 tests: 51 passed, 5 platform skips |
| Updated accounting suite on Windows | 13 tests: 12 passed, 1 Linux skip |
| Full Linux offline Python suite after the change | 381 tests: 341 passed, 40 skips |
| CodeRabbit review of the accounting diff and existing measurement/progress docs | 0 issues |

The Windows suites overlap; their counts must not be added as unique tests.
The storage suite exercises native named-pipe capture, private ACLs, byte/time
retention, pinned saves during rollover/stop, stale-session cleanup, generated
video remux/full decode, and bounded helper I/O. This is real Windows component
evidence, rather than Wine or platform mocks. Physical display, microphone,
audio synchronization and GPU-driver acceptance remain open.

## Memory-counter compatibility fix

This older Windows build supplies private commit but lacks the newer EX2
private-working-set counter. The unchanged code correctly reported the latter
as unavailable, leaving the diagnostic snapshot incomplete. The updated code
uses `QueryWorkingSet` to count non-shareable resident pages when EX2 is absent.
It bounds retries and buffer size, closes its stronger-access process handle,
and retains `null` when access or reliable collection is unavailable.

The native regression touches an 8 MiB private allocation and observes the
increase. An independent PDH private-working-set comparison differs by only
32 KiB from the preceding fallback sample (26,796,032 versus 26,763,264 bytes),
with a second fallback sample of 26,783,744 bytes. These are sequential samples,
so exact equality is not expected. Commit is still a separate metric.

## RAM versus disk buffer measurements

Three sequential fresh-worker pairs append the same generated one-second video
180 times into a 120-second, 256 MiB buffer. The meter includes the Python
buffer worker and its Rust helper in disk mode.

| Storage | Sampled peak private working set | Sampled peak private commit |
| --- | --- | --- |
| RAM | 248.60–248.81 MiB | 250.77–250.98 MiB |
| Disk | 20.50–20.63 MiB | 23.88–24.05 MiB |

The saving is about 228 MiB for this **buffer layer**. The encoder runs before
measurement to generate the fixture. Voice, live capture, controller and
settings are excluded. Filling is accelerated, so this establishes neither
real-time throughput nor gameplay performance or SSD wear. Reclaimable OS file
cache is outside process private working set. No combined application budget
passes from this result.

## Voice engine baseline

Two fresh native Windows Python engines replay the supplied public “go forward
ten meters” fixture with the normal matcher and an explicit test alias. Both
recognize one wake and the expected command. Feeding is paced; no device or
action dispatcher is opened. With the VM isolated again, the short baseline
records the following sampled private-working-set peaks:

| Profile | Waiting after ready | Command phase | After command |
| --- | --- | --- | --- |
| Explicit Vosk-only control | 130.25 MiB | 144.04 MiB | 145.76 MiB |
| Production small.en + GTCRN | 549.28 MiB | 703.92 MiB | 567.51 MiB |

The production engine alone exceeds the combined waiting and command budgets.
The Vosk-only control disables accurate recognition and is not a proposed
production setting. The meter, service, capture, replay and settings are
excluded. Waiting is observed for three seconds after ready, with approximately
100 ms sampling. This establishes no plateau, soak, full recognition coverage
or command-latency nonregression. Private commit is also recorded separately
and reaches about 1.16 GiB in the Vosk-only control; that observation does not
establish a leak. See [full voice samples and scope](benchmarks/windows-voice-engine-2026-10-27.json).

## VM preparation and remaining acceptance

Setup rejected an XML namespace prefix introduced by the preparation script;
preserving the standard default namespace fixed it. The initial QEMU socket
forwarder also disconnected after the first HTTP response. Per-connection
forwarding and HTTP/1.1 handling replace that transport. Failed setup and
transfer attempts remain preserved. The installed Windows disk was retained
through orderly shutdowns.

The offline evaluation initially reported an expired license and zero grace.
Temporary internet access activated Windows normally. The licensing API now
reports `LicenseStatus=1` and 129,594 minutes of remaining grace (about 90 days),
and the VM's network is restricted again. No clock change, rearm or license
bypass was used.

Whole-app workloads, settings-open costs, command peaks, real capture/GTCRN,
CPU/latency, 8 GB pressure and three final sequential 24-hour soaks remain open.
The selected recognition architecture is still unresolved. A VM using four
modern host cores does not establish ordinary laptop CPU or GPU performance.

Evidence: [component tests and counter comparison](benchmarks/windows-memory-plan-components-2026-10-27.json),
[full generated-buffer samples](benchmarks/windows-replay-storage-2026-10-27.json).
The latter preserves the old driver's Linux-specific wording and adds an
explicit correction identifying these native Windows measurements.
