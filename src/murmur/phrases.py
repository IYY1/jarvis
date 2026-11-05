"""Configured application launch words."""
from __future__ import annotations


class Rules:
    def __init__(self, launches=None):
        self.launches = launches or {}

    @classmethod
    def from_config(cls, c):
        from .commands import normalize
        rules = cls()
        prefixes = c['app_prefixes']
        if not isinstance(prefixes, dict) or len(prefixes) > 256:
            raise ValueError('Invalid application prefixes')
        aliases = {normalize(k): v for k, v in c['aliases'].items()}
        seen = set()
        for alias, prefix in prefixes.items():
            name = normalize(alias)
            if name in seen or not isinstance(prefix, str) or len(prefix) > 40 or any(ord(ch) < 32 or ord(ch) == 127 for ch in prefix) or len(prefix.split()) > 4:
                raise ValueError('Invalid or duplicate application prefix')
            seen.add(name)
            if name not in aliases:
                raise ValueError('Launch words need an existing application alias')
            p = normalize(prefix)
            phrase = f'{p} {name}' if p else name
            if phrase in rules.launches:
                raise ValueError('Conflicting application shortcut')
            rules.launches[phrase] = name
        return rules

    def parse(self, text):
        from .commands import normalize
        return self.parse_normalized(normalize(text))

    def parse_normalized(self, p):
        from .commands import Intent
        if p in self.launches:
            return Intent('open_app', self.launches[p])
        for shortcut, alias in self.launches.items():
            prefix = shortcut[:-len(alias)]
            if p == prefix + 'the ' + alias:
                return Intent('open_app', alias)
        return None

    def phrases(self):
        return sorted(self.launches)

    def canonical(self, text):
        intent = self.parse(text)
        if intent is None:
            return None
        if intent.action == 'open_app':
            return next(p for p, name in sorted(self.launches.items()) if name == intent.target)
        return None


# Clip lengths, mirrored by native/command-phrases (jarvis_phrases).
MAX_CLIP_SECONDS = 1800   # the longest replay buffer
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
         "fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def number_words(n):
    """1–99 in words ("twenty five"), the form speech grammars contain."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    return str(n)


def _spoken_number(text):
    if text in ("a", "an"):
        return 1
    if text and len(text) <= 4 and text.isascii() and text.isdigit():
        return int(text)
    words = text.split(" ")
    if len(words) == 1:
        if words[0] in _ONES:
            return _ONES.index(words[0])
        return _TENS.index(words[0]) * 10 if words[0] in _TENS[2:] else None
    if len(words) == 2 and words[0] in _TENS[2:] and words[1] in _ONES[1:10]:
        return _TENS.index(words[0]) * 10 + _ONES.index(words[1])
    return None


def _strip_any(text, words):
    for word in words:
        if text.startswith(word):
            return True, text[len(word):]
    return False, text


def clip_seconds(p):
    """Seconds requested by a normalized clip command such as "clip the last
    twenty five seconds", "save the last 2 minutes" or "clip the last minute".
    "clip that" (the whole buffer) is a fixed phrase and returns None here."""
    found, rest = _strip_any(p, ("clip ", "save "))
    if not found:
        return None
    rest = _strip_any(rest, ("that ", "it "))[1]
    rest = rest.removeprefix("the ")
    last, rest = _strip_any(rest, ("last ", "past ", "previous "))
    if last and rest == "minute":
        return 60
    if " " not in rest:
        return None
    amount, unit = rest.rsplit(" ", 1)
    scale = 1 if unit in ("second", "seconds", "sec", "secs") else 60 if unit in ("minute", "minutes", "min", "mins") else None
    number = _spoken_number(amount) if scale else None
    if number is None:
        return None
    total = number * scale
    return total if 1 <= total <= MAX_CLIP_SECONDS else None


def clip_phrase(seconds):
    """The one wording the recognizer is given for a clip length, if it has one."""
    if seconds == 1:
        return "clip the last one second"
    if 2 <= seconds <= 59:
        return f"clip the last {number_words(seconds)} seconds"
    if seconds == 60:
        return "clip the last minute"
    if seconds % 60 == 0 and seconds <= 30 * 60:
        return f"clip the last {number_words(seconds // 60)} minutes"
    return None


def clip_phrases():
    """Every spoken clip length: 1–59 seconds and 1–30 minutes."""
    return [clip_phrase(s) for s in [*range(1, 60), *(m * 60 for m in range(1, 31))]]


def clip_canonical(p):
    """Maps any accepted clip wording ("clip last 25 seconds") to its grammar form."""
    seconds = clip_seconds(p)
    return None if seconds is None else clip_phrase(seconds)
