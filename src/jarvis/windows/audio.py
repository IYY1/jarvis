"""WASAPI microphone and system loopback; streams exist only during explicit use."""

from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
import queue
import sys
import threading
import time

READY = "ready"   # the first line a capture audio worker prints


@contextmanager
def output_closed(output):
    try:
        yield output
    finally:
        output.close()


def _library():
    try:
        import pyaudiowpatch as pa

        return pa
    except ImportError as exc:
        raise RuntimeError(
            "The Windows audio component is missing; reinstall the complete Jarvis package"
        ) from exc


def _audioop():
    # CPython 3.12 ships audioop. Newer Python uses the maintained audioop-lts wheel.
    import audioop

    return audioop


def _device_id(info):
    return ("loopback:" if info.get("isLoopbackDevice") else "input:") + str(
        info["name"]
    )


def devices():
    pa = _library()
    with pa.PyAudio() as engine:
        wasapi = engine.get_host_api_info_by_type(pa.paWASAPI)
        result = [{"id": "", "name": "Default Windows microphone"}]
        for index in range(engine.get_device_count()):
            info = engine.get_device_info_by_index(index)
            if (
                info["hostApi"] == wasapi["index"]
                and info["maxInputChannels"] > 0
                and not info.get("isLoopbackDevice")
            ):
                result.append({"id": _device_id(info), "name": info["name"]})
        return result


def recording_sources():
    pa = _library()
    with pa.PyAudio() as engine:
        wasapi = engine.get_host_api_info_by_type(pa.paWASAPI)
        result = [
            {
                "name": "default_output",
                "description": "Default system audio (WASAPI loopback)",
                "monitor": True,
            },
            {
                "name": "default_input",
                "description": "Default microphone",
                "monitor": False,
            },
        ]
        for index in range(engine.get_device_count()):
            info = engine.get_device_info_by_index(index)
            if info["hostApi"] == wasapi["index"] and info["maxInputChannels"] > 0:
                result.append(
                    {
                        "name": _device_id(info),
                        "description": info["name"],
                        "monitor": bool(info.get("isLoopbackDevice")),
                    }
                )
        return result


def select_device(engine, pa, name="", *, loopback=False):
    wasapi = engine.get_host_api_info_by_type(pa.paWASAPI)
    if name and name not in ("default_input", "default_output"):
        matches = [
            engine.get_device_info_by_index(i) for i in range(engine.get_device_count())
        ]
        matches = [
            m
            for m in matches
            if m["hostApi"] == wasapi["index"]
            and m["maxInputChannels"] > 0
            and _device_id(m) == name
            and bool(m.get("isLoopbackDevice")) == loopback
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "The selected Windows audio device is unavailable or ambiguous; refresh audio devices"
            )
        return matches[0]
    if loopback:
        return engine.get_default_wasapi_loopback()
    return engine.get_device_info_by_index(wasapi["defaultInputDevice"])


# FFmpeg holds video back once it runs 100 ms ahead of the sound, and video held
# back is frames lost for good. Silence must therefore keep arriving well inside
# that, while a packet merely running late, during real sound, gets more patience.
SILENCE_TICK_IDLE = 0.05
SILENCE_TICK_PLAYING = 0.15
PLAYING_WITHIN = 1.0


def silence_wait(seconds_since_sound):
    """How long to wait for a packet before filling the gap with silence."""
    return SILENCE_TICK_PLAYING if seconds_since_sound < PLAYING_WITHIN else SILENCE_TICK_IDLE


