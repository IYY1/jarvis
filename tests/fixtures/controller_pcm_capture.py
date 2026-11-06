#!/usr/bin/python3
"""Replay explicitly supplied public PCM as a paced fake microphone."""
import os
from pathlib import Path
import time

audio = Path(os.environ["JARVIS_TEST_PCM"]).read_bytes()
audio += bytes((-len(audio)) % 960)


def feed(pcm):
    for start in range(0, len(pcm), 960):
        os.write(1, pcm[start:start + 960])
        time.sleep(.03)


feed(bytes(960 * 20))
feed(audio)
while True:
    feed(bytes(960 * 30))
