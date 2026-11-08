#!/usr/bin/env python3
"""Compare Rust voice workers on the Linux voice path with a live-like configuration.

Each run starts a fresh worker with its own temporary payload: wake names
"jarvis"/"alfred", Arabic + English (Moonshine Arabic beside Whisper), the
light (tiny.en) Whisper model and GTCRN noise suppression, matching the
default Linux controller payload (60-second idle release, finalized Vosk
utterances, up to eight threads). A paced fake ``pw-record`` plays the
supplied fixture repeatedly; no microphone, desktop action, service or user
configuration is touched. Arms alternate run by run.

Measured per run: worker start to ``ready``, RSS/anonymous RSS while waiting,
latency from the fixture's last voiced 30 ms frame to the command event (first
command cold, the rest warm), peak RSS (VmHWM), RSS after Whisper/Moonshine
are released, idle CPU over a silent stretch and total CPU.

Other modes: --engine times unpaced --engine requests per fixture; --load
times on-demand model loading after a ``listen`` control; --equality runs
Moonshine, Whisper tiny.en and the full worker over every fixture (folders
allowed) and fails if any arm's outputs differ from the first arm's.
"""
from __future__ import annotations

import argparse
import array
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jarvis.commands import Parser  # noqa: E402
from jarvis.config import DEFAULTS  # noqa: E402
from jarvis.native_worker import prepare  # noqa: E402

CAPTURE = """#!/usr/bin/env python3
import os, time
from pathlib import Path
audio = Path(os.environ['JARVIS_BENCH_PCM']).read_bytes()
audio += bytes((-len(audio)) % 960)
log = open(os.environ['JARVIS_BENCH_LOG'], 'a', buffering=1)
start = time.monotonic()
sent = 0
def feed(frames):
    global sent
    for i in range(0, len(frames), 960):
        os.write(1, frames[i:i + 960])
        sent += 1
        delay = start + sent * .03 - time.monotonic()
        if delay > 0:
            time.sleep(delay)
feed(bytes(960) * round(float(os.environ['JARVIS_BENCH_LEAD']) / .03))
for _ in range(int(os.environ['JARVIS_BENCH_REPEATS'])):
    log.write(f"start {time.monotonic()}\\n")
    feed(audio)
    log.write(f"end {time.monotonic()}\\n")
    feed(bytes(960) * round(float(os.environ['JARVIS_BENCH_GAP']) / .03))
log.write(f"quiet {time.monotonic()}\\n")
while True:
    feed(bytes(960))
"""

APPS = ["firefox", "steam", "discord", "stremio", "spotify", "obs", "terminal", "files"]


