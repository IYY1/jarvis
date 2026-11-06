"""The Rust denoiser is independent of Python and ONNX Runtime."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "native/gtcrn-denoiser/target/release/jarvis-denoise"
MODEL = os.environ.get("JARVIS_GTCRN_MODEL")
FIXTURE = os.environ.get("JARVIS_SPEECH_TEST_RAW")


@unittest.skipUnless(BINARY.is_file(), "build the standalone Rust denoiser")
class RustDenoiserBinaryTests(unittest.TestCase):
    def test_does_not_link_python_sherpa_or_onnxruntime(self):
        output = subprocess.check_output(["ldd", str(BINARY)], text=True).lower()
        for dependency in ("libpython", "sherpa", "onnxruntime", "libstdc++"):
            self.assertNotIn(dependency, output)


@unittest.skipUnless(BINARY.is_file() and MODEL and FIXTURE, "supply GTCRN model and public fixture")
class RustDenoiserNumericalTests(unittest.TestCase):
    def test_matches_native_reference_and_resets_recurrent_state(self):
        subprocess.run([sys.executable, str(ROOT / "scripts/check_rust_denoiser.py"),
                        "--fixture", FIXTURE, "--model", MODEL, "--repeats", "2"],
                       stdout=subprocess.DEVNULL, check=True, timeout=45)

    def test_incomplete_pcm_samples_cannot_create_an_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.raw"; source.write_bytes(b"\0" * 641)
            output = root / "output.f32"
            result = subprocess.run([str(BINARY), MODEL, str(source), str(output)], capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
