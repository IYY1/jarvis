#!/usr/bin/env python3
"""Measure command policy in fresh processes, without a desktop or audio input.

Compare checkouts with --source /path/to/checkout/src. Each case reports wall
and CPU milliseconds per call, peak RSS, transient Python allocations, and a
result digest. RSS includes imports and fixture setup; allocations exclude
setup. Timings run without tracemalloc. These are policy microbenchmarks, not
speech latency, whole-app RAM, or gaming performance measurements.
"""
import argparse
import dataclasses
import gc
import hashlib
import json
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
ITERATIONS = {"parse_brightness": 300, "parse_invalid_brightness": 300,
              "parse_application": 1000, "match_brightness": 100,
              "match_fuzzy": 50, "candidates": 3, "validate": 100,
              "running_windows": 1000, "merge": 1000, "transcribe_input": 500,
              "transcribe_input_10s": 50,
              "app_catalog_100": 30, "app_catalog_1000": 10}


def workload(source, case):
    sys.path.insert(0, str(source))
    from murmur import config
    from murmur.apps import App
    from murmur.commands import Parser, closest_match, match_candidates
    from murmur.hyprland import Hyprland

    if case.startswith("app_catalog_"):
        from murmur.apps import AppRegistry, id_stem
        from murmur.commands import normalize, spoken_numbers
        count = int(case.removeprefix("app_catalog_"))
        # Model the in-memory registry after scanning desktop entries. File I/O
        # and discovery are outside this catalog-generation measurement.
        registry = AppRegistry.__new__(AppRegistry)
        registry.aliases = {}
        registry.apps = {
            f"launcher-{n}.desktop": App(f"launcher-{n}.desktop", f"Program {n}",
                                        f"/fixture/launcher-{n}.desktop", f"launcher-{n}")
            for n in range(count)}
        registry.index = {}
        for app in registry.apps.values():
            names = set()
            for name in (app.name, id_stem(app.id)):
                normalized = normalize(name)
                names.update((normalized, spoken_numbers(normalized),
                              normalized.removeprefix("the "),
                              spoken_numbers(normalized.removeprefix("the "))))
            for name in names:
                registry.index.setdefault(name, []).append(app)
        registry._register_fallbacks()
        parser = Parser()
        return lambda: registry.voice_commands(parser)

    if case in ("transcribe_input", "transcribe_input_10s"):
        from types import SimpleNamespace
        from murmur.transcriber import Transcriber
        class Stream:
            result = SimpleNamespace(text="fixture")
            def accept_waveform(self, rate, samples):
                self.samples = samples
        engine = Transcriber.__new__(Transcriber)
        engine._recognizer = SimpleNamespace(create_stream=Stream, decode_stream=lambda stream: None)
        engine._denoiser = None
        engine._memory_efficient = False
        pcm = b"\x01\x00" * (160000 if case.endswith("10s") else 16000)
        return lambda: engine.transcribe(pcm)

    settings = {"aliases": {f"app {n}": f"app-{n}.desktop" for n in range(128)},
                "app_prefixes": {f"app {n}": "watch" for n in range(128)},
                "brightness": {"monitors": {
                    f"edid:fixture:{n}": {"name": f"screen {word}", "aliases": []}
                    for n, word in enumerate("one two three four five six seven eight".split())},
                    "default": "edid:fixture:0", "step": 10}}
    current = config.validate(settings)
    parser = Parser.from_config(current)
    if case == "validate":
        return lambda: config.validate(settings)
    if case == "merge":
        return lambda: config.merged(current, {"activation": {"sensitivity": 0.7},
                                               "brightness": {"step": 20}})
    if case == "running_windows":
        app = App("game.desktop", "Game", "/fixture/game.desktop", "steam steam://rungameid/42")
        clients = [{"class": "other" if n % 2 else "steam_app_42", "focusHistoryID": n}
                   for n in range(100)]
        desktop = Hyprland()
        desktop.query = lambda _name: clients
        return lambda: desktop.running(app)
    if case == "parse_application":
        return lambda: dataclasses.asdict(parser.parse("please watch the app 127"))
    if case == "parse_brightness":
        return lambda: dataclasses.asdict(parser.parse("set screen eight brightness to 67 percent"))
    if case == "parse_invalid_brightness":
        def invalid():
            try:
                parser.parse("set unknown brightness to 67 percent")
            except ValueError:
                return None
            raise AssertionError("An unknown monitor was accepted")
        return invalid
    grammar = parser.grammar(["firefox", "discord", "deadlock", "steam", "stremio"])
    if case == "candidates":
        return lambda: match_candidates(grammar)
    candidates = match_candidates(grammar)
    text = "set screen eight brightness to 67 percent" if case == "match_brightness" else "open fire fox"
    return lambda: closest_match(text, candidates)


def sample(source, case):
    run = workload(source, case)
    expected = run()  # warm code and imports before timing
    gc.collect()
    count = ITERATIONS[case]
    cpu = time.process_time()
    wall = time.perf_counter()
    for _ in range(count):
        run()
    wall = (time.perf_counter() - wall) * 1000 / count
    cpu = (time.process_time() - cpu) * 1000 / count
    gc.collect()
    tracemalloc.start()
    run()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    return {"case": case, "iterations": count, "wall_ms": wall, "cpu_ms": cpu,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "peak_python_bytes": peak, "result_sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "src")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=ITERATIONS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not (args.source / "murmur/commands.py").is_file() or not 1 <= args.repeats <= 10:
        parser.error("Supply a Python source directory and 1–10 repeats")
    if args.case:
        print(json.dumps(sample(args.source.resolve(), args.case)))
        return
    samples = []
    for _ in range(args.repeats):
        for case in ITERATIONS:
            out = subprocess.check_output([sys.executable, str(Path(__file__).resolve()),
                                           "--source", str(args.source.resolve()), "--case", case], text=True)
            samples.append(json.loads(out))
    medians = {}
    for case in ITERATIONS:
        selected = [s for s in samples if s["case"] == case]
        assert len({s["result_sha256"] for s in selected}) == 1
        medians[case] = {key: statistics.median(s[key] for s in selected)
                         for key in ("wall_ms", "cpu_ms", "peak_rss_mib", "peak_python_bytes")}
        medians[case]["result_sha256"] = selected[0]["result_sha256"]
    result = {"platform": platform.platform(), "python": sys.version, "source": str(args.source.resolve()),
              "repeats": args.repeats, "scope": __doc__, "medians": medians, "samples": samples}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
