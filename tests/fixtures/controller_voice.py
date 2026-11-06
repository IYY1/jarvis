#!/usr/bin/python3
"""Owned fake voice process for controller tests; never opens an audio device."""
import json
import os
import sys
import time


def log(event, **fields):
    fd = os.open(os.environ["JARVIS_TEST_LOG"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps({"program": "voice", "event": event, "pid": os.getpid(), **fields}) + "\n").encode())
    finally:
        os.close(fd)


def emit(**event):
    print(json.dumps(event), flush=True)


payload = json.loads(sys.stdin.readline())
log("start", payload=payload)
scenario = os.environ.get("JARVIS_TEST_VOICE", "normal")
if scenario == "delayed":
    time.sleep(1)
if scenario == "invalid":
    print("invalid json", flush=True)
    time.sleep(10)
if scenario == "oversized":
    print("x" * 65537, flush=True)
    time.sleep(10)
engine = None if payload["mode"] == "test" else "vosk+whisper-small"
emit(event="ready", engine=engine, unsupported=[], listening=payload["mode"] == "once", deadline=time.monotonic() + 8)
if scenario == "exit":
    sys.exit(1)
if payload["mode"] == "once" and scenario == "command":
    emit(event="command", text="go to workspace three", confidence=1, captured_at=time.monotonic())
for line in sys.stdin:
    control = line.strip()
    log("control", line=control)
    if scenario == "stale" and control == "listen":
        emit(event="command", text="go to workspace ten", confidence=1, captured_at=time.monotonic())
    emit(event="ready", engine=engine, unsupported=[], listening=control == "listen", deadline=time.monotonic() + 8)
    if scenario in ("command", "stale") and control == "listen":
        emit(event="command", text="go to workspace three", confidence=1, captured_at=time.monotonic())
if scenario == "linger":
    # Outlive the closed control pipe, so only the parent-death signal ends it.
    while True:
        time.sleep(1)
