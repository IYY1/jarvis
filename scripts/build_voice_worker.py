#!/usr/bin/env python3
"""Build the optional Rust worker, Whisper recognizer and GTCRN denoiser.

Requires Cargo/Rust. Models are not downloaded. --test runs Rust unit tests
before installing checkout-local binaries; no service is started.
"""
if __package__:
    from .native_build import build
else:
    from native_build import build


def main():
    build(__doc__, [("gtcrn-denoiser", "jarvis-denoise"),
                    ("whisper-recognizer", "jarvis-whisper"),
                    ("voice-worker", "jarvis-voice")],
          "Rust is preferred when available on Linux; JARVIS_VOICE_WORKER=python selects compatibility mode.")


if __name__ == "__main__":
    main()
