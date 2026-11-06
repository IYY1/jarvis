# Contributing

Jarvis is a Python application with a native GTK settings client and separate
speech worker. See [architecture](docs/architecture.md) before changing process
lifetime or capture behavior. The package namespace and service/storage IDs
retain the earlier `jarvis` spelling for compatibility.

## Development and tests

Use Python 3.11 or newer. The core suite needs only the standard library and the
installed project; optional recognition tests skip when their dependencies or
local models are absent.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

For native UI work, use the distribution's GTK/PyGObject and a venv created with
`--system-site-packages`, as shown in the root README. For real speech tests,
install `.[voice]` and supply `JARVIS_VOSK_MODEL`, `JARVIS_WHISPER_DIR` and
`JARVIS_SPEECH_TEST_RAW` explicitly. Tests never download models automatically.

## Changes for review

- Keep speech parsing allowlisted and independent of executable shell text.
- Preserve immediate mute, single-worker ownership and bounded buffers/queues.
- Add regression coverage for behavior changes; avoid tests that merely repeat
  implementation details or require a real desktop for pure policy checks.
- Measure performance changes in fresh processes with identical models and
  input. Report CPU seconds, wall time, latency and RSS with workload limits.
- Use explicit integration scripts for microphone/screen/desktop actions. Keep
  personal recordings, credentials, local settings and venvs out of commits.

The GitHub workflow is configured for core tests, voice dependencies and package
construction. It contains no upload, release or deployment step. Contributions
are under the project's [MIT license](LICENSE).
