#!/usr/bin/env python3
"""Replay an explicit local PCM corpus through the real Linux voice worker.

Never opens a microphone, downloads audio or dispatches desktop actions.
One worker per case; repeated segments within a case share that worker so a
background -> quiet -> wake test exercises decoder history. Missing corpus
categories, missed commands, wrong commands and extra wakes all fail the gate.
"""
from __future__ import annotations

import argparse
import array
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from murmur.commands import Parser
from murmur.config import validate
from murmur.native_worker import binary_path, prepare
from murmur.process_memory import snapshot

REQUIRED = {"command", "recorded_command", "confusable", "unrelated_speech", "music", "game_audio", "background_wake"}
CAPTURE = '''#!/usr/bin/env python3
import os, time
from pathlib import Path
while not Path(os.environ['JARVIS_ACCEPT_GO']).exists():
    time.sleep(.01)
audio = Path(os.environ['JARVIS_ACCEPT_PCM']).read_bytes()
for offset in range(0, len(audio), 960):
    os.write(1, audio[offset:offset+960])
    time.sleep(.03)
Path(os.environ['JARVIS_ACCEPT_DONE']).touch()
while True:
    os.write(1, bytes(960))
    time.sleep(.03)
'''


def audio_case(case, base):
    repeat = case.get("repeat", 1)
    if type(repeat) is not int or not 1 <= repeat <= 100:
        raise ValueError("Case repeat must be 1–100")
    chunks, clips = [], []
    for segment in case["segments"]:
        if ("silence" in segment) == ("pcm" in segment):
            raise ValueError("Each segment needs exactly one of pcm or silence")
        if "silence" in segment:
            seconds = segment["silence"]
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 60:
                raise ValueError("Silence must be 0–60 finite seconds")
            pcm = bytes(960) * round(seconds / .03)
        else:
            path = base / segment["pcm"]
            if not 6400 <= path.stat().st_size <= 32000 * 60 or path.stat().st_size % 2:
                raise ValueError("Clips must be 0.2–60 seconds of s16le 16 kHz mono PCM")
            pcm = path.read_bytes()
            clips.append({"sha256": hashlib.sha256(pcm).hexdigest(), "seconds": len(pcm) / 32000,
                          "reversed": bool(segment.get("reverse"))})
            if segment.get("reverse"):
                samples = array.array("h", pcm)
                samples.reverse()
                pcm = samples.tobytes()
            pcm += bytes((-len(pcm)) % 960)
        chunks.append(pcm)
    length = sum(map(len, chunks)) * repeat
    if not 6400 <= length <= 32000 * 600:
        raise ValueError("Each complete case must be 0.2–600 seconds")
    return b"".join(chunks) * repeat, clips


def evaluate(events, expected, expected_wakes):
    actual = [event["text"] for event in events if event.get("event") == "command"]
    wakes = sum(event.get("event") == "wake" for event in events)
    false_actions = sum(index >= len(expected) or text != expected[index] for index, text in enumerate(actual))
    return {"expected_commands": expected, "actual_commands": actual,
            "expected_wakes": expected_wakes, "actual_wakes": wakes,
            "false_actions": false_actions, "extra_wakes": max(0, wakes - expected_wakes),
            "passed": actual == expected and wakes == expected_wakes
                      and not any(event.get("event") == "error" for event in events)}


