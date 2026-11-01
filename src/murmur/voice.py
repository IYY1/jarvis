"""Disposable Linux microphone worker. Only this process imports the speech model.

Windows uses the Rust worker (native/voice-worker) with WASAPI capture.
"""
from __future__ import annotations

from collections import deque
import ctypes
import json
import functools
import math
import operator
import os
import selectors
import signal
import struct
import subprocess
import sys
import threading
import time


def emit(**message):
    print(json.dumps(message), flush=True)


def _parent_death(sig=signal.SIGTERM, expected_parent=None):
    # Linux: microphone children must die if their owning service/worker dies.
    before = os.getppid()
    if before == 1 or (expected_parent is not None and before != expected_parent):
        os.kill(os.getpid(), sig)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, sig, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot bind microphone worker to parent lifetime")
    if os.getppid() != before:
        os.kill(os.getpid(), sig)


def _capture_child(expected_parent):
    _parent_death(signal.SIGKILL, expected_parent)


# Exact integer sum of squares; math.sumprod (Python 3.12+) runs it in C, about 2x faster per frame.
_sum_of_products = getattr(math, "sumprod", lambda a, b: sum(map(operator.mul, a, b)))


def rms(pcm: bytes) -> float:
    samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
    return math.sqrt(_sum_of_products(samples, samples) / max(1, len(samples))) / 32768


class NoiseFloor:
    """Adaptive speech gate: follows the room's background level.

    The floor drops quickly when it gets quiet and rises slowly (about 20 s)
    under steady background sound such as a fan, game music or a TV. Speech
    must be about 3x (≈10 dB) louder than that floor, and never quieter than
    the configured minimum. A nearby voice clears it; a distant one doesn't.
    """
    RATIO = 3.0
    CEILING = 0.08   # never require more than this, so loud rooms can't lock you out

    def __init__(self, minimum: float, adaptive: bool = True):
        self.minimum, self.adaptive = minimum, adaptive
        self.floor: float | None = None

    @property
    def gate(self) -> float:
        if not self.adaptive or self.floor is None:
            return self.minimum
        return max(self.minimum, min(self.CEILING, self.floor * self.RATIO))

    def update(self, energy: float) -> None:
        if self.floor is None:
            self.floor = energy
        else:
            rate = 0.05 if energy < self.floor else 0.0015
            self.floor += (energy - self.floor) * rate


def make_engine(config: dict, grammar: dict | list[str]):
    """Vosk when its package and model are installed (better with accents),
    otherwise PocketSphinx. ``recognition.backend`` can force either one."""
    import importlib.util
    from pathlib import Path
    from .models import default_model_path, valid_model
    settings = config.get("recognition", {})
    backend = settings.get("backend", "auto")
    activation = config["activation"]
    path = Path(settings.get("model_path") or default_model_path()).expanduser()
    vosk_ready = importlib.util.find_spec("vosk") is not None and valid_model(path)
    if backend == "vosk" or (backend == "auto" and vosk_ready):
        from .vosk_engine import VoskEngine
        transcriber = None
        if settings.get("accurate", True):
            from . import transcriber as accurate
            from .models import selected_model
            if accurate.available(selected_model(config)):
                emit(event="loading", message="Loading the accurate voice model…")
                transcriber = accurate.Transcriber(selected_model(config), denoise=settings.get("noise_suppression", False))
        engine = VoskEngine(grammar, activation["wake_phrase"], activation["sensitivity"], path,
                            fuzzy=settings.get("closest_match", True), transcriber=transcriber, wake_aliases=activation.get("wake_aliases", []))
        engine.name = "vosk+" + transcriber.name if transcriber else "vosk"
        return engine
    from .speech import SpeechEngine
    return SpeechEngine(grammar, activation["wake_phrase"], activation["sensitivity"], wake_aliases=activation.get("wake_aliases", []))


