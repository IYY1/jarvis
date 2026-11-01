# Open and close commands for every app

The Commands API retains its built-in `commands` reference and adds an
`applications` list with one explicit open/close pair per installed desktop app.
The GTK Commands page searches the complete list and displays 30 cards at a
time. Each Applications card also shows its pair. The Windows Commands page
lists the same pairs.

The registry verifies both phrases against the parser and checks that both
resolve to the intended desktop ID. It tries the app name and its indexed
aliases before assigning an unambiguous `application` number using spoken
digits. This handles duplicate names, command-name collisions, and names whose
saved custom launch word prevents the ordinary `open` form. Existing custom
launch settings remain available. Only selected fallback names enter the voice
grammar; unused fallback names do not enlarge it. Rescanning detects newly
installed apps even when their display names duplicate an existing entry.

Closing requests a normal close for matching app windows. Application save
prompts remain in control; background processes are not force-killed.

Validation on 2026-10-28:

- 93 installed apps, 93 command pairs, 186 listed open/close phrases.
- Every pair parses and resolves to its intended app; Python and Rust catalogs
  match, and all listed phrases are in the loaded voice grammar.
- Python suite: 394 tests, 380 passed and 14 skipped.
- Native controller: 7 tests passed; Clippy passed with warnings denied.
- Hidden GTK window: bounded pages, navigation and search passed.
- The tested controller is active; voice control and 120-second RAM replay are
  running, and the saved configuration is unchanged.

These checks validate routing and the catalog without opening or closing real
apps. They do not measure recognition accuracy for every app name spoken aloud.
See [activation evidence](benchmarks/every-app-commands-2026-10-28.json).
