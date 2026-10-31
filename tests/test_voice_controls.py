"""Configured voice controls and wake names."""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

from murmur import config
from murmur.commands import CommandError, Parser, all_wake_variants, closest_match, match_candidates
from tests.test_rust_controller import BINARY, policies
from tests.test_voice_upgrade import FakeModel, FakeRecognizer, words


def settings():
    return config.validate({
        'activation': {'wake_phrase': 'alfred', 'wake_aliases': ['jarvis']},
        'aliases': {'firefox': 'firefox.desktop', 'video': 'video.desktop'},
        'app_prefixes': {'firefox': '', 'video': 'watch'}})


class VoiceControlsTests(unittest.TestCase):
    def setUp(self):
        self.c = settings()
        self.parser = Parser.from_config(self.c)

    def test_bare_and_custom_launch_words_remain_allowlisted(self):
        for phrase, target in [('firefox', 'firefox'), ('watch video', 'video'), ('watch the video', 'video'),
                               ('please watch the video', 'video'), ('open the steam', 'steam'), ('focus the firefox', 'firefox')]:
            self.assertEqual(self.parser.parse(phrase).target, target)
        for phrase in ('open firefox', 'launch video', 'arbitrary executable'):
            with self.assertRaises(CommandError, msg=phrase):
                self.parser.parse(phrase)
        # Legacy settings keep all existing verbs and fixed phrases.
        self.assertEqual(Parser().parse('open the video').target, 'video')
        self.assertEqual(Parser().parse('open the video recorder').action, 'open_recorder')

    def test_launch_word_candidates_are_exact(self):
        candidates = match_candidates(self.parser.grammar(['firefox', 'video', 'steam']))
        self.assertIn('firefox', candidates)
        self.assertNotIn('open firefox', candidates)
        self.assertIsNone(closest_match('fire fox', candidates, exact_only=True))

    def test_invalid_configuration_cannot_disable_startup(self):
        patches = [ {'activation': {'wake_aliases': ['hey Alfred']}}, {'activation': {'wake_aliases': 'jarvis'}},
                    {'app_prefixes': {'missing': ''}},
                    {'aliases': {'listening': 'a'}, 'app_prefixes': {'listening': 'stop'}} ]
        for update in patches:
            case = config.merged(self.c, update)
            with self.assertRaises(ValueError, msg=update):
                config.validate(case)
        self.assertEqual(all_wake_variants(self.c['activation']), ['alfred', 'jarvis'])
        self.assertEqual(config.validate({})['activation']['wake_aliases'], [])

    def test_each_wake_name_can_carry_a_bare_app_command(self):
        from murmur.vosk_engine import VoskEngine
        from murmur import models  # Import before patch.dict restores sys.modules.
        fake = types.SimpleNamespace(Model=FakeModel, KaldiRecognizer=FakeRecognizer, SetLogLevel=lambda _: None)
        with patch.dict(sys.modules, vosk=fake), patch.object(models, 'valid_model', return_value=True):
            engine = VoskEngine(self.parser.grammar(['firefox', 'video']), 'alfred', model_path='/model', wake_aliases=['jarvis'])
        FakeRecognizer.script = []
        for wake in ('alfred', 'jarvis'):
            engine.begin_wake()
            self.assertIsNotNone(engine._wake_result(words(wake + ' firefox')))
            self.assertEqual(engine.after_wake().text, 'firefox')
        engine.begin_wake()
        self.assertIsNone(engine._wake_result(words('firefox')))
        self.assertIsNone(engine._wake_result(words('someone over there mentioned jarvis firefox')))
        engine.close()


@unittest.skipUnless(BINARY.is_file(), 'build the Rust controller')
class RustVoiceControlsTests(unittest.TestCase):
    def test_parser_and_grammar_match_python_for_configured_controls(self):
        c = settings()
        parser = Parser.from_config(c)
        grammar = parser.grammar(['firefox', 'video', 'steam'])
        phrases = grammar['phrases'] + ['watch the video', 'please watch the video', 'open the steam', 'open firefox']
        actual = policies([{'op': 'parse', 'text': p, 'config': c} for p in phrases])
        for phrase, result in zip(phrases, actual, strict=True):
            try:
                expected = dataclasses.asdict(parser.parse(phrase))
            except ValueError:
                self.assertFalse(result['ok'], phrase)
            else:
                self.assertEqual(result.get('result', {}).get('intent'), expected, (phrase, result))
        actual = policies([{'op': 'grammar', 'apps': ['firefox', 'video', 'steam'], 'config': c}])[0]
        self.assertEqual(actual['result']['grammar'], grammar)


@unittest.skipUnless(os.environ.get('MURMUR_VOSK_MODEL') and os.environ.get('MURMUR_SPEECH_TEST_RAW'), 'provide public audio and local Vosk model')
class RealWakeAliasTests(unittest.TestCase):
    def test_secondary_wake_name_and_same_breath_command_match_python(self):
        from murmur.native_worker import prepare
        from murmur.vosk_engine import VoskEngine
        from murmur.voice import rms
        c = settings()
        c['activation']['wake_aliases'] = ['go forward']
        c['commands'] = {'go forward ten meters': 'cancel', 'ten meters': 'cancel'}
        c['recognition'].update(accurate=False, model_path=os.environ['MURMUR_VOSK_MODEL'])
        grammar = Parser.from_config(c).grammar(['firefox', 'video'])
        fixture = os.environ['MURMUR_SPEECH_TEST_RAW']
        audio = Path(fixture).read_bytes()
        audio += bytes(((len(audio) + 64000 + 959) // 960) * 960 - len(audio))
        engine = VoskEngine(grammar, 'alfred', model_path=c['recognition']['model_path'], wake_aliases=['go forward'])
        engine.begin_wake()
        expected = []
        for offset in range(0, len(audio), 960):
            frame = audio[offset:offset + 960]
            event = engine.feed(frame, quiet=rms(frame) < .005)
            if event:
                expected.append(event.to_dict())
                if follow := engine.after_wake():
                    expected.append(follow.to_dict())
                break
        engine.close()
        self.assertEqual([e['kind'] for e in expected], ['wake', 'command'])
        with patch.dict(os.environ, MURMUR_VOICE_WORKER='rust'):
            argv, payload, env = prepare(c, 'once', grammar)
        request = json.dumps(payload) + '\n' + json.dumps({'path': fixture, 'session': 'wake'}) + '\n'
        out = subprocess.run(argv + ['--engine'], input=request, env=env, text=True, capture_output=True, timeout=30, check=True)
        events = [json.loads(line) for line in out.stdout.splitlines()][1]['events']
        self.assertEqual(events, expected)
