"""Accurate offline transcription of one spoken command (sherpa-onnx, CPU).

Vosk stays in charge of the cheap, always-on part (hearing "Jarvis") and of
finding where a command ends. The few seconds of command audio are then
written down by a stronger model (Whisper small.en by default), which copes
much better with accents, and ``commands.closest_match`` picks the command.
Nothing is saved; the audio lives only in memory for the duration of a command.
"""
from __future__ import annotations

import array
import ctypes
import os
from pathlib import Path

from .models import ACCURATE_MODELS, DEFAULT_ACCURATE_MODEL, accurate_model_dir, accurate_model_ready, denoiser_path

SAMPLE_RATE = 16000
PADDING = array.array("f", [0.0]) * (SAMPLE_RATE // 4)  # 250 ms around the command


def default_threads() -> int:
    # Half the logical CPUs, at most 8: quick, without starving a running game.
    return max(1, min(8, (os.cpu_count() or 2) // 2))


def release_unused_memory() -> None:
    """Return freed native scratch pages on glibc; other allocators can skip it.

    This doesn't unload weights or touch live allocations. Run only between
    commands, after the per-utterance stream has been destroyed.
    """
    if os.name == "nt":
        return
    try:
        trim = ctypes.CDLL(None).malloc_trim
    except (AttributeError, OSError):
        return
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    trim(0)


class Transcriber:
    def __init__(self, name: str = DEFAULT_ACCURATE_MODEL, denoise: bool = False, threads: int | None = None,
                 *, memory_efficient: bool = True):
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError("sherpa-onnx is not installed; run: pip install -e '.[voice]'") from exc
        if not accurate_model_ready(name):
            raise RuntimeError("Accurate voice model missing. Install it with: murmur install-model")
        spec = ACCURATE_MODELS[name]
        folder = accurate_model_dir(name)
        files = {key: str(folder / value) for key, value in spec["files"].items()}
        threads = threads or default_threads()
        self.name = name
        # Keep the same model resident. Only ORT's scratch-buffer retention
        # changes; memory_efficient=False is used for allocator benchmarks.
        self._memory_efficient = memory_efficient
        if spec["kind"] == "whisper":
            provider = "cpu:" + str(Path(__file__).with_name("onnx-cpu.conf")) if memory_efficient else "cpu"
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
                files["encoder"], files["decoder"], files["tokens"], language="en", num_threads=threads,
                provider=provider)
        else:
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                files["encoder"], files["decoder"], files["joiner"], files["tokens"],
                num_threads=threads, model_type=spec["kind"])
        self._denoiser = None
        if denoise and denoiser_path().is_file():
            config = sherpa_onnx.OfflineSpeechDenoiserConfig(
                model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
                    gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(model=str(denoiser_path())),
                    num_threads=threads))
            self._denoiser = sherpa_onnx.OfflineSpeechDenoiser(config)
        if self._memory_efficient:
            release_unused_memory()

    def transcribe(self, pcm: bytes) -> str:
        """16 kHz mono signed 16-bit PCM in, plain text out ("" if nothing)."""
        if len(pcm) < SAMPLE_RATE // 5 * 2:    # under 0.2 s
            return ""
        # Scaling signed 16-bit PCM by this power of two is exact in float32.
        # Keep compact samples instead of allocating one Python float per sample.
        raw = memoryview(pcm)[:len(pcm) - len(pcm) % 2].cast("h")
        samples = array.array("f", (value / 32768 for value in raw))
        del raw
        if self._denoiser is not None:
            samples = array.array("f", self._denoiser(samples, SAMPLE_RATE).samples)
        # Pad the owned buffer in place and release it before inference.
        samples[:0] = PADDING
        samples.extend(PADDING)
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        del samples
        self._recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        del stream
        if self._memory_efficient:
            release_unused_memory()
        return text


def available(name: str = DEFAULT_ACCURATE_MODEL) -> bool:
    import importlib.util
    return importlib.util.find_spec("sherpa_onnx") is not None and accurate_model_ready(name)


__all__ = ["Transcriber", "available", "default_threads"]
