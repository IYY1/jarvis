"""Vosk engine, closest-match guessing, Jarvis wake word and app-name fixes.

Fake-recognizer tests always run. Real-model tests run when MURMUR_VOSK_MODEL
points to vosk-model-small-en-us-0.15 (and MURMUR_SPEECH_TEST_RAW to the
upstream goforward.raw recording for the audio tests).
"""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from murmur import config
from murmur.apps import AppRegistry, id_stem, spoken_numbers
from murmur.commands import Parser, closest_match, match_candidates, wake_variants

APPS = ["firefox", "dolphin", "discord", "deadlock", "steam", "stremio", "tekken eight", "calculator"]


class ClosestMatchTests(unittest.TestCase):
    def setUp(self):
        self.candidates = match_candidates(Parser().grammar(APPS))

    def test_near_misses_resolve(self):
        for heard, expected in (("open fire fox", "open firefox"), ("open stream io", "open stremio"),
                                ("go to workspace tree", "go to workspace three"),
                                ("stat recording", "start recording"), ("open taken eight", "open tekken eight"),
                                ("switch to discourt", "switch to discord"), ("open team", "open steam"),
                                ("could you open fire fox please", "open firefox")):
            with self.subTest(heard=heard):
                self.assertEqual(closest_match(heard, self.candidates)[0], expected)

    def test_unclear_or_unrelated_speech_runs_nothing(self):
        for heard in ("i want to eat pizza", "what time is it", "can sell", "recording", "open", "", "hey"):
            with self.subTest(heard=heard):
                self.assertIsNone(closest_match(heard, self.candidates))

    def test_command_before_long_background_speech_still_matches(self):
        heard = "start recording " + "and the weather tomorrow will be cloudy " * 7
        self.assertGreater(len(heard), 256)
        self.assertEqual(closest_match(heard, self.candidates)[0], "start recording")

    def test_wordings_of_the_same_command_do_not_compete(self):
        # "start recording" and "start a recording" are one intent; only a
        # *different* command (stop recording) counts as a rival.
        self.assertEqual(closest_match("start a cording", self.candidates)[0], "start a recording")