def trailing_silence(pcm: bytes) -> float:
    samples = array.array("h", pcm[:len(pcm) // 2 * 2])
    voiced = [i for i in range(0, len(samples), 480)
              if math.sqrt(sum(s * s for s in samples[i:i + 480]) / len(samples[i:i + 480])) / 32768 >= .005]
    return (len(samples) - (voiced[-1] + 480)) / 16000 if voiced else 0.0


def status(pid: int) -> dict:
    values = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in ("VmRSS", "VmHWM", "RssAnon", "RssFile", "Threads"):
            values[key] = int(rest.split()[0]) / (1 if key == "Threads" else 1024)
    return values


def cpu_seconds(pid: int) -> float:
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")


def model_threads(pid: int) -> int:
    count = 0
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            count += (task / "comm").read_text().startswith("whisper-")
        except OSError:
            pass
    return count


def payload(threads: int | None, idle: float) -> tuple[dict, dict]:
    config = copy.deepcopy(DEFAULTS)
    config["privacy"]["onboarded"] = True
    config["activation"].update(wake_phrase="jarvis", wake_aliases=["alfred"], sensitivity=.5, command_timeout=8)
    config["recognition"].update(accurate=True, noise_suppression=True, closest_match=True,
                                 speech_model="light", language="ar_en")
    config["audio"].update(device="", vad_rms=.005, adaptive_gate=True)
    grammar = Parser({}).grammar(APPS)
    with patch.dict(os.environ, JARVIS_VOICE_WORKER="rust"):
        _argv, data, env = prepare(config, "wake_word", grammar)
    native = data["native"]
    if not native.get("whisper") or not native.get("denoiser") or not native.get("arabic"):
        raise SystemExit("Install the tiny.en Whisper, GTCRN and Arabic models first")
    native["whisper_idle_seconds"] = idle
    native["vosk_finalize_utterances"] = True
    if threads is not None:
        native["threads"] = threads
    return data, env


def run(binary: Path, fixture: Path, repeats: int, gap: float, idle: float, lead: float,
        quiet_seconds: float, threads: int | None) -> dict:
    data, env = payload(threads, idle)
    pcm = fixture.read_bytes()
    with tempfile.TemporaryDirectory(prefix="jarvis-linux-voice-") as folder:
        root = Path(folder)
        capture = root / "pw-record"
        capture.write_text(CAPTURE)
        capture.chmod(0o700)
        log = root / "speech.log"
        env.update(PATH=f"{root}:{env.get('PATH', '')}", JARVIS_BENCH_PCM=str(fixture.resolve()),
                   JARVIS_BENCH_LOG=str(log), JARVIS_BENCH_LEAD=str(lead), JARVIS_BENCH_GAP=str(gap),
                   JARVIS_BENCH_REPEATS=str(repeats), JARVIS_OWNER_PID=str(os.getpid()))
        start = time.monotonic()
        process = subprocess.Popen([str(binary.resolve())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   env=env, bufsize=0)
        samples, commands, events, ready = [], [], [], None
        quiet_cpu = None
        try:
            process.stdin.write((json.dumps(data) + "\n").encode())
            deadline = start + 30 + lead + repeats * (gap + 10) + quiet_seconds
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                quiet_start = None
                while True:
                    now = time.monotonic()
                    if now > deadline:
                        raise RuntimeError(f"timed out; commands so far {commands}")
                    if process.poll() is not None:
                        raise RuntimeError("The worker exited during the benchmark")
                    if len(commands) >= repeats and quiet_start is None:
                        marks = log.read_text().split()
                        if "quiet" in marks:
                            quiet_start = (now, cpu_seconds(process.pid))
                    if quiet_start and now - quiet_start[0] >= quiet_seconds:
                        quiet_cpu = (cpu_seconds(process.pid) - quiet_start[1]) / (now - quiet_start[0])
                        break
                    s = status(process.pid)
                    samples.append((now, s["VmRSS"], s["RssAnon"], model_threads(process.pid)))
                    if not selector.select(.02):
                        continue
                    message = json.loads(process.stdout.readline())
                    events.append((time.monotonic() - start, message.get("event")))
                    if message["event"] == "error":
                        raise RuntimeError(message["message"])
                    if message["event"] == "ready" and ready is None:
                        ready = time.monotonic()
                    if message["event"] == "command":
                        commands.append((time.monotonic(), message["text"], message.get("heard"),
                                         message.get("recognition_pass")))
            final = status(process.pid)
            cpu = cpu_seconds(process.pid)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()
        marks = [line.split() for line in log.read_text().splitlines()]
    starts = [float(t) for kind, t in marks if kind == "start"]
    ends = [float(t) - trailing_silence(pcm) for kind, t in marks if kind == "end"]
    quiet_mark = next(float(t) for kind, t in marks if kind == "quiet")

    def before(moment):
        return max((s for s in samples if s[0] <= moment), key=lambda s: s[0])

    results = [{"text": text, "heard": heard, "pass": rpass,
                "latency_ms": round((received - ended) * 1000, 1),
                "models_loaded_at_speech_start": before(began)[3] > 0,
                "rss_at_speech_start_mib": round(before(began)[1], 2)}
               for (received, text, heard, rpass), began, ended in zip(commands, starts, ends, strict=True)]
    waiting = [s for s in samples if ready + .5 <= s[0] < starts[0]]
    released = [s for s in samples if s[0] > quiet_mark + idle + 2 and s[3] == 0]
    return {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "ready_seconds": round(ready - start, 3),
            "waiting_rss_mib": round(statistics.median(s[1] for s in waiting), 2),
            "waiting_anon_mib": round(statistics.median(s[2] for s in waiting), 2),
            "released_rss_mib": round(statistics.median(s[1] for s in released), 2) if released else None,
            "released_anon_mib": round(statistics.median(s[2] for s in released), 2) if released else None,
            "max_sampled_rss_mib": round(max(s[1] for s in samples), 2),
            "peak_rss_mib": round(final["VmHWM"], 2),
            "quiet_cpu_percent_of_core": round(quiet_cpu * 100, 3) if quiet_cpu is not None else None,
            "cpu_seconds": round(cpu, 2), "commands": results}


def summarize(runs: list[dict]) -> dict:
    def med(values):
        values = [v for v in values if v is not None]
        return round(statistics.median(values), 2) if values else None
    cold = [c["latency_ms"] for r in runs for c in r["commands"] if not c["models_loaded_at_speech_start"]]
    warm = [c["latency_ms"] for r in runs for c in r["commands"] if c["models_loaded_at_speech_start"]]
    out = {key: med(r[key] for r in runs) for key in (
        "ready_seconds", "waiting_rss_mib", "waiting_anon_mib", "released_rss_mib", "released_anon_mib",
        "peak_rss_mib", "quiet_cpu_percent_of_core", "cpu_seconds")}
    out.update(cold_latency_ms=med(cold), warm_latency_ms=med(warm), cold_n=len(cold), warm_n=len(warm),
               per_run_peak=[r["peak_rss_mib"] for r in runs],
               per_run_waiting=[r["waiting_rss_mib"] for r in runs],
               texts=sorted({c["text"] for r in runs for c in r["commands"]}))
    return out


def engine(binary: Path, fixtures: list[Path], repeats: int, threads: int | None) -> dict:
    """Unpaced ``--engine`` requests with Whisper and Moonshine resident.

    Each request feeds a whole fixture as a wake session (wake + command in one
    breath) and as a command session. ``seconds`` covers Vosk decoding of the
    fixture plus the post-endpoint recognition passes; events are recorded for
    the output-equality check.
    """
    data, env = payload(threads, 60.0)
    data["native"].pop("whisper_idle_seconds")
    process = subprocess.Popen([str(binary.resolve()), "--engine"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               env=env, text=True)
    try:
        process.stdin.write(json.dumps(data) + "\n")
        process.stdin.flush()
        ready = json.loads(process.stdout.readline())
        rows = {}
        for fixture in fixtures:
            for session in ("wake", "command"):
                times, events = [], None
                for _ in range(repeats):
                    process.stdin.write(json.dumps({"path": str(fixture), "session": session}) + "\n")
                    process.stdin.flush()
                    result = json.loads(process.stdout.readline())
                    if result.get("event") != "result":
                        raise RuntimeError(result)
                    times.append(result["seconds"])
                    if events is None:
                        events = result["events"]
                    elif events != result["events"]:
                        raise RuntimeError(f"{fixture}: events changed between repetitions")
                rows[f"{fixture.name}:{session}"] = {"median_ms": round(statistics.median(times) * 1000, 2),
                                                     "events": events}
        peak = json.loads(json.dumps(result["memory"]))
    finally:
        process.stdin.close()
        process.wait()
    return {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "load_seconds": ready["load_seconds"],
            "peak_rss_mib": peak["peak_rss_mib"], "rows": rows}


def task_names(pid: int) -> list[str]:
    names = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            names.append((task / "comm").read_text().strip())
        except OSError:
            pass
    return names


def load_wall(binary: Path, threads: int | None) -> dict:
    """Seconds from a ``listen`` control to every model loaded.

    ``listen`` starts a command session, which starts the on-demand loads
    (Moonshine, Whisper and GTCRN) on ``whisper-load`` threads; the load is
    over when none of those threads remain. Paced silence only.
    """
    data, env = payload(threads, 60.0)
    with tempfile.TemporaryDirectory(prefix="jarvis-load-") as folder:
        capture = Path(folder) / "pw-record"
        capture.write_text("#!/usr/bin/env python3\nimport os, time\nwhile True:\n"
                           "    os.write(1, bytes(960)); time.sleep(.03)\n")
        capture.chmod(0o700)
        env.update(PATH=f"{folder}:{env.get('PATH', '')}", JARVIS_OWNER_PID=str(os.getpid()))
        process = subprocess.Popen([str(binary.resolve())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   env=env, text=True)
        try:
            process.stdin.write(json.dumps(data) + "\n")
            process.stdin.flush()
            while (message := json.loads(process.stdout.readline()))["event"] != "ready":
                if message["event"] == "error":
                    raise RuntimeError(message["message"])
            time.sleep(1.0)
            start = time.monotonic()
            process.stdin.write("listen\n")
            process.stdin.flush()
            seen = False
            while True:
                loading = "whisper-load" in task_names(process.pid)
                seen |= loading
                if seen and not loading:
                    break
                if time.monotonic() - start > 10:
                    raise RuntimeError("models did not load within 10 s")
                time.sleep(.002)
            wall = time.monotonic() - start
            time.sleep(.3)
            after = status(process.pid)
        finally:
            process.terminate()
            process.wait()
    return {"load_wall_ms": round(wall * 1000, 1), "peak_rss_mib": round(after["VmHWM"], 2),
            "loaded_rss_mib": round(after["VmRSS"], 2)}


def corpus(paths: list[Path]) -> list[Path]:
    """Fixture files; a directory contributes its *.raw and *.s16le files."""
    files = []
    for path in paths:
        files += sorted([*path.glob("*.raw"), *path.glob("*.s16le")]) if path.is_dir() else [path]
    return [f for f in files if f.stat().st_size >= 6400 and f.stat().st_size % 2 == 0]


def batch(argv: list[str], files: list[Path], limit: int) -> dict:
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                               stderr=subprocess.DEVNULL)
    line = process.stdout.readline()
    if not line:
        process.wait()
        raise RuntimeError(f"{argv[0]} did not start")
    rows = {}
    for path in files:
        if path.stat().st_size > limit:
            continue
        process.stdin.write(json.dumps({"path": str(path)}) + "\n")
        process.stdin.flush()
        result = json.loads(process.stdout.readline())
        rows[str(path)] = {"text": result["text"], "seconds": result["seconds"]}
    process.stdin.close()
    process.wait()
    return rows


def outputs(binary: Path, files: list[Path], threads: int | None) -> dict:
    """Moonshine, Whisper tiny.en and full-worker results for every file.

    ``binary`` is a jarvis-voice whose folder also holds jarvis-moonshine and
    jarvis-whisper. Moonshine runs on ``threads`` (default 8) when the binary
    accepts --threads; older binaries run single-threaded, which gives the
    same transcripts. Files over 10 s skip the worker, over 20 s Whisper.
    """
    folder = binary.resolve().parent
    models = Path.home() / ".local/share/jarvis/models"
    moonshine = [str(folder / "jarvis-moonshine"), "--batch", str(models / "moonshine-streaming-tiny-ar")]
    try:
        moon = batch(moonshine[:1] + ["--threads", str(threads or 8)] + moonshine[1:], files, 960000)
    except RuntimeError:
        moon = batch(moonshine, files, 960000)
    whisper = batch([str(folder / "jarvis-whisper"), "--threads", str(threads or 8), "--model-name", "tiny.en",
                     "--batch", str(models / "sherpa-onnx-whisper-tiny.en")], files, 640000)
    data, env = payload(threads, 60.0)
    data["native"].pop("whisper_idle_seconds")
    process = subprocess.Popen([str(binary.resolve()), "--engine"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               text=True, env=env)
    rows = {}
    try:
        process.stdin.write(json.dumps(data) + "\n")
        process.stdin.flush()
        json.loads(process.stdout.readline())
        for path in files:
            if path.stat().st_size > 320000:
                continue
            for session in ("command", "wake"):
                process.stdin.write(json.dumps({"path": str(path), "session": session}) + "\n")
                process.stdin.flush()
                result = json.loads(process.stdout.readline())
                if result.get("event") != "result":
                    raise RuntimeError(result)
                rows[f"{path}:{session}"] = {"events": result["events"], "seconds": result["seconds"]}
    finally:
        process.stdin.close()
        process.wait()
    return {"moonshine": moon, "whisper": whisper, "engine": rows}


def equality(results: dict) -> dict:
    """Each arm against the first: identical outputs and total decode time."""
    names = list(results)
    first = results[names[0]]
    summary = {}
    for name in names:
        arm, out = results[name], {}
        for kind, field in (("moonshine", "text"), ("whisper", "text"), ("engine", "events")):
            rows = arm[kind]
            out[kind] = {"items": len(rows),
                         "identical_to_first_arm": sum(rows[k][field] == first[kind][k][field] for k in rows),
                         "seconds_sum": round(sum(r["seconds"] for r in rows.values()), 2)}
        out["engine"]["items_with_events"] = sum(bool(r["events"]) for r in arm["engine"].values())
        summary[name] = out
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, action="append", required=True)
    parser.add_argument("--load", action="store_true",
                        help="Time on-demand model loading after a listen control, --runs per arm")
    parser.add_argument("--equality", action="store_true",
                        help="Compare Moonshine, Whisper and worker outputs per fixture (directories allowed)")
    parser.add_argument("--engine", action="store_true",
                        help="Time unpaced --engine requests for every fixture instead of a paced run")
    parser.add_argument("--arm", action="append", required=True, help="NAME=BINARY")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--gap", type=float, default=4.0)
    parser.add_argument("--idle", type=float, default=8.0, help="Whisper idle release seconds")
    parser.add_argument("--lead", type=float, default=3.0)
    parser.add_argument("--quiet", type=float, default=20.0, help="Silent seconds after the commands")
    parser.add_argument("--threads", type=int)
    parser.add_argument("--expect", help="Fail unless every command equals this text")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    arms = [tuple(a.split("=", 1)) for a in args.arm]
    if args.load:
        report = {"runs": args.runs, "arms": {name: [] for name, _ in arms}}
        for index in range(args.runs):
            for name, path in (arms if index % 2 == 0 else arms[::-1]):
                report["arms"][name].append(load_wall(Path(path), args.threads))
        for name, runs in report["arms"].items():
            report[f"{name}_summary"] = {key: round(statistics.median(r[key] for r in runs), 2)
                                         for key in ("load_wall_ms", "peak_rss_mib", "loaded_rss_mib")}
            print(name, json.dumps(report[f"{name}_summary"]), [r["load_wall_ms"] for r in runs])
        if args.output:
            args.output.write_text(json.dumps(report, indent=1) + "\n")
        return
    if args.equality:
        files = corpus(args.fixture)
        results = {name: outputs(Path(path), files, args.threads) for name, path in arms}
        report = {"files": len(files), "summary": equality(results)}
        print(json.dumps(report, indent=1))
        if args.output:
            args.output.write_text(json.dumps(report | {"results": results}, indent=1, ensure_ascii=False) + "\n")
        if any(v["identical_to_first_arm"] != v["items"] for arm in report["summary"].values() for v in arm.values()):
            raise SystemExit("Outputs differ between arms")
        return
    if args.engine:
        report = {"fixtures": {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in args.fixture},
                  "repeats": args.repeats, "runs": args.runs, "arms": {name: [] for name, _ in arms}}
        for index in range(args.runs):
            for name, path in (arms if index % 2 == 0 else arms[::-1]):
                result = engine(Path(path), args.fixture, args.repeats, args.threads)
                report["arms"][name].append(result)
                print(name, "load", round(result["load_seconds"], 3), "peak", result["peak_rss_mib"],
                      {k: v["median_ms"] for k, v in result["rows"].items()}, flush=True)
        first = None
        for name, runs in report["arms"].items():
            keys = runs[0]["rows"]
            summary = {k: round(statistics.median(r["rows"][k]["median_ms"] for r in runs), 2) for k in keys}
            events = [r["rows"][k]["events"] for r in runs for k in keys]
            first = first if first is not None else events[:len(keys)]
            report[f"{name}_summary"] = {"median_ms": summary,
                                         "total_median_ms": round(sum(summary.values()), 2),
                                         "events_identical_to_first_arm": all(
                                             e == first[i % len(keys)] for i, e in enumerate(events))}
            print(name, json.dumps(report[f"{name}_summary"]["total_median_ms"]),
                  "identical:", report[f"{name}_summary"]["events_identical_to_first_arm"])
        if args.output:
            args.output.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
        return
    if len(args.fixture) != 1:
        parser.error("a paced run takes one --fixture")
    args.fixture = args.fixture[0]
    report = {"fixture": str(args.fixture), "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
              "settings": {k: getattr(args, k) for k in ("runs", "repeats", "gap", "idle", "lead", "quiet", "threads")},
              "cpu": os.cpu_count(), "arms": {name: {"binary": str(path), "runs": []} for name, path in arms}}
    for index in range(args.runs):
        order = arms if index % 2 == 0 else arms[::-1]
        for name, path in order:
            result = run(Path(path), args.fixture, args.repeats, args.gap, args.idle, args.lead, args.quiet,
                         args.threads)
            report["arms"][name]["runs"].append(result)
            print(name, json.dumps({k: v for k, v in result.items() if k != "commands"}),
                  [(c["text"], c["latency_ms"]) for c in result["commands"]], flush=True)
            if args.expect and any(c["text"] != args.expect for c in result["commands"]):
                raise SystemExit(f"{name}: unexpected command {result['commands']}")
    for name, arm in report["arms"].items():
        arm["summary"] = summarize(arm["runs"])
        print(name, json.dumps(arm["summary"], ensure_ascii=False))
    if args.output:
        args.output.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
