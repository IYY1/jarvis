"""Deterministic, allowlisted speech parsing. No shell or free-form dispatch.

One set of rule tables drives both directions: ``Parser.grammar`` builds what
the recognizer may hear, and ``Parser.parse`` turns that text into an intent.
Keeping them together means every documented phrase is both speakable and
understood (see ``GrammarMatcher`` and the consistency tests).
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


# Arabic spelling folded the way speech transcripts vary: no diacritics,
# tatweel or hamza seats, one alef, Western digits (see phrases.translate_arabic).
_ARABIC_MARKS = re.compile("[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640\u0621]")
_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي", "ئ": "ي", "ؤ": "و",
                              **{chr(0x660 + i): str(i) for i in range(10)},
                              **{chr(0x6F0 + i): str(i) for i in range(10)}})


def normalize(text: str) -> str:
    if not isinstance(text, str) or len(text) > 256:
        raise ValueError("Phrase must be at most 256 characters")
    text = _ARABIC_MARKS.sub("", unicodedata.normalize("NFKC", text)).translate(_ARABIC_FOLD)
    phrase = " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())
    return re.sub(r"\bfullscreen\b", "full screen", phrase)


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class Intent:
    action: str
    target: str | int | dict | None = None


NUMBERS = dict(zip("one two three four five six seven eight nine ten".split(), range(1, 11), strict=True))
# Keys are already normalized ("fullscreen" is spelled "full screen" by normalize()).
FIXED = {
    "make this window full screen": Intent("fullscreen"),
    "exit full screen": Intent("unfullscreen"),
    "turn the volume down": Intent("volume_down"), "volume down": Intent("volume_down"),
    "turn the volume up": Intent("volume_up"), "volume up": Intent("volume_up"),
    "mute volume": Intent("volume_mute"),
    # "unmute" is not in the recognizer's dictionary, so the canonical form uses common words.
    "turn the sound back on": Intent("volume_unmute"),
    "start recording": Intent("record_start"), "stop recording": Intent("record_stop"),
    "open the video recorder": Intent("open_recorder"), "open video recorder": Intent("open_recorder"),
    "save replay": Intent("replay_save"), "start replay": Intent("replay_start"), "stop replay": Intent("replay_stop"),
    "stop listening": Intent("pause"), "cancel": Intent("cancel"),
    "listen to me": Intent("dictation_start"), "stop dictation": Intent("dictation_stop"),
    "close yourself": Intent("quit"),
}

# Explicit wording variants. These are grammar rules, never fuzzy intent guesses.
VARIANTS = {
    "turn down the volume": "turn the volume down", "lower the volume": "volume down",
    "lower volume": "volume down", "decrease the volume": "volume down",
    "turn up the volume": "turn the volume up", "raise the volume": "volume up",
    "increase the volume": "volume up", "mute the volume": "mute volume",
    "mute audio": "mute volume", "mute the audio": "mute volume",
    "turn the sound off": "mute volume",
    "turn the sound on": "turn the sound back on", "sound on": "turn the sound back on",
    "restore the volume": "turn the sound back on",
    # For the second recognizer, which knows the word: spelling is all that separates
    # this from "mute volume", so without these the closest match is the opposite.
    "unmute volume": "turn the sound back on", "unmute the volume": "turn the sound back on",
    "unmute audio": "turn the sound back on", "unmute the audio": "turn the sound back on",
    "unmute sound": "turn the sound back on", "unmute the sound": "turn the sound back on",
    "unmute": "turn the sound back on",
    "start screen recording": "start recording", "start a recording": "start recording",
    "start recording my screen": "start recording", "record my screen": "start recording",
    "stop screen recording": "stop recording", "stop the recording": "stop recording",
    "stop recording my screen": "stop recording", "end recording": "stop recording",
    "open recorder": "open the video recorder", "open the recorder": "open the video recorder",
    "make the window full screen": "make this window full screen",
    "make this full screen": "make this window full screen", "go full screen": "make this window full screen",
    "leave full screen": "exit full screen", "stop listening to me": "stop listening",
    "pause listening": "stop listening", "cancel that": "cancel", "never mind": "cancel",
    "clip that": "save replay", "save that": "save replay", "save the replay": "save replay",
    "save a replay": "save replay", "start instant replay": "start replay",
    "turn on instant replay": "start replay", "stop instant replay": "stop replay",
    "turn off instant replay": "stop replay",
    "start dictation": "listen to me", "take dictation": "listen to me", "start dictating": "listen to me",
    "listen": "listen to me",
    "quit jarvis": "close yourself", "close jarvis": "close yourself", "exit jarvis": "close yourself",
    "quit yourself": "close yourself", "shut yourself down": "close yourself", "turn yourself off": "close yourself",
}

# Optional sentence-edge courtesy. The parser accepts both; the speech grammar
# only offers the prefixes. With optional trailing words the decoder tends to
# hear a stray "for" in the silence after a command and then reject the whole
# utterance (measured on the upstream PocketSphinx recording), so suffixes are
# left to typed/CLI input.
POLITE_PREFIXES = ("please", "can you", "could you", "would you",
                   "can you please", "could you please", "would you please")
POLITE_SUFFIXES = ("please", "for me", "for me please")
# Spoken workspace forms -> intent action.
WORKSPACE_FORMS = {
    "go to workspace": "workspace", "go to the workspace": "workspace",
    "switch to workspace": "workspace", "switch to the workspace": "workspace",
    "switch workspace": "workspace",
    "move this window to workspace": "move_workspace", "move the window to workspace": "move_workspace",
    "move this window to the workspace": "move_workspace", "move the window to the workspace": "move_workspace",
}
# Spoken application verbs -> intent action.
APP_VERBS = {"open": "open_app", "open up": "open_app", "launch": "open_app", "start": "open_app",
             "focus": "focus_app", "switch to": "focus_app", "close": "close_app"}
# Accepted when typed, but not offered to the recognizer: "open up" competes
# with "open" and lowered the score of a plain "open firefox" (0.96 -> 0.73).
TYPED_ONLY_VERBS = ("open up",)

MAX_GRAMMAR_PHRASES = 4096
MAX_GRAMMAR_APPS = 2048


def _alternation(words) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


POLITE_PREFIX = re.compile(rf"^(?:{_alternation(POLITE_PREFIXES)}) ")
POLITE_SUFFIX = re.compile(rf" (?:{_alternation(POLITE_SUFFIXES)})$")
WORKSPACE_RE = re.compile(rf"({_alternation(WORKSPACE_FORMS)}) (\w+)")
APP_RE = re.compile(rf"({_alternation(APP_VERBS)}) (.+)")


def command_wording(phrase: str) -> str:
    """Strip only allowlisted sentence-edge courtesy, preserving app names."""
    phrase = POLITE_PREFIX.sub("", normalize(phrase), count=1)
    return POLITE_SUFFIX.sub("", phrase, count=1)


REFERENCE = [
    {"category": "Applications", "title": "Open or focus an app", "examples": ["open Dolphin", "please open Firefox", "could you launch Firefox", "focus Firefox", "switch to Firefox"]},
    {"category": "Applications", "title": "Close an app", "examples": ["close Firefox", "close Dolphin", "please close Firefox"]},
    {"category": "Workspaces", "title": "Navigate without keybindings", "examples": ["go to workspace three", "switch to workspace three", "move this window to workspace two"]},
    {"category": "Windows", "title": "Fullscreen", "examples": ["make this window fullscreen", "exit fullscreen"]},
    {"category": "Audio", "title": "Volume", "examples": ["turn the volume down", "turn down the volume", "turn the volume up", "mute volume", "turn the sound back on"]},
    {"category": "Recording", "title": "Capture your screen", "examples": ["start recording", "stop recording", "open the video recorder"]},
    {"category": "Recording", "title": "Instant replay", "examples": ["clip that", "clip the last thirty seconds", "clip the last two minutes", "save replay", "start replay", "stop replay"]},
    {"category": "Dictation", "title": "Type what you say into the focused text field", "examples": ["listen to me", "start dictation", "stop dictation"]},
    {"category": "Privacy", "title": "Stop immediately", "examples": ["stop listening", "cancel"]},
    {"category": "Privacy", "title": "Close Jarvis", "examples": ["close yourself", "quit Jarvis"]},
    {"category": "Arabic", "title": "Arabic commands (Arabic and English setting)", "examples": ["افتح فايرفوكس", "سكّر ديسكورد", "علّ الصوت", "وطّ الصوت", "روح لمساحة العمل الثالثة", "احفظ آخر ثلاثين ثانية", "كلّبها", "اسمعني", "خلاص"]},
]


class GrammarMatcher:
    """Checks recognizer output against the same grammar the decoder was given.

    Pure Python, so it is testable without a speech engine.
    """

    def __init__(self, phrases, apps=(), verbs=tuple(APP_VERBS)):
        self.phrases = frozenset(phrases)
        self.apps = frozenset(apps)
        self.verbs = tuple(sorted(verbs, key=len, reverse=True))

    def _core(self, text: str) -> bool:
        if text in self.phrases:
            return True
        return any(text.startswith(verb + " ") and text[len(verb) + 1:] in self.apps for verb in self.verbs)

    def accepts(self, text: str) -> bool:
        text = " ".join(text.split())
        if self._core(text):
            return True
        stripped = POLITE_SUFFIX.sub("", POLITE_PREFIX.sub("", text, count=1), count=1)
        return stripped != text and self._core(stripped)


def close_words(words) -> list[str]:
    """Validated words that close an application ("close", "quit", "kill")."""
    message = "Close words must be 1–8 distinct words of up to three words each, not other app verbs"
    if not isinstance(words, list) or not 1 <= len(words) <= 8:
        raise CommandError(message)
    reserved = {verb for verb, action in APP_VERBS.items() if action != "close_app"} | set(POLITE_PREFIXES)
    result = []
    for word in words:
        if not isinstance(word, str) or len(word) > 40 or any(ord(ch) < 32 or 127 <= ord(ch) < 160 for ch in word):
            raise CommandError(message)
        word = normalize(word)
        if not word or len(word.split(" ")) > 3 or word in reserved or word in result:
            raise CommandError(message)
        result.append(word)
    return result


class Parser:
    def __init__(self, custom: dict[str, str] | None = None):
        from .phrases import Rules
        self.rules = Rules()
        self.blocked_apps = set()
        self.close_words = ["close"]
        self.custom = {normalize(k): normalize(v) for k, v in (custom or {}).items()}
        # Targets are canonical built-ins; no recursion, arbitrary commands, or app paths.
        for phrase, target in self.custom.items():
            if phrase in FIXED or phrase in VARIANTS:
                raise CommandError("Built-in phrases cannot be overridden; choose a new phrase")
            if target not in FIXED:
                raise CommandError(f"Custom command target is not a built-in phrase: {target}")

    @classmethod
    def from_config(cls, config):
        from .phrases import Rules
        parser = cls(config["commands"])
        parser.close_words = close_words(config["close_words"])
        rules = Rules.from_config(config)
        for phrase in parser.custom:
            if rules.parse_normalized(phrase) is not None:
                raise CommandError("Command shortcut conflicts with a configured voice control")
        for phrase in rules.launches:
            try:
                original = parser.parse(phrase)
            except CommandError:
                continue
            if original.action not in ("open_app", "focus_app") or phrase in parser.custom:
                raise CommandError("Application shortcut conflicts with an existing command")
        parser.rules = rules
        parser.blocked_apps = {normalize(k) for k in config["app_prefixes"]}
        return parser

    def parse(self, text: str) -> Intent:
        """Arabic speech is first tried as an exact configured shortcut or voice
        alias, then translated onto the English command it asks for."""
        from .phrases import has_arabic, translate_arabic
        if not isinstance(text, str):
            raise CommandError("Phrase must be text")
        if not has_arabic(text):
            return self._parse_text(text)
        try:
            return self._parse_text(text)
        except CommandError:
            pass
        english = translate_arabic(text, self.close_words[0] if self.close_words else "close")
        if english is None:
            raise CommandError("Arabic phrase not recognized. Open Commands to see the Arabic commands.")
        return self._parse_text(english)

    def _parse_text(self, text: str) -> Intent:
        if re.search(r"workspace\s+[+-]\s*\w", text, re.IGNORECASE):
            raise CommandError("Use an unsigned workspace number one through ten")
        phrase = normalize(text)
        exact = self.rules.parse_normalized(phrase)
        if exact is not None:
            return exact
        # An exact user shortcut takes precedence over optional politeness.
        if phrase not in self.custom:
            phrase = POLITE_SUFFIX.sub("", POLITE_PREFIX.sub("", phrase, count=1), count=1)
        phrase = self.custom.get(phrase, phrase)
        phrase = VARIANTS.get(phrase, phrase)
        if phrase in FIXED:
            return FIXED[phrase]
        exact = self.rules.parse_normalized(phrase)
        if exact is not None:
            return exact
        from .phrases import clip_seconds
        seconds = clip_seconds(phrase)
        if seconds is not None:
            return Intent("replay_save", seconds)
        m = WORKSPACE_RE.fullmatch(phrase)
        if m:
            number = NUMBERS.get(m[2], int(m[2]) if m[2].isascii() and m[2].isdigit() else 0)
            if not 1 <= number <= 10:
                raise CommandError("Choose workspace one through ten")
            return Intent(WORKSPACE_FORMS[m[1]], number)
        verbs = self.app_verbs()
        m = re.fullmatch(rf"({_alternation(verbs)}) (.+)", phrase)
        if m:
            target = m[2].removeprefix("the ")
            if verbs[m[1]] == "open_app" and target in self.blocked_apps:
                raise CommandError("Use the launch word configured for this application name")
            if target:
                return Intent(verbs[m[1]], target)
        raise CommandError("Phrase not recognized. Open Commands to see supported phrases.")

    def app_verbs(self) -> dict[str, str]:
        """Built-in app verbs, with the configured close words replacing "close"."""
        verbs = {verb: action for verb, action in APP_VERBS.items() if action != "close_app"}
        return verbs | dict.fromkeys(self.close_words, "close_app")

    def grammar(self, app_names) -> dict:
        """Everything the recognizer may hear, built from the parser's own tables.

        Application names are kept separate from the verbs, so the grammar grows
        by one entry per app rather than one per verb and app. Apps beyond
        ``MAX_GRAMMAR_APPS`` are reported in ``overflow`` instead of failing.
        """
        from .phrases import clip_phrases
        core = set(FIXED) | set(VARIANTS) | set(self.custom) | set(self.rules.phrases()) | set(clip_phrases())
        for form in WORKSPACE_FORMS:
            core.update(f"{form} {number}" for number in NUMBERS)
        if len(core) > MAX_GRAMMAR_PHRASES:
            raise CommandError(f"Too many command phrases (limit {MAX_GRAMMAR_PHRASES}). Remove command shortcuts.")
        apps = sorted({name for name in app_names if name})
        return {"launches": self.rules.launches, "blocked_apps": sorted(self.blocked_apps), "phrases": sorted(core), "apps": apps[:MAX_GRAMMAR_APPS], "custom": dict(self.custom),
                "verbs": sorted(v for v in self.app_verbs() if v not in TYPED_ONLY_VERBS),
                "close_verbs": list(self.close_words),
                "prefixes": list(POLITE_PREFIXES),
                "suffixes": [],
                "overflow": [f"open {name}" for name in apps[MAX_GRAMMAR_APPS:]]}


# --- Closest-match fallback -------------------------------------------------

GREETINGS = ("hey", "hi", "hello", "okay", "ok")


def wake_variants(phrase: str) -> list[str]:
    """Forms of the wake phrase to accept: 'hey jarvis' also accepts 'hi jarvis',
    'hello jarvis', 'okay jarvis' and plain 'jarvis'. A false wake only opens a
    short command window; it never runs anything by itself."""
    words = normalize(phrase).split()
    if len(words) >= 2 and words[0] in GREETINGS:
        name = " ".join(words[1:])
        return sorted({f"{greeting} {name}" for greeting in GREETINGS if greeting != "ok"} | {name})
    return [" ".join(words)]


def all_wake_variants(activation: dict) -> list[str]:
    return sorted({form for name in [activation["wake_phrase"], *activation.get("wake_aliases", [])]
                   for form in wake_variants(name)})


def _squash(text: str) -> str:
    return text.replace(" ", "")


NUMBER_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                "fourteen fifteen sixteen seventeen eighteen nineteen twenty").split()


def spoken_numbers(text: str) -> str:
    """'tekken 8' -> 'tekken eight'; speech recognizers often write digits."""
    return re.sub(r"\b(\d{1,2})\b", lambda m: NUMBER_WORDS[int(m[1])] if int(m[1]) <= 20 else m[1], text)


MAX_LEADING_WORDS = 2   # tolerate a misheard word or two before the command ("a", "uh")


def closest_match(heard: str, candidates: dict, min_score: float = 0.82, min_margin: float = 0.06, exact_only: bool = False,
                  close_verbs=("close",)):
    """Return (command, score) for the command closest to free-form text, or None.

    ``candidates`` maps each full command phrase to what it does (its intent),
    e.g. from ``match_candidates``. The score is a character-level similarity
    (0..1) with spaces removed, so "open fire fox" matches "open firefox" and
    "open stream io" matches "open stremio". Digits count as words ("workspace
    3" = "workspace three").

    The command may be only *part* of what was heard: it can start up to two
    words in, and anything after it is ignored. That covers a background voice
    or TV that the transcript picked up after you ("start recording tomorrow
    will be cloudy…").

    To avoid guessing, the best phrase must score at least ``min_score`` *and*
    beat the best phrase that does something *different* by ``min_margin``;
    otherwise nothing is run. (Two wordings of the same command, like "start
    recording" and "start a recording", never compete with each other.)
    """
    if len(heard) > 256:   # only the start can hold the command; keep it within normalize()'s limit
        heard = heard[:257].rsplit(" ", 1)[0]
    from .phrases import translate_arabic
    heard = translate_arabic(heard, close_verbs[0] if close_verbs else "close") or heard
    try:
        literal = normalize(heard)
        if literal in candidates:
            return literal, 1.0
        text = spoken_numbers(command_wording(heard))
    except ValueError:
        return None
    if text in candidates:
        return text, 1.0
    verbs = ["open", "open up", "launch", "start", "focus", "switch to", *close_verbs]
    text = re.sub(rf"^({_alternation(verbs)}) the ", r"\1 ", text)
    if text in candidates:
        return text, 1.0
    # A clip length may follow a misheard word or two, like other commands.
    from .phrases import clip_canonical
    words = text.split()
    for start in range(min(3, len(words))):
        clip = clip_canonical(" ".join(words[start:]))
        if clip is not None:
            return (clip, 1.0) if clip in candidates else None
    # Closing requires a complete, exact name/alias. Never infer it from
    # a similar open command, or turn a misheard close into an open command.
    if any(text.startswith(verb + " ") for verb in close_verbs):
        return None
    if exact_only:
        return None
    words = text.split()
    if len(_squash(text)) < 3:
        return None
    # First the whole transcript; only if that matches nothing, every stretch
    # of it that starts within the first few words (extra speech around the
    # command). Doing it in that order keeps "open stream io" from matching
    # "open steam" through its first two words.
    match = _closest([_squash(text)], candidates, min_score, min_margin)
    if match:
        return match
    stretches = {_squash(" ".join(words[start:end]))
                 for start in range(min(MAX_LEADING_WORDS + 1, len(words)))
                 for end in range(start + 1, len(words) + 1)}
    return _closest(sorted(w for w in stretches if len(w) >= 3), candidates, min_score, min_margin)


def _closest(windows, candidates, min_score, min_margin):
    from collections import Counter
    from difflib import SequenceMatcher
    # A pair scoring below this can neither win nor narrow the winner's lead,
    # so pairs whose upper bound (length, shared letters) is below it are skipped.
    floor = min_score - min_margin
    letters = {window: Counter(window) for window in windows}
    best_by_intent: dict[str, tuple[float, str]] = {}
    for candidate, intent in candidates.items():
        # Exclude exact-only actions while iterating, without copying the full
        # candidate dictionary for each matching pass.
        # Clip lengths are never guessed either: "fifteen" is spelled like "fifty".
        if intent.startswith("close_app:") or re.fullmatch(r"replay_save:\d+", intent):
            continue
        target = _squash(candidate)
        target_letters = matcher = None
        best = 0.0
        for window in windows:
            longest = max(len(target), len(window))
            # Cheap length filter before the O(n*m) comparison.
            if abs(len(target) - len(window)) > longest * (1 - min_score) * 2 \
                    or min(len(target), len(window)) < longest * floor:
                continue
            if target_letters is None:
                target_letters = Counter(target)
            bound = (letters[window] & target_letters).total() / longest
            if bound < floor or bound <= best:
                continue
            if matcher is None:
                matcher = SequenceMatcher(None, "", target, autojunk=False)
            matcher.set_seq1(window)
            same = sum(block.size for block in matcher.get_matching_blocks())
            # Both sides must be mostly covered: "recording" alone must not
            # become "end recording" just because the lengths are close.
            best = max(best, min(same / len(window), same / len(target)))
        if best and best > best_by_intent.get(intent, (0.0, ""))[0]:
            best_by_intent[intent] = (best, candidate)
    if not best_by_intent:
        return None
    ranked = sorted(best_by_intent.values(), key=lambda item: (-item[0], item[1]))
    best_score, best_phrase = ranked[0]
    rival = ranked[1][0] if len(ranked) > 1 else 0.0
    if best_score >= min_score and best_score - rival >= min_margin:
        return best_phrase, round(best_score, 3)
    return None


def match_candidates(grammar: dict, extra_apps=()) -> dict:
    """Every full command the closest-match fallback may choose, mapped to its intent.

    ``grammar["app_ids"]`` maps each app name to the desktop entry it opens, so
    two names for one app (an alias "streamio" and the real name "stremio") count
    as the *same* command and don't block each other in ``closest_match``.
    """
    parser = Parser(dict(grammar.get("custom", {})))
    from .phrases import Rules
    parser.rules = Rules(grammar.get("launches"))
    parser.blocked_apps = set(grammar.get("blocked_apps", []))
    parser.close_words = list(grammar.get("close_verbs", ["close"]))
    app_ids = grammar.get("app_ids", {})
    phrases = list(grammar.get("phrases", []))
    apps = list(grammar.get("apps", [])) + [a for a in extra_apps if a]
    for verb in grammar.get("verbs", []):
        phrases.extend(f"{verb} {app}" for app in apps)
    result = {}
    for phrase in phrases:
        try:
            intent = parser.parse(phrase)
        except ValueError:
            continue
        target = app_ids.get(intent.target, intent.target) if intent.action in ("open_app", "focus_app", "close_app") else intent.target
        if isinstance(target, dict):
            import json
            target = json.dumps(target, sort_keys=True, separators=(",", ":"))
        result[phrase] = f"{intent.action}:{target}"
    return result
