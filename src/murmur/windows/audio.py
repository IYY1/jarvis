"""WASAPI microphone and system loopback; streams exist only during explicit use."""

from __future__ import annotations
import argparse
import json
import queue
import sys
import time


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


class PCMStream:
    """Bounded callback queue. No disk audio, polling timer, or silent sample loss."""

    def __init__(
        self, engine, pa, device, *, rate=48000, channels=2, silence_on_idle=False
    ):
        self.queue = queue.Queue(maxsize=60)
        self.error = None
        self.target_rate, self.target_channels = rate, channels
        self.silence_on_idle = silence_on_idle
        self.last_samples = time.monotonic()
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

    def read(self, frames, timeout=3):
        op = _audioop()
        size = frames * self.target_channels * 2
        deadline = time.monotonic() + (0.2 if self.silence_on_idle else timeout)
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
            self.last_samples = time.monotonic()
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def close(self):
        self.stream.stop_stream()
        self.stream.close()


def mix_to_pipe(config, pipe):
    from .pipes import PipeWriter

    pa, op = _library(), _audioop()
    streams = []
    with PipeWriter(pipe) as output, pa.PyAudio() as engine:
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


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("pipe")
    options = parser.parse_args(args)
    config = json.loads(sys.stdin.buffer.readline(65537))
    if not isinstance(config, dict) or not config.get("configured"):
        raise RuntimeError("Recording audio requires reviewed capture setup")
    try:
        mix_to_pipe(config, options.pipe)
    except Exception as exc:
        code = getattr(exc, "winerror", exc.args[0] if exc.args else None)
        if code in (109, 232, 233, 995):
            return 0  # FFmpeg closed its audio input on stop
        raise
