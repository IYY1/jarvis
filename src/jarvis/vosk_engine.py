"""Vosk (Kaldi) recognizer: better accent tolerance than PocketSphinx, still offline.

Same interface as ``speech.SpeechEngine`` so the capture worker can use either.

* Wake: a recognizer restricted to the wake phrase and its variants plus
  ``[unk]`` (anything else), so ordinary speech is rejected instead of being
  forced onto the wake phrase.
* Command, strict pass: a recognizer restricted to the command grammar plus
  ``[unk]``. A result containing ``[unk]`` is rejected.
* Command, closest-match pass: an open-vocabulary recognizer on the *same*
  model (no extra model memory) transcribes the same audio. If the strict pass
  rejected it, ``commands.closest_match`` may pick the nearest command, only
  with a high similarity and a clear lead over every different command.
* Idle cost: frames below the energy gate are not decoded at all, except a
  short pre-roll (so the soft start of "hey" is not lost) and a hang-over
  (so the recognizer sees the silence that ends an utterance).
"""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import sys
import types

from .commands import GREETINGS, GrammarMatcher, closest_match, command_wording, match_candidates, all_wake_variants
from .speech import Recognition, normalize_phrase, _TOKEN

SAMPLE_RATE = 16000
FRAME_BYTES = 960            # 30 ms of 16-bit mono PCM, same as the PocketSphinx path
PREROLL_FRAMES = 10          # 300 ms of audio kept from before speech starts
HANGOVER_FRAMES = 40         # keep decoding 1.2 s after the last loud frame
MAX_UTTERANCE_BYTES = SAMPLE_RATE * 2 * 10
MIN_SPEECH_FRAMES = 8       # 240 ms of speech before an end can be detected
END_QUIET_FRAMES = 20       # 600 ms below the energy gate ends a command
STRICT_MIN_CONFIDENCE = 0.6  # below this, prefer a clear closest-match of the free transcript
CROSS_CHECK_OVERRIDE = 0.9   # free transcript overrides a disagreeing grammar result only above this


def _import_vosk():
    """Import vosk without the helpers its package imports for model downloads and
    subtitles (requests, tqdm, srt, urllib.request: ~14 MiB in the voice worker).
    They are only called when a model is fetched by name; Jarvis always passes a
    path. Empty stand-ins satisfy the imports and are removed right afterwards."""
    if "vosk" in sys.modules:
        return sys.modules["vosk"]
    stand_ins = {}
    for name, attribute in (("requests", None), ("srt", None), ("tqdm", "tqdm"), ("urllib.request", "urlretrieve")):
        if name not in sys.modules:
            stand_ins[name] = types.ModuleType(name)
            if attribute:
                setattr(stand_ins[name], attribute, None)
    sys.modules.update(stand_ins)
    try:
        import vosk
    finally:
        for name, module in stand_ins.items():
            if sys.modules.get(name) is module:
                del sys.modules[name]
    return vosk


