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