def run_case(binary, payload, env, audio):
    events, memory, failure = [], [], None
    with tempfile.TemporaryDirectory(prefix="jarvis-accept-") as directory:
        root = Path(directory)
        capture = root / "pw-record"
        capture.write_text(CAPTURE)
        capture.chmod(0o700)
        pcm = root / "input.raw"
        tail = float(payload["config"]["activation"]["command_timeout"]) + 3
        pcm.write_bytes(bytes(960) * 20 + audio + bytes(960) * math.ceil(tail / .03))
        done = root / "done"
        go = root / "go"
        env = dict(env, PATH=str(root) + ":" + env.get("PATH", ""),
                   JARVIS_ACCEPT_PCM=str(pcm), JARVIS_ACCEPT_GO=str(go), JARVIS_ACCEPT_DONE=str(done))
        started = time.monotonic()
        process = subprocess.Popen([str(binary.resolve())], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, bufsize=0)
        try:
            process.stdin.write((json.dumps(payload) + "\n").encode())
            process.stdin.flush()
            limit = started + 30 + pcm.stat().st_size / 32000
            pending = b""
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while not done.exists():
                    if time.monotonic() > limit:
                        raise RuntimeError("Worker/capture timed out")
                    if process.poll() is not None:
                        raise RuntimeError("Worker exited before playback completed")
                    memory.append({"elapsed_seconds": round(time.monotonic() - started, 3),
                                   "memory": snapshot([process.pid])})
                    if not selector.select(.25):
                        continue
                    pending += os.read(process.stdout.fileno(), 65536)
                    while b"\n" in pending:
                        raw, pending = pending.split(b"\n", 1)
                        event = json.loads(raw)
                        if event.get("event") == "ready":
                            go.touch()
                        if event.get("event") in ("ready", "wake", "command", "timeout", "error"):
                            events.append(event)
                        if event.get("event") == "error":
                            raise RuntimeError(event.get("message", "Worker error"))
                # Level events continue indefinitely, so draining needs a deadline.
                drain_until = time.monotonic() + .5
                while time.monotonic() < drain_until and selector.select(.1):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    pending += chunk
                    while b"\n" in pending:
                        raw, pending = pending.split(b"\n", 1)
                        event = json.loads(raw)
                        if event.get("event") in ("wake", "command", "timeout", "error"):
                            events.append(event)
        except (OSError, ValueError, RuntimeError) as exc:
            failure = str(exc)
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
    return {"events": events, "error": failure, "samples": memory}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=binary_path())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vosk-fast-path", action="store_true", help="Exercise the experimental Rust fast path (off by default)")
    parser.add_argument("--finalize-vosk-utterances", action="store_true", help="Exercise experimental decoder finalization (off by default)")
    parser.add_argument("--whisper-model-dir", type=Path, help="Explicit local experimental Whisper export; never installed or selected globally")
    parser.add_argument("--whisper-model-name", choices=("tiny.en", "base.en", "small.en"), default="small.en")
    args = parser.parse_args()
    if sys.platform != "linux" or not args.binary or not args.binary.is_file():
        parser.error("Supply a built Linux voice worker; Windows corpus playback is not implemented")
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    config = validate(manifest.get("config", {}))
    config["privacy"]["onboarded"] = True  # only our fake microphone is available to this child
    grammar = Parser(manifest.get("custom_phrases", {})).grammar(manifest.get("apps", ["firefox", "steam"]))
    with patch.dict(os.environ, MURMUR_VOICE_WORKER="rust"):
        _, payload, env = prepare(config, "wake_word", grammar)
    payload["native"]["vosk_fast_path"] = args.vosk_fast_path
    payload["native"]["vosk_finalize_utterances"] = args.finalize_vosk_utterances
    if args.whisper_model_dir:
        if not config["recognition"]["accurate"]:
            parser.error("The explicit Whisper experiment requires recognition.accurate")
        files = {key: str((args.whisper_model_dir / f"{args.whisper_model_name}-{suffix}").resolve())
                 for key, suffix in (("encoder", "encoder.int8.onnx"), ("decoder", "decoder.int8.onnx"), ("tokens", "tokens.txt"))}
        if not all(Path(p).is_file() for p in files.values()):
            parser.error("The explicit model directory is missing encoder, decoder or tokens")
        payload["native"]["whisper"] = files
    elif args.whisper_model_name != "small.en":
        parser.error("--whisper-model-name requires --whisper-model-dir")
    cases = manifest["cases"]
    if not cases or len({case["name"] for case in cases}) != len(cases):
        parser.error("Supply cases with unique names")
    # Validate the whole corpus before starting any workers.
    inputs = []
    for case in cases:
        if case["category"] not in REQUIRED:
            parser.error("Unknown corpus category: " + case["category"])
        if not isinstance(case["expected_commands"], list) or not all(isinstance(s, str) for s in case["expected_commands"]):
            parser.error("expected_commands must be a list of strings")
        if type(case["expected_wakes"]) is not int or case["expected_wakes"] < 0:
            parser.error("expected_wakes must be a nonnegative integer")
        inputs.append(audio_case(case, args.manifest.resolve().parent))
    result = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
              "manifest_sha256": hashlib.sha256(raw).hexdigest(),
              "manifest": manifest,
              "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
              "experimental_vosk_fast_path": args.vosk_fast_path,
              "experimental_vosk_finalize_utterances": args.finalize_vosk_utterances,
              "experimental_whisper_model": args.whisper_model_name if args.whisper_model_dir else None,
              "scope": "real worker and paced fake capture; no microphone or desktop actions; fixture categories are caller-supplied",
              "missing_categories": sorted(REQUIRED - {case["category"] for case in cases}), "cases": [], "passed": False}
    for case, (audio, clips) in zip(cases, inputs):
        measured = run_case(args.binary, payload, env, audio)
        outcome = evaluate(measured["events"], case["expected_commands"], case["expected_wakes"])
        outcome["passed"] = outcome["passed"] and measured["error"] is None
        result["cases"].append({"name": case["name"], "category": case["category"], "clips": clips,
                                **outcome, **measured})
        result["false_actions"] = sum(row["false_actions"] for row in result["cases"])
        result["passed"] = (len(result["cases"]) == len(cases) and not result["missing_categories"]
                            and all(row["passed"] for row in result["cases"]))
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"name": case["name"], **outcome, "error": measured["error"]}), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