class PCMStream:
    """Bounded callback queue. No disk audio, polling timer, or silent sample loss."""

    def __init__(
        self, engine, pa, device, *, rate=48000, channels=2, silence_on_idle=False
    ):
        # 5 s of 20 ms packets, under 1 MB: FFmpeg pauses reading sound while video
        # catches up (a game loading, a permission prompt), and 1.2 s ended recordings.
        self.queue = queue.Queue(maxsize=250)
        self.error = None
        self.target_rate, self.target_channels = rate, channels
        self.silence_on_idle = silence_on_idle
        self.last_samples = self.last_sound = time.monotonic()
        self.source_rate = int(device["defaultSampleRate"])
        self.source_channels = min(2, int(device["maxInputChannels"]))
        if self.source_channels < 1:
            raise RuntimeError("This device has no input channels")
        self.state, self.pending = None, bytearray()

        def callback(data, _frames, _time, status):
            if status:
                self.error = RuntimeError(
                    "Audio capture overflowed; reduce system load and try again"
                )
                return None, pa.paAbort
            try:
                self.queue.put_nowait(data)
            except queue.Full:
                self.error = RuntimeError(
                    "Audio capture could not keep up; no samples were silently discarded"
                )
                return None, pa.paAbort
            return None, pa.paContinue

        self.stream = engine.open(
            format=pa.paInt16,
            channels=self.source_channels,
            rate=self.source_rate,
            input=True,
            input_device_index=int(device["index"]),
            frames_per_buffer=max(1, self.source_rate // 50),
            stream_callback=callback,
        )

    def discard(self):
        """Drop what has been captured so far: nobody is listening yet."""
        if self.error:
            raise self.error
        try:
            while True:
                self.queue.get_nowait()
        except queue.Empty:
            pass
        self.pending.clear()
        self.last_samples = time.monotonic()

    def read(self, frames, timeout=3):
        op = _audioop()
        size = frames * self.target_channels * 2
        now = time.monotonic()
        deadline = now + (silence_wait(now - self.last_sound) if self.silence_on_idle else timeout)
        while len(self.pending) < size:
            if self.error:
                raise self.error
            try:
                data = self.queue.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty as exc:
                if self.error:
                    raise self.error
                if self.silence_on_idle and not self.error:
                    # WASAPI loopback may stop packets while no app renders
                    # sound. Preserve that interval, without masking a missing
                    # microphone or a callback overflow.
                    now = time.monotonic()
                    missing = max(
                        frames, round((now - self.last_samples) * self.target_rate)
                    )
                    if missing > self.target_rate * 3:
                        raise RuntimeError(
                            "System audio stalled for more than three seconds"
                        ) from exc
                    self.pending.extend(bytes(missing * self.target_channels * 2))
                    self.last_samples = now
                    break
                raise RuntimeError(
                    "Windows supplied no audio; check the microphone privacy setting and device"
                ) from exc
            channels = self.source_channels
            if channels == 2 and self.target_channels == 1:
                data = op.tomono(data, 2, 0.5, 0.5)
                channels = 1
            data, self.state = op.ratecv(
                data, 2, channels, self.source_rate, self.target_rate, self.state
            )
            if channels == 1 and self.target_channels == 2:
                data = op.tostereo(data, 2, 1, 1)
            self.pending.extend(data)
            self.last_samples = self.last_sound = time.monotonic()
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def close(self):
        self.stream.stop_stream()
        self.stream.close()


def wait_for_reader(output, streams):
    """Tell Jarvis the sound is flowing, and keep it fresh until FFmpeg connects.

    FFmpeg stamps the first sound it receives as time zero, and stamps video from
    the moment capture starts. Sound arriving even half a second after that leaves
    video permanently "ahead", and FFmpeg then throws most of its frames away.
    """
    connecting = threading.Thread(target=output.connect, daemon=True)
    connecting.start()
    print(READY, flush=True)
    while connecting.is_alive():
        for stream in streams:
            stream.discard()
        connecting.join(0.02)
    if not output.connected:
        raise RuntimeError("FFmpeg did not open the audio input")
    for stream in streams:
        stream.discard()
    output.write(bytes(3840))  # 20 ms of silence: the sound's clock starts now


def mix_to_pipe(config, pipe):
    from .pipes import PipeWriter

    pa, op = _library(), _audioop()
    streams = []
    output = PipeWriter(pipe)
    with output_closed(output), pa.PyAudio() as engine:
        try:
            device = config.get("audio_device", "")
            if config.get("system_audio"):
                name = device if device.startswith("loopback:") else "default_output"
                streams.append(
                    PCMStream(
                        engine,
                        pa,
                        select_device(engine, pa, name, loopback=True),
                        silence_on_idle=True,
                    )
                )
            if config.get("microphone"):
                name = device if device.startswith("input:") else "default_input"
                streams.append(PCMStream(engine, pa, select_device(engine, pa, name)))
            if not streams:
                raise RuntimeError("No recording audio source selected")
            wait_for_reader(output, streams)
            while True:
                frames = [stream.read(960) for stream in streams]
                data = (
                    frames[0]
                    if len(frames) == 1
                    else op.add(op.mul(frames[0], 2, 0.5), op.mul(frames[1], 2, 0.5), 2)
                )
                output.write(data)
        finally:
            for stream in streams:
                stream.close()


def app_audio_to_pipe(program, include, pipe):
    """One program's sound (``include``) or everything except it, from the
    voice worker's process-loopback mode, relayed into an FFmpeg pipe."""
    import subprocess
    from .pipes import PipeWriter
    from ..host import child_options
    from ..native_worker import binary_path

    worker = binary_path()
    if worker is None:
        raise RuntimeError("The Windows voice worker is missing; reinstall the complete Jarvis package")
    output = PipeWriter(pipe)
    with output_closed(output):
        process = subprocess.Popen([str(worker), "--app-audio"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, **child_options())
        try:
            process.stdin.write(json.dumps({"process": program, "include": include}).encode() + b"\n")
            process.stdin.close()
            chunk = process.stdout.read(3840)   # sound is flowing, or the worker has failed
            if chunk:
                connecting = threading.Thread(target=output.connect, daemon=True)
                connecting.start()
                print(READY, flush=True)
                while chunk and connecting.is_alive():
                    chunk = process.stdout.read(3840)
            if chunk:
                connecting.join()
                output.write(bytes(3840))  # the sound's clock starts now
                while chunk := process.stdout.read(3840):
                    output.write(chunk)
            detail = process.stderr.read(4096).decode(errors="replace")
            message = next((json.loads(line).get("message") for line in detail.splitlines()
                            if line.startswith("{")), detail.strip())
            raise RuntimeError(message or f"Audio capture for {program} stopped")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()


def track_to_pipe(config, pipe):
    """One replay track, unmixed: "game" (everything except ``app``, or all
    system sound), "app" (only that program) or "microphone"."""
    track = config["track"]
    kind, program = track.get("kind"), track.get("app", "")
    if kind == "app" or (kind == "game" and program):
        return app_audio_to_pipe(program, kind == "app", pipe)
    if kind not in ("game", "microphone"):
        raise RuntimeError("Unknown replay audio track")
    from .pipes import PipeWriter

    pa = _library()
    output = PipeWriter(pipe)
    with output_closed(output), pa.PyAudio() as engine:
        stream = (PCMStream(engine, pa, select_device(engine, pa, "default_output", loopback=True), silence_on_idle=True)
                  if kind == "game" else PCMStream(engine, pa, select_device(engine, pa, "default_input")))
        try:
            wait_for_reader(output, [stream])
            while True:
                output.write(stream.read(960))
        finally:
            stream.close()


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("pipe")
    options = parser.parse_args(args)
    config = json.loads(sys.stdin.buffer.readline(65537))
    if not isinstance(config, dict) or not config.get("configured"):
        raise RuntimeError("Recording audio requires reviewed capture setup")
    try:
        if config.get("track"):
            track_to_pipe(config, options.pipe)
        else:
            mix_to_pipe(config, options.pipe)
    except Exception as exc:
        code = getattr(exc, "winerror", exc.args[0] if exc.args else None)
        if code in (109, 232, 233, 995):
            return 0  # FFmpeg closed its audio input on stop
        raise
