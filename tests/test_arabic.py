"""Arabic commands, dictation policy and (when models are supplied) the real worker.

Pure policy tests need nothing installed. The worker test runs only with
MURMUR_ARABIC_MODEL (the moonshine-streaming-tiny-ar folder) and
MURMUR_ARABIC_TEST_RAW (16 kHz s16le Arabic speech); it never opens a
microphone: the worker reads the file through MURMUR_TEST_CAPTURE_FILE.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import unittest.mock

from murmur import config
from murmur.commands import CommandError, Intent, Parser, closest_match, match_candidates
from murmur.dictation import split_stop
from murmur.phrases import translate_arabic

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tests/fixtures/arabic_command_cases.json").read_text(encoding="utf-8"))


class ArabicCommandTests(unittest.TestCase):
    def test_shared_cases_match_the_rust_translator(self):
        # The same file drives native/command-phrases/src/arabic.rs's test.
        for heard, english in CASES:
            with self.subTest(heard=heard):
                self.assertEqual(translate_arabic(heard), english)

    def test_parser_runs_arabic_through_the_english_rules(self):
        parser = Parser({})
        self.assertEqual(parser.parse("علّ الصوت"), Intent("volume_up"))
        self.assertEqual(parser.parse("روح لمساحة العمل ٣"), Intent("workspace", 3))
        self.assertEqual(parser.parse("احفظ آخر دقيقتين"), Intent("replay_save", 120))
        self.assertEqual(parser.parse("جارفس اسمعني"), Intent("dictation_start"))
        with self.assertRaisesRegex(CommandError, "Choose workspace one through ten"):
            parser.parse("روح لمساحة العمل عشرين")
        with self.assertRaisesRegex(CommandError, "Arabic phrase not recognized"):
            parser.parse("كيف حالك اليوم")

    def test_configured_close_words_and_arabic_aliases_apply(self):
        c = config.validate({"aliases": {"فايرفوكس": "firefox.desktop"}, "close_words": ["quit"]})
        parser = Parser.from_config(c)
        self.assertEqual(parser.parse("سكّر ستيم"), Intent("close_app", "steam"))
        # The alias is an exact configured name, so diacritics fold onto it.
        self.assertEqual(parser.parse("افتح فايرفوكس"), Intent("open_app", "firefox"))

    def test_closest_match_accepts_arabic_only_for_supported_commands(self):
        parser = Parser({})
        grammar = parser.grammar(["firefox", "steam"])
        candidates = match_candidates(grammar)
        self.assertEqual(closest_match("وطّ الصوت لو سمحت", candidates)[0], "volume down")
        self.assertEqual(closest_match("سكر ستيم", candidates)[0], "close steam")
        # Arabic-script sounds of English speech never become a command.
        self.assertIsNone(closest_match("اوبن فايرفوكس", candidates))


class DictationPolicyTests(unittest.TestCase):
    def test_stop_phrases_end_dictation_without_being_typed(self):
        stops = ["stop dictation", "وقف الكتابة"]
        self.assertEqual(split_stop("send it today. Stop dictation.", stops), ("send it today.", True))
        self.assertEqual(split_stop("ارسل التقرير وقّف الكتابة", stops), ("ارسل التقرير", True))
        self.assertEqual(split_stop("stop dictation software is fine", stops), ("stop dictation software is fine", False))

    def test_untyped_text_goes_to_the_clipboard_except_for_password_fields(self):
        import asyncio
        import types
        from unittest.mock import patch
        from murmur import dictation
        from murmur.daemon import Service
        copies = []
        host = types.SimpleNamespace(dictation=dictation.Session())
        with patch("murmur.dictation.copy_to_clipboard", side_effect=lambda text: copies.append(text) or True):
            first = asyncio.run(Service.hold(host, "hello", "no-field", "No text field is focused."))
            asyncio.run(Service.hold(host, " world", "error", "Could not type."))
            secret = asyncio.run(Service.hold(host, " hunter2", "sensitive", "Password field."))
        self.assertTrue(first.endswith("press Ctrl+V to paste it."))
        self.assertEqual(copies, ["hello", "hello world"])  # the whole untyped text, never the password
        self.assertEqual(secret, "Password field.")

    def test_language_command_switches_arabic_on_and_off(self):
        import contextlib
        import io
        from argparse import Namespace
        from unittest.mock import patch
        from murmur.cli import run_language
        saved = []
        cfg = config.validate({})
        def request(op, **payload):
            if op == "save_config":
                saved.append(payload["config"]["recognition"]["language"])
            return {"ok": True, "config": cfg}
        with patch("murmur.models.arabic_model_ready", return_value=True), contextlib.redirect_stdout(io.StringIO()) as out:
            run_language(Namespace(language="arabic"), request)
            run_language(Namespace(language="english"), request)
            run_language(Namespace(language=None), request)
        self.assertEqual(saved, ["ar_en", "en"])
        self.assertIn("English only", out.getvalue().splitlines()[-1])
        with patch("murmur.models.arabic_model_ready", return_value=False):
            with self.assertRaisesRegex(ValueError, "install-model --arabic"):
                run_language(Namespace(language="arabic"), request)

    def test_dictation_language_follows_the_phrase_unless_fixed(self):
        from murmur.dictation import language
        def cfg(recognition, dictation):
            return {"recognition": {"language": recognition}, "dictation": {"language": dictation}}
        both = cfg("ar_en", "auto")
        self.assertEqual(language(both, "جارفس اسمعني"), "ar")
        self.assertEqual(language(both, "listen to me"), "en")
        self.assertEqual(language(both), "ar")
        self.assertEqual(language(cfg("en", "auto"), "اسمعني"), "en")
        self.assertEqual(language(cfg("ar_en", "en"), "اسمعني"), "en")
        self.assertEqual(language(cfg("ar_en", "ar"), "listen"), "ar")

    def test_dictation_settings_are_validated(self):
        defaults = config.validate({})["dictation"]
        self.assertEqual(defaults["language"], "auto")
        for bad in ({"language": "fr"}, {"idle_seconds": 1}, {"max_minutes": 0}, {"stop_phrases": ["x"] * 9},
                    {"keyboard_fallback": "yes"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate({"dictation": bad})
        with self.assertRaises(ValueError):
            config.validate({"recognition": {"language": "ar"}})


@unittest.skipUnless(os.environ.get("MURMUR_ARABIC_MODEL") and os.environ.get("MURMUR_ARABIC_TEST_RAW"),
                     "set MURMUR_ARABIC_MODEL and MURMUR_ARABIC_TEST_RAW to run the real worker")
class ArabicDictationWorkerTests(unittest.TestCase):
    def test_worker_dictates_arabic_speech_and_stops_when_quiet(self):
        from murmur import native_worker
        built = ROOT / "native/voice-worker/target/release/jarvis-voice"
        binary = built if built.is_file() else native_worker.binary_path()
        if binary is None:
            self.skipTest("build the Rust voice worker first")
        speech = Path(os.environ["MURMUR_ARABIC_TEST_RAW"]).read_bytes()
        with tempfile.NamedTemporaryFile(suffix=".s16le") as capture:
            capture.write(b"\0" * 32000 + speech + b"\0" * 32000)
            capture.flush()
            c = config.validate({"privacy": {"onboarded": True}, "recognition": {"language": "ar_en"},
                                 "dictation": {"idle_seconds": 5}})
            with unittest.mock.patch("murmur.native_worker.arabic_model_dir",
                                     return_value=Path(os.environ["MURMUR_ARABIC_MODEL"])):
                _, payload, env = native_worker.prepare(c, "dictation", Parser.from_config(c).grammar([]))
            env = dict(env, MURMUR_TEST_CAPTURE_FILE=capture.name, MURMUR_OWNER_PID=str(os.getpid()))
            proc = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True)
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
            events = []
            for line in proc.stdout:
                events.append(json.loads(line))
                if events[-1].get("event") == "dictation" and events[-1].get("state") == "stopped":
                    break
            proc.stdin.close()
            proc.wait(timeout=10)
        texts = [e["text"] for e in events if e.get("event") == "dictation_text"]
        self.assertTrue(texts and all(any("؀" <= c <= "ۿ" for c in t) for t in texts), events)
        self.assertEqual(events[-1]["reason"], "idle")
        self.assertFalse(any(e.get("event") == "command" for e in events), "dictation never runs commands")

    def test_hands_free_worker_dictates_in_the_language_it_is_asked_for(self):
        """“dictate ar” (said اسمعني) uses the Arabic model, “dictate en” Whisper."""
        from murmur import native_worker
        built = ROOT / "native/voice-worker/target/release/jarvis-voice"
        binary = built if built.is_file() else native_worker.binary_path()
        if binary is None:
            self.skipTest("build the Rust voice worker first")
        speech = Path(os.environ["MURMUR_ARABIC_TEST_RAW"]).read_bytes()
        arabic = lambda text: any("\u0600" <= c <= "\u06ff" for c in text)
        for language in ("ar", "en"):
            with self.subTest(language=language), tempfile.NamedTemporaryFile(suffix=".s16le") as capture:
                capture.write(b"\0" * 32000 + speech + b"\0" * 96000)
                capture.flush()
                c = config.validate({"privacy": {"onboarded": True}, "recognition": {"language": "ar_en"},
                                     "activation": {"mode": "wake_word"}})
                with unittest.mock.patch("murmur.native_worker.arabic_model_dir",
                                         return_value=Path(os.environ["MURMUR_ARABIC_MODEL"])):
                    _, payload, env = native_worker.prepare(c, "wake_word", Parser.from_config(c).grammar([]))
                env = dict(env, MURMUR_TEST_CAPTURE_FILE=capture.name, MURMUR_OWNER_PID=str(os.getpid()))
                proc = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True)
                proc.stdin.write(json.dumps(payload) + "\n" + f"dictate {language}\n")
                proc.stdin.flush()
                texts = []
                for line in proc.stdout:
                    event = json.loads(line)
                    if event.get("event") == "dictation_text":
                        texts.append(event["text"])
                        break
                    self.assertNotEqual(event.get("event"), "dictation_error", event)
                proc.stdin.write("dictate_stop\n")
                proc.stdin.close()
                proc.terminate()
                proc.wait(timeout=10)
                proc.stdout.close()
                self.assertTrue(texts, "no dictation text")
                self.assertEqual(arabic(texts[0]), language == "ar", texts)


if __name__ == "__main__":
    unittest.main()