class WakeWordTests(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(wake_variants("hey jarvis"), ["hello jarvis", "hey jarvis", "hi jarvis", "jarvis", "okay jarvis"])
        self.assertEqual(wake_variants("computer"), ["computer"])

    def test_new_default_and_recognition_settings(self):
        defaults = config.validate({})
        self.assertEqual(defaults["activation"]["wake_phrase"], "hey jarvis")
        self.assertEqual(defaults["recognition"], {"backend": "auto", "model_path": "", "closest_match": True,
                                                   "accurate": True, "noise_suppression": False})
        with self.assertRaises(ValueError):
            config.validate({"recognition": {"backend": "whisper"}})


class AppNameTests(unittest.TestCase):
    def test_numbers_and_dotted_names(self):
        self.assertEqual(spoken_numbers("tekken 8"), "tekken eight")
        self.assertEqual(id_stem("F.E.A.R. 2 Project Origin.desktop"), "F.E.A.R. 2 Project Origin")
        self.assertEqual(id_stem("org.mozilla.firefox.desktop"), "firefox")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TEKKEN 8.desktop").write_text("[Desktop Entry]\nType=Application\nName=TEKKEN 8\nExec=steam steam://rungameid/1778820\n")
            registry = AppRegistry(roots=[root])
            self.assertEqual(registry.resolve("tekken eight").id, "TEKKEN 8.desktop")
            self.assertNotIn("2 project origin", registry.index)


# ---------------------------------------------------------------- fake Vosk --
class FakeRecognizer:
    """Plays back scripted final results: one per AcceptWaveform call that returns True."""
    script: list = []

    def __init__(self, model, rate, grammar=None):
        self.grammar = json.loads(grammar) if grammar else None
        self.fed = 0

    def SetWords(self, _flag):
        pass

    def Reset(self):
        pass

    def AcceptWaveform(self, pcm):
        self.fed += len(pcm)
        key = "grammar" if self.grammar else "open"
        return bool(FakeRecognizer.script) and FakeRecognizer.script[0][0] == key and self.fed >= 960

    def Result(self):
        return json.dumps(FakeRecognizer.script.pop(0)[1])

    def FinalResult(self):
        if FakeRecognizer.script and FakeRecognizer.script[0][0] == "final":
            return json.dumps(FakeRecognizer.script.pop(0)[1])
        return json.dumps({"text": ""})


class FakeModel:
    def __init__(self, _path):
        pass

    @staticmethod
    def vosk_model_find_word(word):
        return -1 if word in ("stremio", "tekken") else 1


def words(text, conf=1.0, start=0.0, step=0.3):
    return {"text": text, "result": [{"word": w, "conf": conf, "start": start + i * step, "end": start + (i + 1) * step}
                                     for i, w in enumerate(text.split())]}


class FakeVoskEngineTests(unittest.TestCase):
    def setUp(self):
        # Import before patching sys.modules: patch.dict removes modules first
        # imported while it is active, which would break later tests.
        from murmur import models
        from murmur.vosk_engine import VoskEngine
        module = types.SimpleNamespace(Model=FakeModel, KaldiRecognizer=FakeRecognizer, SetLogLevel=lambda _l: None)
        patcher = patch.dict(sys.modules, {"vosk": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        valid = patch.object(models, "valid_model", return_value=True)
        valid.start()
        self.addCleanup(valid.stop)
        self.engine = VoskEngine(Parser().grammar(APPS), "hey jarvis", 0.5, "/model")
        FakeRecognizer.script = []
        self.loud = b"\x10\x00" * 480

    def feed_until_result(self, frames=5):
        for _ in range(frames):
            result = self.engine.feed(self.loud)
            if result:
                return result
        return None

    def test_unpronounceable_apps_stay_reachable_through_closest_match(self):
        self.assertNotIn("stremio", self.engine.apps)       # not in the strict grammar
        self.assertNotIn("open stremio", self.engine.unsupported_phrases)
        self.assertIn("open stremio", self.engine._candidates)

    def test_silence_is_not_decoded(self):
        self.engine.begin_wake()
        for _ in range(500):
            self.assertIsNone(self.engine.feed(bytes(960), quiet=True))
        self.assertEqual(self.engine._wake.fed, 0)
        self.engine.feed(self.loud)                # speech starts: pre-roll is decoded too
        self.assertEqual(self.engine._wake.fed, 960 * 11)

    def test_wake_history_is_discarded_once_at_the_quiet_boundary(self):
        from murmur.vosk_engine import HANGOVER_FRAMES
        self.engine.begin_wake()
        self.engine.feed(self.loud)
        with patch.object(self.engine._wake, "Reset") as reset, \
                patch.object(self.engine._wake, "FinalResult") as final:
            for _ in range(HANGOVER_FRAMES - 1):
                self.assertIsNone(self.engine.feed(bytes(960), quiet=True))
            reset.assert_not_called()
            self.assertIsNone(self.engine.feed(bytes(960), quiet=True))
            reset.assert_called_once_with()
            # Idle silence stays gated and does not repeatedly reset a decoder.
            for _ in range(500):
                self.assertIsNone(self.engine.feed(bytes(960), quiet=True))
            reset.assert_called_once_with()
            final.assert_not_called()
        self.assertEqual(self.engine._mode, "wake")
        self.assertEqual(self.engine._utterance_bytes, 0)
        self.assertEqual(self.engine._wake_audio, b"")
        self.assertEqual(len(self.engine._preroll), 10)

    def test_quiet_boundary_does_not_discard_an_armed_command(self):
        from murmur.vosk_engine import HANGOVER_FRAMES
        self.engine.begin_command()
        self.engine.feed(self.loud)
        with patch.object(self.engine._command, "Reset") as reset:
            for _ in range(HANGOVER_FRAMES + 10):
                self.assertIsNone(self.engine.feed(bytes(960), quiet=True))
            reset.assert_not_called()
        self.assertEqual(self.engine._mode, "command")
        self.assertGreater(len(self.engine._command_audio), 0)

    def test_wake_with_misheard_greeting_and_one_breath_command(self):
        self.engine.begin_wake()
        FakeRecognizer.script = [("grammar", words("the jarvis open firefox"))]
        result = self.feed_until_result()
        self.assertEqual(result.kind, "wake")
        follow_up = self.engine.after_wake()
        self.assertEqual((follow_up.kind, follow_up.text), ("command", "open firefox"))

    def test_wake_name_late_in_a_sentence_does_not_wake(self):
        self.engine.begin_wake()
        FakeRecognizer.script = [("grammar", words("i told my friend jarvis"))]
        self.assertIsNone(self.feed_until_result())

    def test_strict_result_with_unknown_words_falls_back_to_closest_match(self):
        self.engine.begin_command()
        FakeRecognizer.script = [("grammar", words("open [unk]", 0.9)), ("final", {"text": "open stream io"})]
        result = self.feed_until_result()
        self.assertEqual((result.text, result.closest_match, result.heard), ("open stremio", True, "open stream io"))

    def test_disagreeing_passes_run_nothing(self):
        # Grammar says "launch deadlock", free transcript clearly says something else
        # that is close to "launch discord": not close enough to override -> nothing.
        self.engine.begin_command()
        FakeRecognizer.script = [("grammar", words("launch deadlock", 0.95)), ("final", {"text": "launch this cord"})]
        self.assertIsNone(self.feed_until_result())

    def test_free_transcript_overrides_when_it_is_a_very_close_match(self):
        self.engine.begin_command()
        FakeRecognizer.script = [("grammar", words("launch deadlock", 0.95)), ("final", {"text": "launch discord"})]
        self.assertEqual(self.feed_until_result().text, "launch discord")

    def test_one_breath_command_gets_the_same_cross_check(self):
        # Whisper hears "discord" where the grammar decoded "deadlock": a pause
        # before the command runs Discord, so one breath must not run Deadlock.
        heard = []
        self.engine._transcriber = types.SimpleNamespace(transcribe=lambda pcm: heard.append(pcm) or "Launch Discord.")
        self.engine.begin_wake()
        FakeRecognizer.script = [("grammar", words("hey jarvis launch deadlock", 0.95, step=0.01))]
        self.assertEqual(self.feed_until_result().kind, "wake")
        self.assertEqual(self.engine.after_wake().text, "launch discord")
        self.assertEqual(len(heard), 1)

    def test_agreeing_passes_report_the_stronger_score(self):
        # The service's minimum score may exceed the grammar's 0.7 here.
        self.engine._transcriber = types.SimpleNamespace(transcribe=lambda pcm: "Open Firefox.")
        self.engine.begin_command()
        FakeRecognizer.script = [("grammar", words("open firefox", 0.7))]
        result = self.feed_until_result()
        self.assertEqual((result.text, result.confidence, result.min_word_posterior), ("open firefox", 1.0, 0.7))

    def test_low_confidence_strict_result_is_not_run(self):
        self.engine.begin_command()
        FakeRecognizer.script = [("grammar", words("open firefox", 0.4)), ("final", {"text": "umm"})]
        self.assertIsNone(self.feed_until_result())


# ------------------------------------------------------------- real model --
MODEL = os.environ.get("MURMUR_VOSK_MODEL", "")
FIXTURE = os.environ.get("MURMUR_SPEECH_TEST_RAW", "")


@unittest.skipUnless(importlib.util.find_spec("vosk"), "optional voice dependency is not installed")
class VoskImportTests(unittest.TestCase):
    def test_binding_loads_without_download_helpers(self):
        # A clean interpreter: the voice worker never uses vosk's model-download
        # and subtitle helpers, so they must not stay in memory.
        code = ("import sys; from murmur.vosk_engine import _import_vosk; vosk = _import_vosk(); "
                "print(bool(vosk.Model and vosk.KaldiRecognizer and vosk.SetLogLevel)); "
                "print(sorted(m for m in ('requests', 'tqdm', 'srt', 'urllib.request') if m in sys.modules)); "
                "import urllib.request; print(callable(urllib.request.urlretrieve))")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.stdout.split(), ["True", "[]", "True"], out.stderr[-800:])


@unittest.skipUnless(MODEL and importlib.util.find_spec("vosk"), "set MURMUR_VOSK_MODEL to vosk-model-small-en-us-0.15")
class RealVoskTests(unittest.TestCase):
    def frames(self, pcm, engine):
        pcm = bytes(16000) + pcm + bytes(64000)
        pcm += bytes((-len(pcm)) % engine.frame_bytes)
        return [pcm[i:i + engine.frame_bytes] for i in range(0, len(pcm), engine.frame_bytes)]

    def test_full_grammar_builds_with_real_app_list(self):
        from murmur.vosk_engine import VoskEngine
        engine = VoskEngine(Parser().grammar(APPS), "hey jarvis", 0.5, MODEL)
        self.assertIn("firefox", engine.apps)
        self.assertIn("jarvis", engine._wake_forms)

    @unittest.skipUnless(FIXTURE, "set MURMUR_SPEECH_TEST_RAW to upstream goforward.raw")
    def test_real_recording_command_and_one_breath_wake(self):
        from murmur.vosk_engine import VoskEngine
        pcm = Path(FIXTURE).read_bytes()
        engine = VoskEngine({"phrases": ["go forward ten meters", "ten meters"]}, "go forward", 0.5, MODEL)
        engine.begin_command()
        results = [r for f in self.frames(pcm, engine) if (r := engine.feed(f))]
        self.assertEqual([r.text for r in results], ["go forward ten meters"])
        # Same recording as wake phrase "go forward" + command "ten meters" in one breath.
        engine.begin_wake()
        wake = next(r for f in self.frames(pcm, engine) if (r := engine.feed(f)))
        self.assertEqual(wake.kind, "wake")
        self.assertEqual(engine.after_wake().text, "ten meters")

    @unittest.skipUnless(FIXTURE, "supply the public fixture")
    def test_repeated_background_quiet_wake_without_room_noise(self):
        import array
        from murmur.vosk_engine import VoskEngine
        from murmur.voice import rms
        pcm = Path(FIXTURE).read_bytes()
        pcm += bytes((-len(pcm)) % 960)
        reversed_samples = array.array("h", pcm)
        reversed_samples.reverse()
        sequence = pcm + bytes(960) * 50 + reversed_samples.tobytes() + bytes(960) * 117
        engine = VoskEngine(Parser({"ten meters": "start recording"}).grammar(["firefox", "steam"]),
                            "go forward", .5, MODEL)
        self.addCleanup(engine.close)
        engine.begin_wake()
        events = []
        for _ in range(3):
            for offset in range(0, len(sequence), 960):
                frame = sequence[offset:offset + 960]
                if result := engine.feed(frame, quiet=rms(frame) < .005):
                    events.append((result.kind, result.text))
                    self.assertEqual(result.kind, "wake")
                    follow = engine.after_wake()
                    self.assertIsNotNone(follow)
                    events.append((follow.kind, follow.text))
                    engine.begin_wake()
        self.assertEqual(events, [("wake", "go forward"), ("command", "ten meters")] * 3)


class HyprlandLuaConfigTests(unittest.TestCase):
    """Hyprland with a Lua config rejects the old "focuswindow address:..." form
    (reported October 2026: "dispatch in lua is a shorthand for hl.dispatch(...)")."""

    def setUp(self):
        from murmur.hyprland import Hyprland
        Hyprland.syntax = None
        self.addCleanup(setattr, Hyprland, "syntax", None)
        self.Hyprland = Hyprland

    def run_with(self, responses, lua_config):
        from unittest.mock import MagicMock
        calls = []
        def fake_run(argv, **_kwargs):
            calls.append(argv)
            return MagicMock(returncode=0, stdout=responses.pop(0), stderr="")
        with patch("murmur.hyprland.subprocess.run", side_effect=fake_run), \
                patch.object(self.Hyprland, "_uses_lua_config", return_value=lua_config):
            self.Hyprland().dispatch("focus_address", "0x557e1234")
        return calls

    def test_lua_config_uses_lua_dispatchers(self):
        calls = self.run_with(["ok"], lua_config=True)
        self.assertEqual(calls, [["hyprctl", "dispatch", 'hl.dsp.focus({ window = "address:0x557e1234" })']])
        self.assertEqual(self.Hyprland.syntax, "lua")

    def test_falls_back_and_remembers_when_the_guess_is_wrong(self):
        error = "error: [string \"return hl.dispatch(focuswindow address:0x557e...\"]:1: ')' expected near 'address'"
        calls = self.run_with([error, "ok"], lua_config=False)
        self.assertEqual([c[2] for c in calls], ["focuswindow", 'hl.dsp.focus({ window = "address:0x557e1234" })'])
        self.assertEqual(self.Hyprland.syntax, "lua")

    def test_lua_forms_of_every_action(self):
        commands = self.Hyprland._commands
        self.assertEqual(commands("workspace", 3)["lua"], ['hl.dsp.focus({ workspace = "3" })'])
        self.assertEqual(commands("move_workspace", 2)["lua"], ['hl.dsp.window.move({ workspace = "2", follow = false })'])
        self.assertEqual(commands("fullscreen", None)["lua"], ['hl.dsp.window.fullscreen({ action = "set", mode = "fullscreen" })'])
        with self.assertRaises(ValueError):
            commands("focus_address", '0x1" }) hl.dsp.exec_cmd("rm')


class AliasAndWakeTests(unittest.TestCase):
    def test_alias_and_real_name_of_one_app_do_not_block_each_other(self):
        grammar = Parser().grammar(["stremio", "streamio", "steam"])
        grammar["app_ids"] = {"stremio": "com.stremio.Stremio.desktop", "streamio": "com.stremio.Stremio.desktop",
                              "steam": "steam.desktop"}
        candidates = match_candidates(grammar)
        self.assertEqual(closest_match("open stream io", candidates)[0], "open streamio")
        self.assertEqual(closest_match("open streemio", candidates)[0], "open streamio")
        # Without the mapping the two names look like two different apps, so a
        # pronunciation halfway between them runs nothing.
        del grammar["app_ids"]
        self.assertIsNone(closest_match("open streemio", match_candidates(grammar)))

    def test_single_word_wake_phrase(self):
        self.assertEqual(wake_variants("jarvis"), ["jarvis"])
        self.assertEqual(config.validate({"activation": {"wake_phrase": "jarvis"}})["activation"]["wake_phrase"], "jarvis")


class AliasCommandTests(unittest.TestCase):
    APPS = [{"id": "com.stremio.Stremio.desktop", "name": "Stremio"}, {"id": "steam.desktop", "name": "Steam"},
            {"id": "firefox.desktop", "name": "Firefox"}, {"id": "org.mozilla.firefox.desktop", "name": "Firefox"}]

    def test_find_app_by_name_id_or_unique_part(self):
        from murmur.cli import find_app
        self.assertEqual(find_app(self.APPS, "stremio")["id"], "com.stremio.Stremio.desktop")
        self.assertEqual(find_app(self.APPS, "steam.desktop")["id"], "steam.desktop")
        self.assertEqual(find_app(self.APPS, "strem")["id"], "com.stremio.Stremio.desktop")
        self.assertEqual(find_app(self.APPS, "firefox")["id"], "firefox.desktop")   # exact desktop ID wins
        with self.assertRaisesRegex(ValueError, "several apps"):
            find_app(self.APPS, "Firefox")
        with self.assertRaisesRegex(ValueError, "No installed app"):
            find_app(self.APPS, "photoshop")

    def test_alias_is_added_through_the_service(self):
        import argparse
        from murmur.cli import run_alias
        saved = {}
        def request(op, **kwargs):
            if op == "config":
                return {"ok": True, "config": {"aliases": {}, "activation": {"wake_phrase": "jarvis"}}}
            if op == "apps":
                return {"ok": True, "apps": self.APPS}
            saved.update(kwargs["config"])
            return {"ok": True}
        with patch("builtins.print") as out:
            run_alias(argparse.Namespace(name="Streamio", app="stremio", remove=False), request)
        self.assertEqual(saved["aliases"], {"streamio": "com.stremio.Stremio.desktop"})
        self.assertIn("jarvis, open streamio", out.call_args.args[0])


class UnmatchedSpeechTests(FakeVoskEngineTests):
    """Speech that matches no command is reported (so the user sees what was heard)."""

    def test_unmatched_command_is_reported(self):
        self.engine.begin_command()
        FakeRecognizer.script = [("grammar", words("[unk] [unk]", 0.9)), ("final", {"text": "play some music"})]
        self.assertIsNone(self.feed_until_result())
        self.assertEqual(self.engine.unmatched, "play some music")


if __name__ == "__main__":
    unittest.main()
