"""Rust/Python parity and owned capture lifetimes; no real microphone or actions."""
import copy
import asyncio
import json
import os
from pathlib import Path
import random
import selectors
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from jarvis.commands import Parser, POLITE_PREFIXES, POLITE_SUFFIXES, closest_match, match_candidates
from jarvis.config import DEFAULTS
from jarvis.native_worker import binary_path, prepare

APPS = ["firefox", "dolphin", "discord", "deadlock", "steam", "stremio", "streamio", "tekken eight", "calculator"]


def native(config=None, mode="once", grammar=None):
    with patch.dict(os.environ, JARVIS_VOICE_WORKER="rust"):
        return prepare(config or copy.deepcopy(DEFAULTS), mode, grammar or Parser().grammar(APPS))


def event(process, timeout=10):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise AssertionError("Worker did not respond")
    line = process.stdout.readline()
    if not line:
        raise AssertionError(f"Worker exited: {process.poll()}")
    return json.loads(line)


class PreparationTests(unittest.TestCase):
    def test_default_prefers_rust_when_binary_library_and_model_are_available(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            (library / "libvosk.so").touch()
            with patch.dict(os.environ), patch("jarvis.native_worker.binary_path", return_value=Path("/built/jarvis-voice")), \
                    patch("jarvis.native_worker.package_dir", return_value=library), \
                    patch("jarvis.native_worker.valid_model", return_value=True), \
                    patch("jarvis.native_worker.accurate_model_ready", return_value=False):
                os.environ.pop("JARVIS_VOICE_WORKER", None)
                argv, payload, _ = prepare(copy.deepcopy(DEFAULTS), "once", {})
            self.assertEqual(argv, ["/built/jarvis-voice"])
            self.assertEqual(payload["native"]["vosk_library"], str(library / "libvosk.so"))

    def test_default_source_only_install_uses_python_without_loading_dependencies(self):
        with patch.dict(os.environ), patch("jarvis.native_worker.binary_path", return_value=None), \
                patch("jarvis.native_worker.package_dir", side_effect=AssertionError):
            os.environ.pop("JARVIS_VOICE_WORKER", None)
            argv, payload, _ = prepare(copy.deepcopy(DEFAULTS), "once", {})
        self.assertEqual(argv, [sys.executable, "-m", "jarvis.voice"])
        self.assertNotIn("native", payload)

    def test_default_preserves_pocketsphinx_and_missing_dependency_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            for backend, package, model_ready, library_ready in (
                    ("pocketsphinx", library, True, True), ("auto", None, True, True),
                    ("auto", library, False, True), ("auto", library, True, False)):
                with self.subTest(backend=backend, package=package, model=model_ready, library=library_ready):
                    (library / "libvosk.so").unlink(missing_ok=True)
                    if library_ready:
                        (library / "libvosk.so").touch()
                    config = copy.deepcopy(DEFAULTS)
                    config["recognition"]["backend"] = backend
                    with patch.dict(os.environ), patch("jarvis.native_worker.binary_path", return_value=Path("/built/jarvis-voice")), \
                            patch("jarvis.native_worker.package_dir", return_value=package), \
                            patch("jarvis.native_worker.valid_model", return_value=model_ready):
                        os.environ.pop("JARVIS_VOICE_WORKER", None)
                        argv, payload, _ = prepare(config, "once", {})
                    self.assertEqual(argv, [sys.executable, "-m", "jarvis.voice"])
                    self.assertNotIn("native", payload)

    def test_default_meter_can_use_rust_without_recognition_dependencies(self):
        with patch.dict(os.environ), patch("jarvis.native_worker.binary_path", return_value=Path("/built/jarvis-voice")), \
                patch("jarvis.native_worker.package_dir", return_value=None), \
                patch("jarvis.native_worker.accurate_model_ready", return_value=False):
            os.environ.pop("JARVIS_VOICE_WORKER", None)
            argv, payload, _ = prepare(copy.deepcopy(DEFAULTS), "test", {})
        self.assertEqual(argv, ["/built/jarvis-voice"])
        self.assertEqual(payload["native"]["vosk_library"], "")

    def test_python_remains_available_without_rust_or_model_imports(self):
        with patch.dict(os.environ, JARVIS_VOICE_WORKER="python"), patch("jarvis.native_worker.binary_path", side_effect=AssertionError):
            argv, payload, env = prepare(copy.deepcopy(DEFAULTS), "test", {})
        self.assertEqual(argv, [sys.executable, "-m", "jarvis.voice"])
        self.assertNotIn("native", payload)
        self.assertEqual(env["JARVIS_OWNER_PID"], str(os.getpid()))

    def test_explicit_rust_requires_a_built_worker_and_rejects_unknown_selection(self):
        for selection, error in (("rust", "missing"), ("typo", "must be")):
            with patch.dict(os.environ, JARVIS_VOICE_WORKER=selection), patch("jarvis.native_worker.binary_path", return_value=None):
                with self.assertRaisesRegex(ValueError, error):
                    prepare(copy.deepcopy(DEFAULTS), "test", {})

    def windows_release(self, directory, *, worker=True, library=True):
        tools = Path(directory) / "tools"
        tools.mkdir()
        if worker:
            (tools / "jarvis-voice.exe").touch(mode=0o755)
        if library:
            (tools / "libvosk.dll").touch()
        return patch.multiple("jarvis.native_worker", is_windows=lambda: True, app_dir=lambda: Path(directory), EXE=".exe",
                              python_worker_available=lambda: False, valid_model=lambda _path: True,
                              accurate_model_ready=lambda *_: False)

    def test_windows_release_uses_the_bundled_rust_worker_and_vosk_dll(self):
        with tempfile.TemporaryDirectory() as directory, self.windows_release(directory), patch.dict(os.environ):
            os.environ.pop("JARVIS_VOICE_WORKER", None)
            argv, payload, env = prepare(copy.deepcopy(DEFAULTS), "wake_word", Parser().grammar([]))
        tools = Path(directory) / "tools"
        self.assertEqual(argv, [str(tools / "jarvis-voice.exe")])
        self.assertEqual(payload["native"]["vosk_library"], str(tools / "libvosk.dll"))
        self.assertEqual(env["JARVIS_OWNER_PID"], str(os.getpid()))

    def test_windows_release_without_worker_or_library_explains_reinstall(self):
        for worker, library in ((False, True), (True, False)):
            with self.subTest(worker=worker, library=library), tempfile.TemporaryDirectory() as directory, \
                    self.windows_release(directory, worker=worker, library=library), \
                    patch("jarvis.native_worker.package_dir", return_value=None), patch.dict(os.environ):
                os.environ.pop("JARVIS_VOICE_WORKER", None)
                with self.assertRaisesRegex(ValueError, "[Rr]einstall"):
                    prepare(copy.deepcopy(DEFAULTS), "wake_word", Parser().grammar([]))

    def test_rust_ready_reports_release_voice_availability(self):
        from jarvis.native_worker import rust_ready
        with tempfile.TemporaryDirectory() as directory, self.windows_release(directory):
            self.assertTrue(rust_ready(copy.deepcopy(DEFAULTS)))
            config = copy.deepcopy(DEFAULTS)
            config["recognition"]["backend"] = "pocketsphinx"
            self.assertFalse(rust_ready(config))

    def test_light_speech_model_loads_whisper_tiny(self):
        config = copy.deepcopy(DEFAULTS)
        config["recognition"].update(accurate=True, speech_model="light")
        ready = []
        with patch.dict(os.environ, JARVIS_VOICE_WORKER="rust"), \
                patch("jarvis.native_worker.binary_path", return_value=Path("/built/jarvis-voice")), \
                patch("jarvis.native_worker.package_dir", return_value=None), \
                patch("jarvis.native_worker.accurate_model_ready", side_effect=lambda name: ready.append(name) or True):
            _, payload, _ = prepare(config, "test", Parser().grammar([]))
        self.assertEqual(ready, ["whisper-tiny"])
        self.assertTrue(payload["native"]["whisper"]["encoder"].endswith("sherpa-onnx-whisper-tiny.en/tiny.en-encoder.int8.onnx"))

    def test_missing_chosen_model_falls_back_to_the_installed_one(self):
        from jarvis.models import usable_model
        config = copy.deepcopy(DEFAULTS)
        for chosen, installed, expected in (("light", {"whisper-tiny", "whisper-small"}, "whisper-tiny"),
                                            ("light", {"whisper-small"}, "whisper-small"),
                                            ("small", {"whisper-tiny"}, "whisper-tiny"),
                                            ("light", set(), "whisper-tiny")):
            config["recognition"]["speech_model"] = chosen
            with self.subTest(chosen=chosen, installed=installed), \
                    patch("jarvis.models.accurate_model_ready", side_effect=lambda name: name in installed):
                self.assertEqual(usable_model(config), expected)

    def test_speech_model_setting_is_validated(self):
        from jarvis.config import validate
        self.assertEqual(validate({})["recognition"]["speech_model"], "light")
        self.assertEqual(validate({"recognition": {"speech_model": "light"}})["recognition"]["speech_model"], "light")
        with self.assertRaisesRegex(ValueError, "small or light"):
            validate({"recognition": {"speech_model": "tiny"}})

    def test_rust_whisper_preparation_does_not_require_sherpa(self):
        config = copy.deepcopy(DEFAULTS)
        config["recognition"]["accurate"] = True
        with patch.dict(os.environ, JARVIS_VOICE_WORKER="rust"), \
                patch("jarvis.native_worker.binary_path", return_value=Path("/built/jarvis-voice")), \
                patch("jarvis.native_worker.package_dir", return_value=None) as packages, \
                patch("jarvis.native_worker.accurate_model_ready", return_value=True):
            _, payload, _ = prepare(config, "test", Parser().grammar([]))
        self.assertIsNotNone(payload["native"]["whisper"])
        self.assertEqual(payload["native"]["whisper_idle_seconds"], 60)
        self.assertNotIn("vosk_fast_path", payload["native"])
        self.assertIs(payload["native"]["vosk_finalize_utterances"], True)
        self.assertEqual([c.args[0] for c in packages.call_args_list], ["vosk"])


@unittest.skipUnless(binary_path(), "build the optional Rust worker")
class RustPolicyTests(unittest.TestCase):
    def test_matches_python_across_commands_typos_ambiguities_and_unicode(self):
        config = copy.deepcopy(DEFAULTS)
        # Preparation in meter-test mode does not require a Vosk model.
        argv, _payload, env = native(config, "test")
        grammar = Parser().grammar(APPS)
        candidates = match_candidates(grammar)
        corpus = ["open fire fox", "open stream io", "go to workspace tree", "stat recording", "open taken eight",
                  "switch to discourt", "open team", "could you open fire fox please", "recording", "open", "", "hey",
                  "start a cording", "Go to workspace 3.", "uh open firefox and the weather tomorrow", "OPEN FIREFOX!",
                  "please open ﬁrefox", "start recording " + "and the weather tomorrow will be cloudy " * 7,
                  "i want to eat pizza", "what time is it", "can sell", "the weather tomorrow will be mostly cloudy"]
        rng = random.Random(20261022)
        for text in rng.sample(list(candidates), 90):
            corpus.extend((text, "could you " + text + " please", text[:3] + text[4:], "uh " + text + " tomorrow"))
        requests = [{"heard": text, "candidates": list(candidates.items()), "prefixes": list(POLITE_PREFIXES), "suffixes": list(POLITE_SUFFIXES)} for text in corpus]
        out = subprocess.run(argv + ["--match"], input="".join(json.dumps(r) + "\n" for r in requests), env=env, capture_output=True, text=True, timeout=60, check=True)
        actual = [json.loads(line)["match"] for line in out.stdout.splitlines()]
        self.assertEqual(len(actual), len(corpus))
        for text, result in zip(corpus, actual):
            expected = closest_match(text, candidates)
            self.assertEqual(result, list(expected) if expected else None, text)

    def test_no_consent_cannot_spawn_capture(self):
        argv, payload, env = native(mode="test")
        env["PATH"] = "/not-a-real-directory"
        out = subprocess.run(argv, input=json.dumps(payload) + "\n", env=env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("consent", json.loads(out.stdout)["message"])

    def test_payload_bound_is_enforced_before_model_or_capture_load(self):
        argv, _payload, env = native(mode="test")
        out = subprocess.run(argv, input=b"x" * 1048577, env=env, capture_output=True, timeout=10)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("size limit", json.loads(out.stdout)["message"])

    def test_invalid_whisper_idle_duration_is_rejected_before_loading(self):
        argv, payload, env = native(mode="test")
        for value in (0, 0.5, -60, 86401):
            with self.subTest(value=value):
                payload["native"]["whisper_idle_seconds"] = value
                out = subprocess.run(argv, input=json.dumps(payload) + "\n", env=env, text=True,
                                     capture_output=True, timeout=10)
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("Whisper idle duration", json.loads(out.stdout)["message"])


FIXTURE = os.environ.get("JARVIS_SPEECH_TEST_RAW")
MODEL = os.environ.get("JARVIS_VOSK_MODEL")
WHISPER = os.environ.get("JARVIS_WHISPER_MODEL")


def whisper_threads(pid):
    """Whisper's loader and inference pool threads exist only while it is loaded."""
    names = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            names.append((task / "comm").read_text().strip())
        except OSError:
            pass
    return sum(name.startswith("whisper-") for name in names)


def whisper_worker(idle, pcm):
    """Worker with real Vosk/Whisper, wake name "go forward" and a paced fake microphone."""
    config = copy.deepcopy(DEFAULTS)
    config["privacy"]["onboarded"] = True
    config["recognition"].update(model_path=MODEL, accurate=True)
    config["activation"]["wake_phrase"] = "go forward"
    argv, payload, env = native(config, "wake_word", Parser({"ten meters": "start recording"}).grammar(APPS))
    root = Path(WHISPER)
    payload["native"]["whisper"] = {key: str(root / f"small.en-{name}") for key, name in
                                    (("encoder", "encoder.int8.onnx"), ("decoder", "decoder.int8.onnx"), ("tokens", "tokens.txt"))}
    if idle is None:
        del payload["native"]["whisper_idle_seconds"]
    else:
        payload["native"]["whisper_idle_seconds"] = idle
    directory = tempfile.TemporaryDirectory()
    fake = Path(directory.name) / "pw-record"
    fake.write_text("#!/usr/bin/env python3\nimport os,time\nfrom pathlib import Path\naudio=bytes(19200)+Path(os.environ['PUBLIC_PCM']).read_bytes()\naudio+=bytes((-len(audio))%960)\nfor offset in range(0,len(audio),960):\n os.write(1,audio[offset:offset+960]);time.sleep(.03)\nwhile True: os.write(1,bytes(960));time.sleep(.03)\n")
    fake.chmod(0o700)
    env.update(PATH=directory.name + ":" + env.get("PATH", ""), PUBLIC_PCM=pcm)
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, bufsize=0)
    process.stdin.write((json.dumps(payload) + "\n").encode()); process.stdin.flush()
    while event(process, 30)["event"] != "ready":
        pass
    return process, directory


@unittest.skipUnless(binary_path() and MODEL and FIXTURE, "build Rust and supply the public fixture and Vosk model")
class RustEngineTests(unittest.TestCase):
    def test_continuous_wake_probe_counts_repeated_wakes_without_command_handoff(self):
        config = copy.deepcopy(DEFAULTS)
        config["recognition"].update(model_path=MODEL, accurate=False)
        config["activation"]["wake_phrase"] = "go forward"
        _, payload, env = native(config, grammar=Parser({"ten meters": "start recording"}).grammar(APPS))
        env["PATH"] = "/no/capture/program"
        binary = Path(__file__).resolve().parents[1] / "native/voice-worker/target/release/jarvis-voice"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "repeated.raw"
            audio = Path(FIXTURE).read_bytes() + bytes(64000)
            path.write_bytes(audio)
            requests = [{"path": str(path), "session": "wake", "repeat": 3},
                        {"path": str(path), "session": "wake", "repeat": 3, "wake_only": True}]
            for finalize in (False, True):
                with self.subTest(finalize=finalize):
                    payload["native"]["vosk_finalize_utterances"] = finalize
                    output = subprocess.run([str(binary), "--engine"],
                                            input=json.dumps(payload) + "\n" + "".join(json.dumps(r) + "\n" for r in requests),
                                            text=True, capture_output=True, check=True, env=env, timeout=20)
                    rows = [json.loads(line) for line in output.stdout.splitlines()]
                    self.assertEqual([r["event"] for r in rows], ["ready", "result", "result"])
                    self.assertEqual(sum(e["kind"] == "wake" for e in rows[1]["events"]), 1)
                    events = rows[2]["events"]
                    self.assertEqual([e["kind"] for e in events], ["wake"] * 3)
                    ticks = [e["input_seconds"] for e in events]
                    self.assertEqual(ticks, sorted(set(ticks)))
                    self.assertGreater(ticks[0], 0)
                    self.assertLessEqual(ticks[-1], len(audio) * 3 / 32000 + 2.03)
                    self.assertTrue(rows[2]["wake_only"])

    def test_wake_only_probe_rejects_a_command_session(self):
        config = copy.deepcopy(DEFAULTS)
        config["recognition"].update(model_path=MODEL, accurate=False)
        _, payload, env = native(config)
        binary = Path(__file__).resolve().parents[1] / "native/voice-worker/target/release/jarvis-voice"
        request = {"path": FIXTURE, "session": "command", "wake_only": True}
        output = subprocess.run([str(binary), "--engine"],
                                input=json.dumps(payload) + "\n" + json.dumps(request) + "\n",
                                text=True, capture_output=True, env=env, timeout=20)
        self.assertNotEqual(output.returncode, 0)
        self.assertIn("requires session wake", json.loads(output.stdout.splitlines()[-1])["message"])

    def test_finalized_decoders_recover_wakes_and_commands_across_repeated_background(self):
        from scripts.benchmark_vosk_finalization import engine_probe
        binary = Path(__file__).resolve().parents[1] / "native/voice-worker/target/release/jarvis-voice"
        for finalize in (False, True):
            with self.subTest(finalize=finalize):
                result = engine_probe(binary, Path(FIXTURE), finalize, 3, 12)
                self.assertEqual(result["correct_commands"], 6)
                self.assertEqual(result["background_events"], 0)
                self.assertTrue(result["commands_identical"])

    def test_engine_probe_rejects_invalid_audio_repetition(self):
        config = copy.deepcopy(DEFAULTS)
        config["recognition"].update(model_path=MODEL, accurate=False)
        argv, payload, env = native(config)
        for repeat in (0, 21, -1, True, "3"):
            with self.subTest(repeat=repeat):
                request = {"path": FIXTURE, "repeat": repeat}
                output = subprocess.run(argv + ["--engine"], input=json.dumps(payload) + "\n" + json.dumps(request) + "\n",
                                        text=True, capture_output=True, env=env, timeout=20)
                self.assertNotEqual(output.returncode, 0)
                error = json.loads(output.stdout.splitlines()[-1])
                self.assertEqual(error["event"], "error")
                self.assertRegex(error["message"], "repetition")

    def test_decoder_history_probe_runs_without_a_capture_program(self):
        config = copy.deepcopy(DEFAULTS)
        config["recognition"].update(model_path=MODEL, accurate=False)
        config["activation"]["wake_phrase"] = "go forward"
        _, payload, env = native(config, grammar=Parser({"ten meters": "start recording"}).grammar(APPS))
        env["PATH"] = "/no/capture/program"
        binary = Path(__file__).resolve().parents[1] / "native/voice-worker/target/release/jarvis-voice"
        request = {"path": FIXTURE, "decoder": "wake", "policy": "finalize", "cycles": 3, "seconds_per_cycle": 1}
        output = subprocess.run([str(binary), "--decoder-history"], input=json.dumps(payload) + "\n" + json.dumps(request) + "\n",
                                text=True, capture_output=True, check=True, env=env, timeout=20)
        rows = [json.loads(line) for line in output.stdout.splitlines()]
        self.assertEqual([row["event"] for row in rows], ["ready", "cycle", "cycle", "cycle"])
        self.assertAlmostEqual(rows[-1]["decoded_seconds"], 2.97)
        self.assertGreater(rows[-1]["memory"]["pss_mib"], 0)

    @unittest.skipUnless(WHISPER, "supply the Whisper model")
    def test_experimental_fast_path_avoids_loads_and_retains_whisper_fallback(self):
        from scripts.benchmark_vosk_fast_path import counters
        binary = Path(__file__).resolve().parents[1] / "native/voice-worker/target/release/jarvis-voice"
        for enabled, free in ((False, True), (True, True), (True, False)):
            with self.subTest(enabled=enabled, free=free):
                result = counters(binary, Path(FIXTURE), enabled, free, 2)
                stats = result["final_stats"]
                self.assertEqual(stats["whisper_loads"], 0 if enabled and free else 1)
                self.assertEqual(stats["fast_commands"], 2 if enabled and free else 0)
                actual = [e for row in result["results"][1:] for e in row["events"] if e["kind"] == "command"]
                self.assertEqual([e["text"] for e in actual], ["ten meters", "ten meters"])
                self.assertEqual([e.get("recognition_pass") for e in actual], ["vosk_fast" if free else "whisper"] * 2 if enabled else [None, None])

    def test_repeated_background_quiet_wake_without_room_noise(self):
        from scripts.benchmark_recognition_acceptance import audio_case, evaluate, run_case
        config = copy.deepcopy(DEFAULTS)
        config["privacy"]["onboarded"] = True
        config["recognition"].update(model_path=MODEL, accurate=False)
        config["activation"]["wake_phrase"] = "go forward"
        argv, payload, env = native(config, "wake_word", Parser({"ten meters": "start recording"}).grammar(APPS))
        case = {"repeat": 3, "segments": [
            {"pcm": str(Path(FIXTURE).resolve())}, {"silence": 1.5},
            {"pcm": str(Path(FIXTURE).resolve()), "reverse": True}, {"silence": 3.5}]}
        audio, _ = audio_case(case, Path("."))
        measured = run_case(Path(argv[0]), payload, env, audio)
        self.assertIsNone(measured["error"])
        result = evaluate(measured["events"], ["ten meters"] * 3, 3)
        self.assertTrue(result["passed"], result)

    def test_service_selects_rust_and_accepts_its_command_without_dispatching_actions(self):
        from jarvis.daemon import Service
        async def check():
            config = copy.deepcopy(DEFAULTS)
            config["privacy"]["onboarded"] = True
            config["activation"]["mode"] = "push_to_talk"
            config["recognition"].update(model_path=MODEL, accurate=False)
            config["commands"] = {"go forward ten meters": "start recording"}
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                capture = root / "pw-record"
                capture.write_text("#!/usr/bin/env python3\nimport os,time\nfrom pathlib import Path\naudio=Path(os.environ['PUBLIC_PCM']).read_bytes()+bytes(64000)\naudio+=bytes((-len(audio))%960)\nfor offset in range(0,len(audio),960):\n os.write(1,audio[offset:offset+960]);time.sleep(.03)\nwhile True: os.write(1,bytes(960));time.sleep(.03)\n")
                capture.chmod(0o700)
                with patch.dict(os.environ, JARVIS_VOICE_WORKER="rust", PATH=str(root) + ":" + os.environ.get("PATH", ""), PUBLIC_PCM=FIXTURE), \
                        patch("jarvis.daemon.AppRegistry", return_value=MagicMock(index={})):
                    service = Service(config)
                    service.loop = asyncio.get_running_loop()
                    service.execute = AsyncMock()
                    try:
                        await service.start_worker("once")
                        self.assertEqual(Path(f"/proc/{service.worker.pid}/exe").resolve().name, "jarvis-voice")
                        await asyncio.wait_for(service.voice_task, 12)
                        service.execute.assert_awaited_once()
                        self.assertEqual(service.execute.await_args.args[0], "go forward ten meters")
                        self.assertEqual(service.engine, "vosk")
                        self.assertIsNone(service.worker)
                    finally:
                        await service.stop_worker()
        asyncio.run(check())

    def test_real_vosk_commands_and_one_breath_wake_match_python(self):
        from jarvis.vosk_engine import VoskEngine
        from jarvis.voice import rms
        grammar = Parser({"go forward ten meters": "start recording", "ten meters": "start recording"}).grammar(APPS)
        config = copy.deepcopy(DEFAULTS)
        config["recognition"].update(model_path=MODEL, accurate=False)
        config["activation"]["wake_phrase"] = "go forward"
        argv, payload, env = native(config, grammar=grammar)
        audio = Path(FIXTURE).read_bytes()
        audio += bytes(((len(audio) + 64000 + 959) // 960) * 960 - len(audio))
        expected = []
        engine = VoskEngine(grammar, "go forward", model_path=MODEL)
        for session in ("command", "wake", "wake", "command"):
            engine.begin_command() if session == "command" else engine.begin_wake()
            results = []
            for offset in range(0, len(audio), 960):
                frame = audio[offset:offset + 960]
                result = engine.feed(frame, quiet=rms(frame) < .005)
                if result:
                    results.append(result.to_dict())
                    if result.kind == "wake" and (follow := engine.after_wake()):
                        results.append(follow.to_dict())
                    break
            expected.append(results)
        request = json.dumps(payload) + "\n" + "".join(json.dumps({"path": FIXTURE, "session": s}) + "\n" for s in ("command", "wake", "wake", "command"))
        out = subprocess.run(argv + ["--engine"], input=request, env=env, capture_output=True, text=True, timeout=30, check=True)
        actual = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual(actual[0]["event"], "ready")
        self.assertEqual([e["events"] for e in actual[1:]], expected)
        self.assertTrue(all(expected))

    def test_control_priority_and_capture_cleanup_with_a_fake_microphone(self):
        config = copy.deepcopy(DEFAULTS)
        config["privacy"]["onboarded"] = True
        config["recognition"].update(model_path=MODEL, accurate=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "pw-record"
            fake.write_text("#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\nPath(os.environ['CAPTURE_PID']).write_text(str(os.getpid()))\nwhile True: os.write(1, bytes(960))\n")
            fake.chmod(0o700)
            argv, payload, env = native(config, "wake_word")
            env.update(PATH=str(root) + ":" + env.get("PATH", ""), CAPTURE_PID=str(root / "capture.pid"))
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, bufsize=0)
            try:
                process.stdin.write((json.dumps(payload) + "\n").encode()); process.stdin.flush()
                while event(process)["event"] != "ready":
                    pass
                deadline = time.monotonic() + 3
                while not (root / "capture.pid").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / "capture.pid").exists())
                process.stdin.write(b"listen\n"); process.stdin.flush()
                self.assertTrue(event(process)["listening"])
                process.stdin.write(b"cancel\n"); process.stdin.flush()
                self.assertFalse(event(process)["listening"])
                capture_pid = int((root / "capture.pid").read_text())
                process.terminate(); self.assertEqual(process.wait(timeout=5), 0)
                self.assertFalse(Path(f"/proc/{capture_pid}").exists(), "Capture outlived the Rust worker")
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
                process.stdin.close(); process.stdout.close()

    def close(self, process, directory):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        process.stdin.close(); process.stdout.close()
        directory.cleanup()

    @unittest.skipUnless(WHISPER, "supply the Whisper model")
    def test_whisper_loads_on_wake_and_is_released_after_idle(self):
        process, directory = whisper_worker(1, FIXTURE)
        try:
            self.assertEqual(whisper_threads(process.pid), 0, "Whisper loaded before a wake name")
            events = [event(process, 15)]
            while events[-1]["event"] != "command":
                events.append(event(process, 15))
            self.assertEqual([e["event"] for e in events if e["event"] in ("wake", "command")], ["wake", "command"])
            self.assertEqual(events[-1]["text"], "ten meters")
            self.assertGreater(whisper_threads(process.pid), 0)
            deadline = time.monotonic() + 6
            while whisper_threads(process.pid) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertEqual(whisper_threads(process.pid), 0, "Whisper was not released after going idle")
            self.assertIsNone(process.poll())
        finally:
            self.close(process, directory)

    @unittest.skipUnless(WHISPER, "supply the Whisper model")
    def test_payload_without_idle_limit_keeps_whisper_resident(self):
        process, directory = whisper_worker(None, FIXTURE)
        try:
            self.assertGreater(whisper_threads(process.pid), 0, "Whisper was not loaded at startup")
        finally:
            self.close(process, directory)


if __name__ == "__main__":
    unittest.main()
