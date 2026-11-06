#!/usr/bin/env python3
"""Repeatable isolated engine probes; no microphone and no capture/GUI costs.

Examples (run each as a new process for an independent peak RSS):
  .venv/bin/python scripts/benchmark_speech.py --case silence --seconds 30
  .venv/bin/python scripts/benchmark_speech.py --case active --fixture /tmp/goforward.raw
  .venv/bin/python scripts/benchmark_speech.py --case vosk --fixture /tmp/goforward.raw --vosk-model /tmp/vosk-model-small-en-us-0.15

The optional fixture must be mono 16 kHz signed 16-bit little-endian raw PCM.
Use the linked upstream fixture from docs/speech-research.md, not private audio.
"""
from __future__ import annotations

import argparse
import array
import json
import pathlib
import platform
import random
import resource
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))


def metadata() -> dict:
    cpu = next((line.split(':', 1)[1].strip() for line in pathlib.Path('/proc/cpuinfo').read_text().splitlines()
                if line.startswith('model name')), 'unknown')
    return {'python': platform.python_version(), 'platform': platform.platform(), 'cpu': cpu,
            'maxrss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def fixture(path: pathlib.Path | None) -> bytes:
    if path is None:
        raise ValueError('Provide --fixture, mono 16 kHz signed 16-bit raw PCM')
    if path.stat().st_size > 16000 * 2 * 15:
        raise ValueError('Fixture must contain at most 15 seconds of PCM samples')
    data = path.read_bytes()
    if len(data) % 2:
        raise ValueError('Fixture must contain at most 15 seconds of complete 16-bit PCM samples')
    return data


def probe(args) -> dict:
    from jarvis.speech import SpeechEngine
    start = time.perf_counter()
    engine = SpeechEngine(['go forward ten meters', 'open firefox', 'turn the volume down'])
    load_ms = (time.perf_counter() - start) * 1000
    events = []
    if args.case == 'active':
        data = fixture(args.fixture)
        pcm = data + bytes(32000)
        durations = []
        for _ in range(args.repeats):
            engine.begin_command()
            start = time.perf_counter()
            for offset in range(0, len(pcm) - engine.frame_bytes + 1, engine.frame_bytes):
                result = engine.feed(pcm[offset:offset + engine.frame_bytes])
                if result:
                    events.append(result.to_dict())
            durations.append((time.perf_counter() - start) * 1000)
        return {'load_ms': load_ms, 'median_decode_ms': statistics.median(durations),
                'p95_decode_ms': sorted(durations)[max(0, int(len(durations) * .95) - 1)],
                'fixture_seconds': len(data) / 32000, 'appended_silence_seconds': 1,
                'matched': len(events), 'repetitions': args.repeats,
                'last_result': events[-1] if events else None}
    engine.begin_wake()
    if args.case == 'white_noise':
        rng = random.Random(42)
        pcm = array.array('h', (rng.randint(-1000, 1000) for _ in range(engine.frame_bytes // 2))).tobytes()
    else:
        pcm = bytes(engine.frame_bytes)
    start, cpu, frames = time.monotonic(), time.process_time(), 0
    while time.monotonic() - start < args.seconds:
        result = engine.feed(pcm)
        frames += 1
        if result:
            events.append(result.to_dict())
            engine.begin_wake()
        time.sleep(max(0, start + frames * engine.frame_seconds - time.monotonic()))
    wall, used = time.monotonic() - start, time.process_time() - cpu
    return {'load_ms': load_ms, 'wall_seconds': wall, 'cpu_seconds': used,
            'cpu_percent_one_logical_core': used / wall * 100,
            'frames': frames, 'events': events}


def vosk_probe(args) -> dict:
    if not args.vosk_model:
        raise ValueError('Provide --vosk-model pointing to an already downloaded model')
    start = time.perf_counter()
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)
    model = Model(str(args.vosk_model))  # explicit path; never automatic download
    load_ms = (time.perf_counter() - start) * 1000
    data = fixture(args.fixture)
    pcm = data + bytes(32000)
    results = {}
    for name, grammar in [('constrained', json.dumps(['go forward ten meters', 'open firefox',
                                                     'turn the volume down', '[unk]'])),
                          ('general', None)]:
        durations, decoded = [], []
        for _ in range(args.repeats):
            start = time.perf_counter()
            recognizer = KaldiRecognizer(model, 16000, grammar) if grammar else KaldiRecognizer(model, 16000)
            recognizer.SetWords(True)
            for offset in range(0, len(pcm), 960):
                if recognizer.AcceptWaveform(pcm[offset:offset + 960]):
                    decoded.append(json.loads(recognizer.Result()))
            decoded.append(json.loads(recognizer.FinalResult()))
            durations.append((time.perf_counter() - start) * 1000)
        results[name] = {'median_decode_ms': statistics.median(durations),
                         'max_decode_ms': max(durations),
                         'first_result': next((item for item in decoded if item.get('text')), {})}
    return {'load_ms_including_import': load_ms, 'fixture_seconds': len(data) / 32000,
            'repetitions': args.repeats, 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=['silence', 'white_noise', 'active', 'vosk'], default='silence')
    parser.add_argument('--seconds', type=float, default=15)
    parser.add_argument('--repeats', type=int, default=20)
    parser.add_argument('--fixture', type=pathlib.Path)
    parser.add_argument('--vosk-model', type=pathlib.Path)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600 or not 1 <= args.repeats <= 1000:
        parser.error('seconds must be 1–3600 and repeats 1–1000')
    result = vosk_probe(args) if args.case == 'vosk' else probe(args)
    result.update(metadata())
    result.update({'case': args.case, 'scope': 'isolated engine; excludes microphone, service IPC, GUI, video capture'})
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
