import importlib.util
import os
from pathlib import Path
import unittest

from jarvis.speech import Recognition, SpeechEngine, normalize_phrase


class SpeechContractTests(unittest.TestCase):
    def test_normalize_phrase(self):
        self.assertEqual(normalize_phrase('  OPEN  Firefox! '), 'open firefox')
        self.assertEqual(normalize_phrase('open (firefox)'), 'open (firefox)')

    def test_posterior_quantization_and_invalid_values(self):
        self.assertEqual(SpeechEngine._posterior(1.0001), 1.0)
        self.assertIsNone(SpeechEngine._posterior(float('nan')))
        self.assertIsNone(SpeechEngine._posterior(-1))
        self.assertIsNone(SpeechEngine._posterior(2))

    def test_serializable_result(self):
        self.assertEqual(Recognition('wake', 'hey jarvis', None).to_dict()['kind'], 'wake')


@unittest.skipUnless(importlib.util.find_spec('pocketsphinx'), 'optional voice dependency is not installed')
class PocketSphinxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = SpeechEngine(['open firefox', 'go forward ten meters', 'open stremio'])

    def tearDown(self):
        self.engine.close()

    def test_unknown_app_requires_pronounceable_alias(self):
        self.assertIn('open stremio', self.engine.unsupported_phrases)
        self.assertNotIn('open stremio', self.engine.phrases)

    def test_grammar_injection_is_rejected(self):
        engine = SpeechEngine(['open firefox', 'open firefox; public <other> = launch firefox'])
        self.assertEqual(len(engine.phrases), 1)
        engine.close()

    def test_silence_does_not_reach_keyword_decoder(self):
        self.engine.begin_wake()
        baseline_frames = self.engine._decoder.n_frames()
        for _ in range(100):
            self.assertIsNone(self.engine.feed(bytes(self.engine.frame_bytes)))
        self.assertEqual(self.engine._decoder.n_frames(), baseline_frames)

    def test_frame_size_is_enforced(self):
        with self.assertRaises(ValueError):
            self.engine.feed(b'\0\0')

    def test_wake_alias_keyword_file_is_loaded_then_removed(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder, patch.object(tempfile, 'tempdir', folder):
            engine = SpeechEngine(['open firefox'], wake_phrase='hey computer', wake_aliases=['jarvis'])
            try:
                self.assertIn('jarvis', engine._wake_forms)
                self.assertEqual(os.listdir(folder), [])
            finally:
                engine.close()

    def test_cancel_disarms(self):
        self.engine.begin_command()
        self.engine.cancel()
        self.assertIsNone(self.engine.feed(bytes(self.engine.frame_bytes)))
        self.assertIsNone(self.engine._mode)

    def test_upstream_keyword_fixture_if_provided(self):
        path = os.environ.get('JARVIS_SPEECH_TEST_RAW')
        if not path:
            self.skipTest('set JARVIS_SPEECH_TEST_RAW to upstream goforward.raw (not bundled)')
        engine = SpeechEngine(['open firefox'], 'go forward', 0.5)
        engine.begin_wake()
        pcm = Path(path).read_bytes() + bytes(32000)
        events = [result for offset in range(0, len(pcm) - engine.frame_bytes + 1, engine.frame_bytes)
                  if (result := engine.feed(pcm[offset:offset + engine.frame_bytes]))]
        self.assertEqual([event.kind for event in events], ['wake'])
        self.assertIsNone(events[0].confidence)  # KWS uses its configured likelihood ratio
        engine.close()

    def test_wake_then_valid_command_survives_unmatched_tail(self):
        path = os.environ.get('JARVIS_SPEECH_TEST_RAW')
        if not path:
            self.skipTest('set JARVIS_SPEECH_TEST_RAW to upstream goforward.raw (not bundled)')
        engine = SpeechEngine(['open firefox', 'go forward ten meters'], 'go forward', 0.5)
        engine.begin_wake()
        utterance = Path(path).read_bytes() + bytes(32000)
        pcm = utterance + utterance
        events = []
        for offset in range(0, len(pcm) - engine.frame_bytes + 1, engine.frame_bytes):
            result = engine.feed(pcm[offset:offset + engine.frame_bytes])
            if result:
                events.append(result)
                if result.kind == 'wake':
                    engine.begin_command()
        self.assertEqual([event.kind for event in events], ['wake', 'command'])
        self.assertEqual(events[-1].text, 'go forward ten meters')
        engine.close()

    def test_keyword_search_restarts_after_unmatched_utterance(self):
        path = os.environ.get('JARVIS_SPEECH_TEST_RAW')
        if not path:
            self.skipTest('set JARVIS_SPEECH_TEST_RAW to upstream goforward.raw (not bundled)')
        engine = SpeechEngine(['open firefox'], 'go forward', 0.5)
        engine.begin_wake()
        raw = Path(path).read_bytes()
        pcm = raw[16000 * 2:] + bytes(32000) + raw + bytes(32000)
        events = [result for offset in range(0, len(pcm) - engine.frame_bytes + 1, engine.frame_bytes)
                  if (result := engine.feed(pcm[offset:offset + engine.frame_bytes]))]
        self.assertEqual([event.kind for event in events], ['wake'])
        engine.close()

    def test_upstream_speech_fixture_if_provided(self):
        path = os.environ.get('JARVIS_SPEECH_TEST_RAW')
        if not path:
            self.skipTest('set JARVIS_SPEECH_TEST_RAW to upstream goforward.raw (not bundled)')
        pcm = Path(path).read_bytes() + bytes(32000)
        self.engine.begin_command()
        events = [result for offset in range(0, len(pcm) - self.engine.frame_bytes + 1,
                                             self.engine.frame_bytes)
                  if (result := self.engine.feed(pcm[offset:offset + self.engine.frame_bytes]))]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].text, 'go forward ten meters')
        self.assertIsNotNone(events[0].confidence)
        self.assertIsNotNone(events[0].min_word_posterior)
        self.assertGreater(events[0].confidence, 0)


if __name__ == '__main__':
    unittest.main()
