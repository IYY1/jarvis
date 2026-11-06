"""Small local speech adapter; capture, process lifetime and policy live in daemon.

PocketSphinx uses a US English acoustic model and a bounded finite grammar.
Posteriors are diagnostics, not calibrated probabilities that speech is correct.
The only retained audio is the Endpointer's 300 ms decision window.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Literal

from .commands import GrammarMatcher


_TOKEN = re.compile(r"^[a-z]+(?:['-][a-z]+)*$")


def normalize_phrase(text: str) -> str:
    """Normalize case/spacing without turning punctuation into grammar syntax."""
    return " ".join(text.lower().strip().rstrip(".!?").split())


@dataclass(frozen=True)
class Recognition:
    kind: Literal["wake", "command"]
    text: str
    confidence: float | None
    min_word_posterior: float | None = None
    heard: str | None = None          # free-form transcript, when closest-match chose the command
    closest_match: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class SpeechEngine:
    """VAD-gated keyword search followed by a separately armed command grammar.

    Feed exact ``frame_bytes`` chunks of mono signed 16-bit PCM at 16 kHz.
    Call ``begin_command`` after a wake event or push-to-talk request; feed until
    a result or the caller's timeout. Each command arm emits at most one result.
    Caller owns wall-clock timeout, cancellation, mute, and acceptance threshold.
    Unsupported dictionary tokens are excluded, never guessed as pronunciations.
    """

    sample_rate = 16000

    def __init__(self, grammar: dict | list[str], wake_phrase: str = "hey jarvis",
                 sensitivity: float = 0.5, wake_aliases=()):
        """``grammar`` is ``Parser.grammar()`` output, or a plain phrase list."""
        if not math.isfinite(sensitivity) or not 0 <= sensitivity <= 1:
            raise ValueError("Activation sensitivity must be between 0 and 1")
        try:
            from pocketsphinx import Decoder, Endpointer
        except ImportError as exc:
            raise RuntimeError("PocketSphinx is missing; install the project's voice dependencies") from exc
        self._endpointer_type = Endpointer
        self._vad_mode = max(0, min(3, round(3 * (1 - sensitivity))))
        # Smaller KWS likelihood-ratio thresholds accept more candidates.
        self.kws_threshold = 10 ** (-10 - 20 * sensitivity)
        self._decoder = Decoder(lm=None, samprate=self.sample_rate,
                                bestpath=True, loglevel="ERROR",
                                kws_threshold=self.kws_threshold)
        self.wake_phrase = normalize_phrase(wake_phrase)
        if isinstance(grammar, list):
            grammar = {"phrases": grammar}
        self.unsupported_phrases: list[str] = list(grammar.get("overflow", []))
        known = self._supported(grammar.get("phrases", []))
        apps = self._supported(grammar.get("apps", []), report="open {}")
        if not known and not apps:
            raise ValueError("No command phrases use supported dictionary words; configure phonetic aliases")
        verbs = self._supported(grammar.get("verbs", []), report=None)
        prefixes = self._supported(grammar.get("prefixes", []), report=None)
        suffixes = self._supported(grammar.get("suffixes", []), report=None)
        self.phrases = frozenset(known)
        self.apps = frozenset(apps)
        self._matcher = GrammarMatcher(known, apps if verbs else (), verbs)
        self._decoder.add_jsgf_string("commands", self.jsgf(known, apps, verbs, prefixes, suffixes))
        from .commands import all_wake_variants
        self._wake_forms = all_wake_variants({"wake_phrase": self.wake_phrase, "wake_aliases": list(wake_aliases)})
        missing_wake = sorted({w for name in self._wake_forms for w in self._missing_words(name)})
        if missing_wake:
            raise ValueError("Wake phrase contains unsupported dictionary words: " + ", ".join(missing_wake))
        if wake_aliases:
            import os
            import tempfile
            # Closed before PocketSphinx opens it by name: Windows refuses a
            # second open of a still-open delete-on-close temporary file.
            fd, keywords = tempfile.mkstemp(suffix=".kws", text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    file.write("\n".join(self._wake_forms) + "\n")
                self._decoder.add_kws("wake", keywords)
            finally:
                os.unlink(keywords)
        else:
            self._decoder.add_keyphrase("wake", self.wake_phrase)
        self._mode: Literal["wake", "command"] | None = None
        self._utterance = False
        self._utterance_bytes = 0
        self._reset_endpointer()

    def _supported(self, items, report="{}") -> list[str]:
        """Keep entries whose words are all in the dictionary; report the rest."""
        kept = []
        for item in sorted({normalize_phrase(x) for x in items if isinstance(x, str)}):
            if not self._missing_words(item):
                kept.append(item)
            elif report:
                self.unsupported_phrases.append(report.format(item))
        return kept

    @staticmethod
    def jsgf(phrases, apps=(), verbs=(), prefixes=(), suffixes=()) -> str:
        """Build the command grammar. Inputs must already be dictionary-checked
        lowercase words (``_TOKEN``), so no JSGF syntax can be injected.

        Plain commands stay top-level alternatives and the polite forms are
        added beside them (``<prefix> <core>``) rather than written as an
        optional ``[<prefix>] <core>``. Both accept the same sentences, but the
        optional form lowered PocketSphinx's posterior for plain commands
        (0.49 -> 0.33 on the upstream recording) and so made the score
        threshold reject more correct commands.
        """
        core = list(phrases)
        rules = []
        if apps and verbs:
            core.append("<verb> <app>")
            rules += ["<verb> = " + " | ".join(verbs) + ";", "<app> = " + " | ".join(apps) + ";"]
        top = list(core)
        if prefixes or suffixes:
            rules.append("<core> = " + " | ".join(core) + ";")
        if prefixes:
            top.append("<prefix> <core>")
            rules.append("<prefix> = " + " | ".join(prefixes) + ";")
        if suffixes:
            top += ["<core> <suffix>"] + (["<prefix> <core> <suffix>"] if prefixes else [])
            rules.append("<suffix> = " + " | ".join(suffixes) + ";")
        return " ".join(["#JSGF V1.0; grammar jarvis;",
                         "public <command> = " + " | ".join(top) + ";", *rules])

    def _missing_words(self, phrase: str) -> list[str]:
        if not phrase:
            return ["(empty phrase)"]
        return [word for word in phrase.split()
                if not _TOKEN.fullmatch(word) or self._decoder.lookup_word(word) is None]

    def _reset_endpointer(self) -> None:
        self._ep = self._endpointer_type(sample_rate=self.sample_rate,
                                        vad_mode=self._vad_mode,
                                        frame_length=0.03, window=0.3, ratio=0.9)
        self.frame_bytes = self._ep.frame_bytes
        self.frame_seconds = self._ep.frame_length

    def _end_utterance(self) -> None:
        if self._utterance:
            self._decoder.end_utt()
            self._utterance = False

    def _begin(self, mode: Literal["wake", "command"]) -> None:
        self._end_utterance()
        self._reset_endpointer()
        self._decoder.activate_search("wake" if mode == "wake" else "commands")
        self._mode = mode

    def begin_wake(self) -> None:
        self._begin("wake")

    def begin_command(self) -> None:
        self._begin("command")

    def cancel(self) -> None:
        """Discard the current utterance and the bounded VAD decision window."""
        self._end_utterance()
        self._mode = None
        self._reset_endpointer()

    def close(self) -> None:
        self.cancel()

    @staticmethod
    def _posterior(value) -> float | None:
        value = float(value)
        # PocketSphinx's fixed-base logmath can round unit posteriors to 1.0001.
        if not math.isfinite(value) or not 0 <= value <= 1.001:
            return None
        return min(1.0, value)

    def _result(self) -> Recognition | None:
        hypothesis = self._decoder.hyp()
        if hypothesis is None or not hypothesis.hypstr.strip():
            return None
        text = normalize_phrase(hypothesis.hypstr)
        if self._mode == "wake":
            # KWS acceptance uses kws_threshold, not an invented confidence score.
            return Recognition("wake", self.wake_phrase, None) if any(name in text for name in getattr(self, "_wake_forms", [self.wake_phrase])) else None
        if not self._matcher.accepts(text):
            return None
        posterior = self._posterior(hypothesis.prob)
        words = [self._posterior(seg.prob) for seg in self._decoder.seg()
                 if not seg.word.startswith(("<", "["))]
        valid = [word for word in words if word is not None]
        return Recognition("command", text, posterior, min(valid) if valid else None)

    def feed(self, pcm: bytes) -> Recognition | None:
        """Process one frame. No audio or transcript is saved to disk."""
        if len(pcm) != self.frame_bytes:
            raise ValueError(f"Expected exactly {self.frame_bytes} PCM bytes, got {len(pcm)}")
        if self._mode is None:
            return None
        speech = self._ep.process(pcm)
        if speech is None:
            return None
        if not self._utterance:
            self._decoder.start_utt()
            self._utterance = True
            self._utterance_bytes = 0
        self._utterance_bytes += len(speech)
        if self._utterance_bytes > self.sample_rate * 2 * 10:
            # Bound decoder history even under continuous game dialogue/noise.
            self._end_utterance()
            self._reset_endpointer()
            if self._mode == "command":
                self._mode = None
            return None
        self._decoder.process_raw(speech, False, False)
        if self._mode == "wake":
            result = self._result()
            if result:
                self._end_utterance()
                self._mode = None
                return result
        if not self._ep.in_speech:
            self._end_utterance()
            result = self._result()
            if self._mode == "command" and result is not None:
                self._mode = None  # one recognized result per command arm
            return result
        return None
