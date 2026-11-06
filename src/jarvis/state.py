"""Pure activation state machine with bounded sessions and deduplication."""
from __future__ import annotations

import math
import time

from .commands import normalize


class Activation:
    # Inference may finish after the capture window, especially during a game.
    # A completed recording is still bounded; an arbitrarily stale result isn't.
    MAX_RESULT_DELAY = 15.0

    def __init__(self, timeout=8.0, confidence=0.65, clock=time.monotonic):
        self.state = "paused"
        self.deadline = None
        self.started_at = None
        self.timeout = timeout
        self.confidence = confidence
        self.clock = clock
        self.last = None
        self.last_at = -100.0

    def arm(self):
        self.state, self.deadline, self.started_at = "idle", None, None

    def listen(self, deadline=None):
        if self.state not in ("idle", "paused", "error"):
            return False
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise ValueError("Invalid capture deadline")
        self.state = "listening"
        self.deadline = self.clock() + self.timeout if deadline is None else deadline
        self.started_at = self.deadline - self.timeout
        return True

    def pause(self):
        self.state, self.deadline, self.started_at = "paused", None, None

    def error(self):
        self.state, self.deadline, self.started_at = "error", None, None

    def timed_out(self):
        return self.state == "listening" and self.deadline is not None and self.clock() >= self.deadline

    def accept(self, text: str, confidence: float | None, *, captured_at=None):
        if self.state != "listening":
            return False
        now = self.clock()
        if captured_at is None:
            if self.timed_out():
                return False
        elif (type(captured_at) not in (int, float) or not math.isfinite(captured_at)
              or not self.started_at <= captured_at <= self.deadline
              or not 0 <= now - captured_at <= self.MAX_RESULT_DELAY):
            return False
        if confidence is None or not self.confidence <= confidence <= 1:
            return False
        phrase = normalize(text)
        if not phrase or (phrase == self.last and now - self.last_at < 1.5):
            return False
        self.last, self.last_at = phrase, now
        self.state, self.deadline, self.started_at = "processing", None, None
        return True