class VoskEngine:
    sample_rate = SAMPLE_RATE
    frame_bytes = FRAME_BYTES
    wants_raw_audio = True    # the worker passes real audio plus a quiet flag
    name = "vosk"

    def __init__(self, grammar: dict | list[str], wake_phrase: str, sensitivity: float = 0.5,
                 model_path: str | Path = "", fuzzy: bool = True, transcriber=None, wake_aliases=()):
        if not 0 <= sensitivity <= 1:
            raise ValueError("Activation sensitivity must be between 0 and 1")
        try:
            vosk = _import_vosk()
        except ImportError as exc:
            raise RuntimeError("Vosk is not installed; run: pip install vosk==0.3.45") from exc
        KaldiRecognizer, Model, SetLogLevel = vosk.KaldiRecognizer, vosk.Model, vosk.SetLogLevel
        from .models import valid_model
        path = Path(model_path)
        if not valid_model(path):
            raise RuntimeError(f"Vosk model missing at {path}. Install it with: jarvis install-model")
        SetLogLevel(-1)
        self._model = Model(str(path))
        if isinstance(grammar, list):
            grammar = {"phrases": grammar}
        self.sensitivity = sensitivity
        self.unsupported_phrases: list[str] = list(grammar.get("overflow", []))

        known = self._supported(grammar.get("phrases", []))
        apps = self._supported(grammar.get("apps", []), report="open {}")
        verbs = self._supported(grammar.get("verbs", []), report=None)
        self._close_verbs = tuple(grammar.get("close_verbs", ["close"]))
        prefixes = self._supported(grammar.get("prefixes", []), report=None)
        if not known and not apps:
            raise ValueError("No command phrases use words the Vosk model knows")
        self.phrases = frozenset(known)
        self.apps = frozenset(apps)
        # Names the model can't spell are often two words it can:
        # "streamio" -> "stream io", "palworld" -> "pal world", "cuphead" -> "cup head".
        self._sounds_like = self._sound_alikes(grammar.get("apps", []), set(apps))
        spoken_apps = list(apps) + list(self._sounds_like)
        self.unsupported_phrases = [p for p in self.unsupported_phrases
                                    if p.removeprefix("open ") not in self._sounds_like.values()]
        # The matcher checks text after _canonical(), so it knows the real names.
        self._matcher = GrammarMatcher(self.phrases, (spoken_apps + list(self._sounds_like.values())) if verbs else (), verbs)
        from .phrases import Rules
        self._rules = Rules(grammar.get("launches"))
        blocked = set(grammar.get("blocked_apps", []))
        self._verbs = sorted(verbs, key=len, reverse=True)
        full = list(known) + [f"{verb}{article} {app}" for verb in verbs for app in spoken_apps
                              if self._sounds_like.get(app, app) not in blocked or verb in ("focus", "switch to", *self._close_verbs)
                              for article in ("", " the")]
        for p, alias in self._rules.launches.items():
            article = p[:-len(alias)] + "the " + alias
            if not self._missing_words(article):
                full.append(article)
            for spoken, original in self._sounds_like.items():
                if original == alias and p.endswith(alias):
                    full.extend((p[:-len(alias)] + spoken, p[:-len(alias)] + "the " + spoken))
        # Polite forms only for the shorter built-in phrases, to keep the
        # strict grammar small; app commands get them via closest-match.
        full += [f"{prefix} {phrase}" for prefix in prefixes for phrase in known]
        command_phrases = sorted(set(full))

        # Closest-match may also pick apps whose names the model can't spell
        # (e.g. "stremio" heard as "stream io"), which the strict grammar can't.
        all_apps = [normalize_phrase(a) for a in grammar.get("apps", [])]
        self._candidates = match_candidates({**grammar, "apps": all_apps})
        self._fuzzy = fuzzy
        if fuzzy:
            # Those apps are now reachable, so don't report them as unsupported.
            reachable = {f"open {a}" for a in all_apps}
            self.unsupported_phrases = [p for p in self.unsupported_phrases if p not in reachable]

        self.wake_phrase = normalize_phrase(wake_phrase)
        self._wake_forms = [w for w in all_wake_variants({"wake_phrase": self.wake_phrase, "wake_aliases": list(wake_aliases)}) if not self._missing_words(w)]
        if not self._wake_forms:
            raise ValueError("Wake phrase uses words the Vosk model doesn't know; choose another")
        words = self.wake_phrase.split()
        # The distinctive part ("jarvis" in "hey jarvis"). Greetings are short
        # and often misheard ("hey" -> "the", "ai"), so only the name must match.
        self._wake_name = words[1:] if len(words) > 1 and words[0] in GREETINGS else words
        self._wake_names = sorted({tuple(w.split()[1:] if len(w.split()) > 1 and w.split()[0] in GREETINGS else w.split()) for w in self._wake_forms}, key=lambda n: -len(n))

        # The wake grammar also knows the command words, so "hey jarvis open
        # firefox" in one breath is decoded as words rather than forced into
        # [unk] (which made the wake words themselves less likely to match).
        wake_grammar = self._wake_forms + [f"{' '.join(name)} {c}" for name in self._wake_names for c in command_phrases]
        self._wake = KaldiRecognizer(self._model, SAMPLE_RATE, json.dumps(wake_grammar + ["[unk]"]))
        self._wake.SetWords(True)
        self._command = KaldiRecognizer(self._model, SAMPLE_RATE, json.dumps(command_phrases + ["[unk]"]))
        self._command.SetWords(True)
        self._open = KaldiRecognizer(self._model, SAMPLE_RATE) if fuzzy else None
        self._mode: str | None = None
        self._preroll: deque[bytes] = deque(maxlen=PREROLL_FRAMES)
        self._hangover = 0
        self._utterance_bytes = 0
        self._open_text: list[str] = []
        # Set when speech was heard during a command but matched nothing, so
        # the user can see *what* was heard instead of a silent wait.
        self.unmatched: str | None = None
        # Audio of the current wake utterance, so "hey jarvis open firefox"
        # said in one breath can still run the command after the wake words.
        self._wake_audio = bytearray()
        # Vosk word times count from the recognizer's creation (Reset() does
        # not restart them), so track every byte ever fed to it.
        self._stream_bytes = 0       # bytes ever fed to the wake recognizer
        self._wake_audio_start = 0   # stream offset (bytes) where _wake_audio begins
        self._pending: Recognition | None = None
        # Optional stronger model (transcriber.Transcriber) that writes down the
        # command audio after Vosk finds where it ends. Better with accents.
        self._transcriber = transcriber
        self._command_audio = bytearray()
        self._speech_frames = 0
        self._quiet_run = 0

    # -- vocabulary -----------------------------------------------------------
    def _split(self, word: str) -> list[str] | None:
        """Split an unknown word into two known words, or None.

        Kept strict so it only produces plausible spoken forms: the first part
        has at least 3 letters, the second at least 2 (or 3 if the first part
        is short). "streamio" -> "stream io", "cuphead" -> "cup head",
        but not "btop" -> "bt op" or "lact" -> "la ct".
        """
        if self._model.vosk_model_find_word(word) >= 0:
            return [word]
        for cut in range(len(word) - 2, 2, -1):     # longest first part first
            head, tail = word[:cut], word[cut:]
            if len(tail) < (2 if len(head) >= 5 else 3):
                continue
            if self._model.vosk_model_find_word(head) >= 0 and self._model.vosk_model_find_word(tail) >= 0:
                return [head, tail]
        return None

    def _sound_alikes(self, names, known: set[str]) -> dict[str, str]:
        result = {}
        for name in {normalize_phrase(n) for n in names if isinstance(n, str)} - known:
            if not all(_TOKEN.fullmatch(w) for w in name.split()):
                continue
            pieces = [self._split(w) for w in name.split()]
            if all(pieces):
                spoken = " ".join(" ".join(p) for p in pieces)
                if spoken != name and spoken not in known:
                    result.setdefault(spoken, name)
        return result

    def _canonical(self, text: str) -> str:
        """Turn a sound-alike back into the app's name: "open stream io" -> "open streamio"."""
        rules = getattr(self, "_rules", None)
        if rules:
            exact = rules.canonical(text)
            if exact:
                return exact
            for p, alias in rules.launches.items():
                for spoken, original in self._sounds_like.items():
                    if original == alias and p.endswith(alias) and text in (p[:-len(alias)] + spoken, p[:-len(alias)] + "the " + spoken):
                        return p
        for verb in self._verbs:
            if text.startswith(verb + " the "):
                text = verb + " " + text[len(verb + " the "):]
            if text.startswith(verb + " ") and text[len(verb) + 1:] in self._sounds_like:
                return f"{verb} {self._sounds_like[text[len(verb) + 1:]]}"
        return text

    def _missing_words(self, phrase: str) -> list[str]:
        if not phrase:
            return ["(empty phrase)"]
        return [w for w in phrase.split()
                if not _TOKEN.fullmatch(w) or self._model.vosk_model_find_word(w) < 0]

    def _supported(self, items, report="{}") -> list[str]:
        kept = []
        for item in sorted({normalize_phrase(x) for x in items if isinstance(x, str)}):
            if not self._missing_words(item):
                kept.append(item)
            elif report:
                self.unsupported_phrases.append(report.format(item))
        return kept

    # -- session control ------------------------------------------------------
    def _reset(self) -> None:
        for recognizer in (self._wake, self._command, self._open):
            if recognizer is not None:
                recognizer.Reset()
        self._preroll.clear()
        self._hangover = 0
        self._utterance_bytes = 0
        self._open_text = []
        self._wake_audio = bytearray()
        self._wake_audio_start = self._stream_bytes
        self._command_audio = bytearray()
        self._speech_frames = 0
        self._quiet_run = 0

    def begin_wake(self) -> None:
        self._reset()
        self._mode = "wake"

    def begin_command(self) -> None:
        self._reset()
        self._mode = "command"

    def cancel(self) -> None:
        self._reset()
        self._mode = None

    def close(self) -> None:
        self.cancel()

    # -- audio ----------------------------------------------------------------
    def feed(self, pcm: bytes, quiet: bool = False) -> Recognition | None:
        if len(pcm) != FRAME_BYTES:
            raise ValueError(f"Expected exactly {FRAME_BYTES} PCM bytes, got {len(pcm)}")
        if self._mode is None:
            return None
        if quiet and self._hangover == 0:
            self._preroll.append(pcm)       # idle: remember, don't decode
            return None
        frames = []
        if not quiet:
            if self._hangover == 0:
                frames.extend(self._preroll)  # speech starts: include its soft onset
                self._preroll.clear()
            self._hangover = HANGOVER_FRAMES
        else:
            self._hangover -= 1
        frames.append(pcm)
        for frame in frames:
            result = self._decode(frame)
            if result is not None:
                return result
        if quiet and self._hangover == 0 and self._mode == "wake":
            # Once quiet frames stop reaching Vosk, discard any unfinished
            # background utterance so the next wake starts fresh. Do not force
            # a final hypothesis into a wake; command sessions keep their tail.
            self._reset()
        if self._mode == "command" and self._transcriber is not None:
            # Our own end-of-command check: Vosk may keep waiting when a TV or
            # game voice continues in the background after you stop talking.
            if quiet:
                self._quiet_run += 1
            else:
                self._speech_frames += 1
                self._quiet_run = 0
            if self._speech_frames >= MIN_SPEECH_FRAMES and self._quiet_run >= END_QUIET_FRAMES:
                return self.flush()
        return None

    def flush(self) -> Recognition | None:
        """Finish a command now (end of speech, or the caller's deadline)."""
        if self._mode != "command" or not self._command_audio:
            return None
        return self._command_result(json.loads(self._command.FinalResult()))

    def _decode(self, pcm: bytes) -> Recognition | None:
        self._utterance_bytes += len(pcm)
        if self._utterance_bytes > MAX_UTTERANCE_BYTES:
            # Bound decoder history under continuous game dialogue/noise.
            mode = self._mode
            self._reset()
            self._mode = mode
            return None
        if self._mode == "wake":
            self._wake_audio += pcm
            self._stream_bytes += len(pcm)
            if self._wake.AcceptWaveform(pcm):
                self._utterance_bytes = 0
                audio, start = bytes(self._wake_audio), self._wake_audio_start
                self._wake_audio = bytearray()
                self._wake_audio_start = self._stream_bytes
                return self._wake_result(json.loads(self._wake.Result()), audio, start)
            return None
        self._command_audio += pcm
        if self._open is not None and self._open.AcceptWaveform(pcm):
            self._open_text.append(json.loads(self._open.Result()).get("text", ""))
        if self._command.AcceptWaveform(pcm):
            self._utterance_bytes = 0
            return self._command_result(json.loads(self._command.Result()))
        return None

    @staticmethod
    def _confidence(result: dict) -> float | None:
        confs = [float(w["conf"]) for w in result.get("result", []) if "conf" in w]
        return min(confs) if confs else None

    def _wake_result(self, result: dict, audio: bytes = b"", start: int = 0) -> Recognition | None:
        """Wake when the wake *name* is among the first words of an utterance.

        "hey jarvis", "hi jarvis", "jarvis" and a misheard greeting ("the
        jarvis") all count; "jarvis" later in a sentence does not. Whatever
        follows the name in the same breath is treated as the command.
        """
        words = [w for w in result.get("result", []) if w.get("word")]
        spoken = [w["word"] for w in words]
        match = next(((i, len(name)) for i in range(3) for name in getattr(self, "_wake_names", [self._wake_name]) if spoken[i:i + len(name)] == list(name)), None)
        if match is None:
            return None
        position, size = match
        name = words[position:position + size]
        confs = [float(w["conf"]) for w in name if "conf" in w]
        # Higher sensitivity accepts less certain wake words (0.5 -> 0.4).
        if confs and min(confs) < 0.8 * (1 - self.sensitivity):
            return None
        rest = words[position + size:]
        self._pending = None
        if rest:
            text = self._canonical(" ".join(w["word"] for w in rest))
            confidence = min((float(w["conf"]) for w in rest if "conf" in w), default=None)
            cut = int(float(name[-1].get("end", 0)) * SAMPLE_RATE) * 2 - start
            tail = audio[max(0, cut - cut % 2):]
            if "[unk]" not in text and self._matcher.accepts(text) and \
                    (confidence is None or confidence >= STRICT_MIN_CONFIDENCE):
                # Same cross-check as a command said after a pause.
                self._pending = self._command_from_audio(tail, {"text": text, "result": rest})
            elif audio:
                # Re-decode just the audio after the wake name with the command
                # grammar and the free transcript (closest-match).
                self._pending = self._command_from_audio(tail)
        self._reset()
        self._mode = None
        return Recognition("wake", self.wake_phrase, None)

    def after_wake(self) -> Recognition | None:
        """A command that followed the wake words without a pause, if any."""
        pending, self._pending = self._pending, None
        return pending

    def _command_from_audio(self, audio: bytes, strict: dict | None = None) -> Recognition | None:
        """``strict``: the grammar result for ``audio`` when the wake decoder already has it."""
        if strict is None and len(audio) < SAMPLE_RATE * 2 // 5:   # under 0.2 s: not a command
            return None
        self._command.Reset()
        self._open_text = []
        self._command_audio = bytearray(audio)
        if self._open is not None:
            self._open.Reset()
            self._open.AcceptWaveform(audio)
        if strict is None:
            self._command.AcceptWaveform(audio)
            strict = json.loads(self._command.FinalResult())
        return self._command_result(strict)

    def _command_result(self, result: dict) -> Recognition | None:
        """Decide between the strict grammar result and the free transcript.

        Vosk's grammar mode can be confidently wrong (it only knows the listed
        words, so "discord" may come out as "deadlock"). The free transcript of
        the same audio is used as a cross-check: when the two point to
        *different* commands, the free transcript wins only if it is a very
        close match, otherwise nothing runs. Doing nothing is safer than doing
        the wrong thing.
        """
        text = self._canonical(" ".join(result.get("text", "").split()))
        heard = " ".join(t for t in self._open_text if t)
        if self._open is not None:
            heard = " ".join(filter(None, [heard, json.loads(self._open.FinalResult()).get("text", "")]))
            self._open.Reset()
        self._open_text = []
        audio, self._command_audio = bytes(self._command_audio), bytearray()
        self._speech_frames = self._quiet_run = 0
        heard_match = closest_match(heard, self._candidates, exact_only=not self._fuzzy, close_verbs=self._close_verbs) if heard and self._candidates else None
        if self._transcriber is not None and audio:
            # The stronger model's transcript comes first; the small model's
            # free transcript is the fallback.
            accurate = normalize_phrase(self._transcriber.transcribe(audio))
            accurate_match = closest_match(accurate, self._candidates, exact_only=not self._fuzzy, close_verbs=self._close_verbs) if accurate and self._candidates else None
            if accurate_match or not heard_match:
                heard, heard_match = accurate or heard, accurate_match
        confidence = self._confidence(result)
        if text and "[unk]" not in text and self._matcher.accepts(text):
            strict_intent = self._candidates.get(command_wording(text)) if self._candidates else None
            if heard and strict_intent and strict_intent.startswith("close_app:") and (
                not heard_match or self._candidates[heard_match[0]] != strict_intent
            ):
                self.unmatched = heard or text
                return None
            if heard_match and strict_intent and self._candidates[heard_match[0]] != strict_intent:
                if heard_match[1] < CROSS_CHECK_OVERRIDE:
                    self.unmatched = heard or text
                    return None          # the two passes disagree: do nothing
            elif confidence is None or confidence >= STRICT_MIN_CONFIDENCE:
                self._mode = None
                if heard_match and strict_intent and confidence is not None:   # both passes chose it
                    return Recognition("command", text, max(confidence, heard_match[1]), confidence)
                return Recognition("command", text, confidence, confidence)
        if heard_match:
            self._mode = None
            phrase, score = heard_match
            return Recognition("command", phrase, score, None, heard=heard, closest_match=True)
        self.unmatched = heard or text.replace("[unk]", "…").strip() or None
        return None  # stay armed until the caller's deadline
