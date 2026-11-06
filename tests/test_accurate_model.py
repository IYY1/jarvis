"""Accurate command model (Whisper via sherpa-onnx), adaptive gate, model install.

Fake-model tests always run. Real-model tests run when JARVIS_WHISPER_DIR points
to an unpacked sherpa-onnx-whisper-small.en folder.
"""

import hashlib
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

from jarvis import models
from jarvis.commands import Parser, closest_match, match_candidates
from jarvis.voice import NoiseFloor

from tests.test_voice_upgrade import APPS, FakeModel, FakeRecognizer, words


class TranscriptMatchingTests(unittest.TestCase):
    def setUp(self):
        self.candidates = match_candidates(Parser().grammar(APPS))

    def test_digits_and_background_speech(self):
        self.assertEqual(closest_match("Go to workspace 3.", self.candidates)[0], "go to workspace three")
        self.assertEqual(closest_match("start recording tomorrow will be mostly cloudy with rain",
                                       self.candidates)[0], "start recording")
        self.assertEqual(closest_match("uh open firefox and the weather tomorrow", self.candidates)[0], "open firefox")
        self.assertIsNone(closest_match("the weather tomorrow will be mostly cloudy", self.candidates))


class NoiseFloorTests(unittest.TestCase):
    def test_gate_rises_with_steady_background_and_falls_in_silence(self):
        noise = NoiseFloor(0.005)
        for _ in range(2000):            # a minute of TV-level background
            noise.update(0.02)
        self.assertGreater(noise.gate, 0.05)
        self.assertLessEqual(noise.gate, NoiseFloor.CEILING)   # a close voice still gets through
        for _ in range(100):             # three seconds of quiet
            noise.update(0.001)
        self.assertAlmostEqual(noise.gate, 0.005, places=3)

    def test_fixed_gate_when_switched_off(self):
        noise = NoiseFloor(0.005, adaptive=False)
        for _ in range(500):
            noise.update(0.05)
        self.assertEqual(noise.gate, 0.005)


class FakeTranscriber:
    name = "whisper-small"

    def __init__(self, text):
        self.text, self.calls = text, []

    def transcribe(self, pcm):
        self.calls.append(len(pcm))
        return self.text


class AccurateEngineTests(unittest.TestCase):
    def setUp(self):
        from jarvis.vosk_engine import VoskEngine
        module = types.SimpleNamespace(Model=FakeModel, KaldiRecognizer=FakeRecognizer, SetLogLevel=lambda _l: None)
        patcher = patch.dict(sys.modules, {"vosk": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        valid = patch.object(models, "valid_model", return_value=True)
        valid.start()
        self.addCleanup(valid.stop)
        self.transcriber = FakeTranscriber("Open Stremio.")
        self.engine = VoskEngine(Parser().grammar(APPS), "jarvis", 0.5, "/model", transcriber=self.transcriber)
        FakeRecognizer.script = []
        self.loud = b"\x10\x00" * 480

    def test_accurate_transcript_beats_a_wrong_grammar_guess(self):
        self.engine.begin_command()
        FakeRecognizer.script = [("grammar", words("open steam", 0.9))]
        result = next(r for _ in range(5) if (r := self.engine.feed(self.loud)))
        self.assertEqual((result.text, result.heard), ("open stremio", "open stremio"))
        self.assertTrue(self.transcriber.calls)

    def test_own_end_of_command_when_vosk_keeps_waiting(self):
        # Vosk never finalizes (e.g. a TV keeps talking far away); 0.6 s of
        # quiet after our speech ends the command anyway.
        self.engine.begin_command()
        for _ in range(10):
            self.assertIsNone(self.engine.feed(self.loud))
        result = None
        for _ in range(25):
            result = result or self.engine.feed(bytes(960), quiet=True)
        self.assertEqual(result.text, "open stremio")

    def test_flush_at_deadline(self):
        self.engine.begin_command()
        for _ in range(5):
            self.engine.feed(self.loud)
        self.assertEqual(self.engine.flush().text, "open stremio")
        self.assertIsNone(self.engine.flush())        # nothing left


class InstallTests(unittest.TestCase):
    def test_verified_archive_unpacks_only_model_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:bz2") as tar:
                for name, data in (("m/enc.onnx", b"e"), ("m/tok.txt", b"t"), ("m/big-fp32.onnx", b"x" * 100),
                                   ("m/test_wavs/0.wav", b"w")):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            archive = root / "m.tar.bz2"
            archive.write_bytes(buffer.getvalue())
            spec = {"archive": "m", "kind": "whisper", "megabytes": 1,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "files": {"encoder": "enc.onnx", "tokens": "tok.txt"}}
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data")}), \
                    patch.dict(models.ACCURATE_MODELS, {"test": spec}):
                folder = models.install_accurate("test", archive=archive)
                self.assertEqual(sorted(p.name for p in folder.iterdir()), ["enc.onnx", "tok.txt"])
                self.assertTrue(models.accurate_model_ready("test"))
                bad = dict(spec, sha256="0" * 64, archive="n")
                with patch.dict(models.ACCURATE_MODELS, {"bad": bad}), self.assertRaisesRegex(ValueError, "Checksum"):
                    models.install_accurate("bad", archive=archive)


WHISPER = os.environ.get("JARVIS_WHISPER_DIR", "")


@unittest.skipUnless(WHISPER, "set JARVIS_WHISPER_DIR to an unpacked sherpa-onnx-whisper-small.en folder")
class RealWhisperTests(unittest.TestCase):
    def test_transcribes_upstream_recording(self):
        from jarvis.transcriber import Transcriber
        fixture = os.environ.get("JARVIS_SPEECH_TEST_RAW")
        if not fixture:
            self.skipTest("set JARVIS_SPEECH_TEST_RAW to goforward.raw")
        with patch.object(models, "accurate_model_dir", return_value=Path(WHISPER)), \
                patch("jarvis.transcriber.accurate_model_dir", return_value=Path(WHISPER)), \
                patch("jarvis.transcriber.accurate_model_ready", return_value=True):
            text = Transcriber(threads=2).transcribe(Path(fixture).read_bytes())
        self.assertIn("forward", text.lower())
        self.assertIn("meters", text.lower())


if __name__ == "__main__":
    unittest.main()