def run(config: dict, mode: str, grammar: dict | list[str]):
    capture = None
    engine = None
    if sys.platform == "win32":
        emit(event="error", message="Windows uses the bundled Rust voice worker; reinstall Jarvis if it is missing")
        return
    selector = selectors.DefaultSelector()
    errors = deque(maxlen=4)
    def shutdown(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    _parent_death(expected_parent=int(os.environ["MURMUR_OWNER_PID"]) if "MURMUR_OWNER_PID" in os.environ else None)
    try:
        if not config["privacy"]["onboarded"]:
            raise RuntimeError("Complete microphone consent in Settings first")
        if mode != "test":
            emit(event="loading", message="Loading local voice model…")
            engine = make_engine(config, grammar)
            frame_bytes = engine.frame_bytes
        else:
            frame_bytes = 960
        # Native decoders own the compiled grammar; initialization scaffolding
        # need not stay resident while this worker waits for microphone frames.
        del grammar
        argv = ["pw-record", "--raw", "--rate", "16000", "--channels", "1", "--format", "s16", "--latency", "30ms"]
        if config["audio"]["device"]:
            argv += ["--target", config["audio"]["device"]]
        argv += ["-"]
        # preexec_fn is only safe because no thread exists yet in this process;
        # keep this spawn before read_errors() starts.
        capture = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, preexec_fn=functools.partial(_capture_child, os.getpid()))
        def read_errors():
            while chunk := capture.stderr.read(512):
                errors.append(chunk)
        threading.Thread(target=read_errors, daemon=True).start()
        audio = selector.register(capture.stdout, selectors.EVENT_READ)
        # A wake worker keeps stdin open. Only after "ready" does the service
        # write "listen" or "cancel" lines (so main()'s buffered payload read
        # holds none); they start or drop a command session without a reload.
        control = selector.register(0, selectors.EVENT_READ) if mode == "wake_word" else None
        partial = b""   # an unfinished control line
        listening = mode == "once"
        deadline = time.monotonic() + (15 if mode == "test" else config["activation"]["command_timeout"]) if mode in ("test", "once") else None
        if engine:
            engine.begin_command() if listening else engine.begin_wake()
        ready = functools.partial(emit, event="ready", unsupported=engine.unsupported_phrases if engine else [],
                                  engine=getattr(engine, "name", "pocketsphinx") if engine else None)
        ready(listening=listening, deadline=deadline)
        pending = bytearray()  # one 30 ms frame maximum, plus kernel pipe buffer
        noise = NoiseFloor(config["audio"]["vad_rms"], config["audio"].get("adaptive_gate", True))
        next_level = 0
        cooldown_until = 0
        settling = False
        quiet_frames = 0
        last_frame_at = time.monotonic()
        while True:
            now = time.monotonic()
            if now - last_frame_at >= 3:
                raise RuntimeError("No microphone frames for three seconds. Check the input device and audio permissions, then Resume.")
            if deadline is not None and now >= deadline:
                # Background speech can keep a command from "ending"; decide on
                # what was said so far instead of throwing it away.
                late = engine.flush() if listening and engine and hasattr(engine, "flush") else None
                # Time spent decoding isn't a stalled microphone. Capture was
                # closed at the deadline, before this synchronous inference.
                last_frame_at = time.monotonic()
                if late is not None:
                    emit(event="command", captured_at=deadline, **late.to_dict())
                    if mode == "once":
                        return
                    listening, deadline, settling = False, None, False
                    engine.begin_wake()
                    cooldown_until = time.monotonic() + 1.5
                    continue
                if getattr(engine, "unmatched", None):
                    emit(event="heard", text=engine.unmatched[:120])
                    engine.unmatched = None
                if mode in ("test", "once"):
                    emit(event="timeout")
                    return
                listening, deadline = False, None
                settling = False
                quiet_frames = 0
                engine.begin_wake()
                emit(event="timeout")
                now = time.monotonic()
            next_deadline = min(last_frame_at + 3, deadline) if deadline is not None else last_frame_at + 3
            events = selector.select(max(0, next_deadline - now))
            if not events:
                continue
            # Handle control first regardless of selector order; a continuously
            # readable microphone must not starve listen/cancel acknowledgements.
            if control is not None and any(key is control for key, _mask in events):
                data = os.read(0, 4096)
                if not data:   # the service closed the channel; keep waiting for the wake word
                    selector.unregister(0)
                    control = None
                *commands, partial = (partial + data).split(b"\n")
                for command in commands:   # each one is answered with "ready"
                    listening = command == b"listen"
                    deadline = time.monotonic() + config["activation"]["command_timeout"] if listening else None
                    settling, quiet_frames, cooldown_until = False, 0, 0
                    engine.begin_command() if listening else engine.begin_wake()
                    ready(listening=listening, deadline=deadline)
                if not any(key is audio for key, _mask in events):
                    continue
            data = os.read(audio.fd, frame_bytes - len(pending))
            if not data:
                capture.wait(timeout=2)
                detail = b"".join(errors).decode(errors="replace").strip()[-800:]
                raise RuntimeError("Microphone stream ended. Check audio permissions and the selected input. " + detail)
            pending.extend(data)
            if len(pending) != frame_bytes:
                continue
            pcm = bytes(pending)
            pending.clear()
            last_frame_at = time.monotonic()
            if mode == "test":
                if now >= next_level:
                    level = rms(pcm)
                    noise.update(level)
                    emit(event="level", level=level, gate=noise.gate)
                    next_level = now + 0.2  # UI meters only on explicit, bounded input test
                continue
            if now < cooldown_until:
                continue
            # Cheap configurable energy gate before the native VAD; feed silence
            # rather than skip frames so endpoint timing remains correct.
            energy = rms(pcm)
            gate = noise.gate
            noise.update(energy)
            if settling:
                quiet_frames = quiet_frames + 1 if energy < gate else 0
                if quiet_frames >= 7:  # 210 ms pause separates wake tail from command
                    settling = False
                    engine.begin_command()
                continue
            quiet = energy < gate
            captured_at = time.monotonic()
            if listening and deadline is not None and captured_at >= deadline:
                # select/read may finish on the boundary. Let the next loop
                # flush only audio captured before it; don't admit a late frame.
                continue
            if getattr(engine, "wants_raw_audio", False):
                result = engine.feed(pcm, quiet=quiet)   # Vosk keeps a pre-roll of quiet audio
            else:
                result = engine.feed(bytes(frame_bytes) if quiet else pcm)
            last_frame_at = time.monotonic()
            unmatched = getattr(engine, "unmatched", None)
            if unmatched:
                engine.unmatched = None
                if result is None or result.kind != "wake":
                    emit(event="heard", text=unmatched[:120])   # shown in the status; nothing runs
            if result is None:
                continue
            if result.kind == "wake":
                listening = True
                deadline = time.monotonic() + config["activation"]["command_timeout"]
                settling, quiet_frames = True, 0
                emit(event="wake", deadline=deadline)
                follow_up = engine.after_wake() if hasattr(engine, "after_wake") else None
                if follow_up is None:
                    if unmatched:   # words after "Jarvis" that matched no command
                        emit(event="heard", text=unmatched[:120])
                    continue
                # "hey jarvis open firefox" in one breath: run it now.
                settling = False
                captured_at = time.monotonic()
                result = follow_up
            if result.kind == "command":
                emit(event="command", captured_at=captured_at, **result.to_dict())
                if mode == "once":
                    return
                listening, deadline = False, None
                engine.begin_wake()
                cooldown_until = time.monotonic() + 1.5
    except Exception as exc:
        emit(event="error", message=str(exc))
    finally:
        selector.close()
        if engine:
            engine.close()
        if capture:
            capture.terminate()
            try:
                capture.wait(timeout=2)
            except subprocess.TimeoutExpired:
                capture.kill()
                capture.wait()
            if capture.stdout:
                capture.stdout.close()
            if capture.stderr:
                capture.stderr.close()


def main():
    try:
        line = sys.stdin.buffer.readline(1048577)
        if len(line) > 1048576:
            raise ValueError("Voice configuration exceeds 1 MiB")
        payload = json.loads(line)
        del line
        # Transfer arguments out of the payload so the caller does not retain
        # a second owner of the source grammar throughout the worker loop.
        run(payload.pop("config"), payload.pop("mode"),
            payload.pop("grammar", payload.pop("phrases", None)))
    except Exception as exc:
        emit(event="error", message="Voice worker configuration: " + str(exc))


if __name__ == "__main__":
    main()
