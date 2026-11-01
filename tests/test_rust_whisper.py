"""Actual Rust Whisper inference, with deliberately supplied offline assets."""
import gc
import copy
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "native/whisper-recognizer/target/release/jarvis-whisper"
MODEL = os.environ.get("MURMUR_WHISPER_MODEL")
FIXTURE = os.environ.get("MURMUR_SPEECH_TEST_RAW")
TINY = os.environ.get("MURMUR_WHISPER_TINY_MODEL")
BASE = os.environ.get("MURMUR_WHISPER_BASE_MODEL")


@unittest.skipUnless(BINARY.is_file(), "build the standalone Rust recognizer")
class RustWhisperBinaryTests(unittest.TestCase):
    def test_does_not_link_an_external_recognition_runtime(self):
        for path in (BINARY, ROOT / "src/murmur/bin/jarvis-voice"):
            if not path.is_file():
                continue
            linked = subprocess.check_output(["ldd", str(path)], text=True).lower()
            for name in ("libpython", "onnxruntime", "sherpa", "libstdc++"):
                self.assertNotIn(name, linked)
        env = dict(os.environ, PATH="/nonexistent", LD_LIBRARY_PATH="")
        version = subprocess.check_output([str(BINARY), "--version"], env=env, text=True)
        self.assertIn("Rust Whisper", version)

    def test_incomplete_arguments_return_an_error_without_panicking(self):
        for args in ([], ["--threads"], ["--batch"], ["--diagnose"], ["--model-name"],
                     ["--model-name", "medium.en", "/missing", "/missing.raw"]):
            out = subprocess.run([str(BINARY), *args], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(out.returncode, 0)
            self.assertNotIn("panicked", out.stderr)


@unittest.skipUnless(BINARY.is_file() and TINY and BASE and FIXTURE, "supply tiny/base exports and public fixture")
class RustWhisperProfileTests(unittest.TestCase):
    def test_smaller_profiles_decode_full_commands_and_reset_caches(self):
        from scripts.benchmark_whisper_models import isolated, reference
        for name, folder in (("tiny.en", TINY), ("base.en", BASE)):
            with self.subTest(model=name):
                expected = reference(Path(folder), name, Path(FIXTURE))
                actual = isolated(BINARY, Path(folder), name, Path(FIXTURE), 2, expected)
                self.assertEqual(actual["ready"]["model"], name)
                first, second = actual["cases"][:7], actual["cases"][7:]
                self.assertEqual([row["text"] for row in first], [row["text"] for row in second])
                for row in actual["cases"]:
                    # Base's noisy punctuation and truncated-clip divergence
                    # remain explicit failures of the benchmark's strict gate.
                    # These supported full-utterance controls must still agree.
                    if name == "tiny.en" or row["case"] not in ("noise", "short"):
                        self.assertTrue(row["reference_identical"], row)

    def test_mismatched_encoder_and_decoder_are_rejected_without_panicking(self):
        from murmur.commands import Parser
        from murmur.config import DEFAULTS
        from murmur.native_worker import prepare
        config = copy.deepcopy(DEFAULTS)
        config["recognition"].update(accurate=True, noise_suppression=False)
        with patch.dict(os.environ, MURMUR_VOICE_WORKER="rust"):
            argv, payload, env = prepare(config, "test", Parser().grammar([]))
        payload["native"]["whisper"] = {
            "encoder": str(Path(TINY) / "tiny.en-encoder.int8.onnx"),
            "decoder": str(Path(BASE) / "base.en-decoder.int8.onnx"),
            "tokens": str(Path(TINY) / "tiny.en-tokens.txt"),
        }
        output = subprocess.run(argv + ["--transcribe"], input=json.dumps(payload) + "\n",
                                text=True, capture_output=True, env=env, timeout=20)
        self.assertNotEqual(output.returncode, 0)
        self.assertIn("export wiring", output.stdout)
        self.assertNotIn("panicked", output.stderr)


@unittest.skipUnless(BINARY.is_file() and MODEL and FIXTURE, "supply Whisper model and public fixture")
class RustWhisperInferenceTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("MURMUR_GTCRN_MODEL"), "supply GTCRN model")
    def test_minimum_length_denoised_command_matches_reference(self):
        from murmur.commands import Parser
        from murmur.config import DEFAULTS
        from murmur.native_worker import prepare
        from murmur.transcriber import Transcriber, release_unused_memory
        config = copy.deepcopy(DEFAULTS)
        config["recognition"].update(accurate=True, noise_suppression=True)
        with patch.dict(os.environ, MURMUR_VOICE_WORKER="rust"):
            argv, payload, env = prepare(config, "test", Parser().grammar([]))
        root = Path(MODEL)
        payload["native"]["whisper"] = {key: str(root / f"small.en-{name}") for key, name in
                                        (("encoder", "encoder.int8.onnx"), ("decoder", "decoder.int8.onnx"), ("tokens", "tokens.txt"))}
        payload["native"]["denoiser"] = os.environ["MURMUR_GTCRN_MODEL"]
        pcm = bytes(6400)
        reference = Transcriber(denoise=True)
        self.assertIsNotNone(reference._denoiser)
        expected = reference.transcribe(pcm)
        del reference
        gc.collect()
        release_unused_memory()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "minimum.raw"
            path.write_bytes(pcm)
            request = json.dumps(payload) + "\n" + json.dumps({"path": str(path)}) + "\n"
            out = subprocess.run(argv + ["--transcribe"], input=request, env=env,
                                 capture_output=True, text=True, check=True, timeout=30)
        result = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual(result[-1]["text"], expected)

    def test_matches_reference_across_commands_and_resets_caches(self):
        from murmur.transcriber import Transcriber, release_unused_memory
        from scripts.benchmark_transcriber import variants
        cases = variants(Path(FIXTURE).read_bytes())
        reference = Transcriber()
        expected = [reference.transcribe(pcm) for _, pcm in cases]
        del reference
        gc.collect()
        release_unused_memory()
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for i, (_, pcm) in enumerate(cases):
                path = Path(directory) / f"{i}.raw"
                path.write_bytes(pcm)
                paths.append(path)
            requests = "".join(json.dumps({"path": str(path)}) + "\n" for _ in range(2) for path in paths)
            env = dict(os.environ, PATH="/nonexistent", LD_LIBRARY_PATH="")
            out = subprocess.run([str(BINARY), "--batch", MODEL], input=requests, env=env,
                                 capture_output=True, text=True, check=True, timeout=60)
        responses = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual(responses[0]["event"], "ready")
        self.assertEqual([r["text"] for r in responses[1:]], expected * 2)

    def test_runtime_maps_contain_no_python_sherpa_or_onnxruntime(self):
        process = subprocess.Popen([str(BINARY), "--batch", MODEL], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=dict(os.environ, PATH="/nonexistent", LD_LIBRARY_PATH=""))
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(15), "Recognizer failed to load")
            ready = json.loads(process.stdout.readline())
            self.assertEqual(ready["event"], "ready")
            self.assertEqual(ready["embedding"], {"storage": "lossless-f16", "bytes": 51864 * 768 * 2})
            maps = Path(f"/proc/{process.pid}/maps").read_text().lower()
            for name in ("libpython", "onnxruntime", "sherpa", "libstdc++"):
                self.assertNotIn(name, maps)
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if not process.stdin.closed:
                process.stdin.close()
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
