"""File-only experimental KWS protocol and optional real-model checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "native/kws-probe/target/release/jarvis-kws-probe"
HANDOFF_BINARY = BINARY.with_name("jarvis-kws-handoff-probe")
MODEL = os.environ.get("MURMUR_KWS_MODEL")
LIBRARY = os.environ.get("MURMUR_KWS_LIBRARY")
FIXTURE = os.environ.get("MURMUR_SPEECH_TEST_RAW")


@unittest.skipUnless(BINARY.is_file(), "build the optional offline KWS probe")
class KwsProbeProtocolTests(unittest.TestCase):
    def test_file_only_version_and_incomplete_arguments(self):
        result = subprocess.check_output([str(BINARY), "--version"], text=True)
        self.assertIn("experimental sherpa-onnx 1.13.8 C API", result)
        for args in ([], ["--microphone"], ["one", "two"]):
            result = subprocess.run([str(BINARY), *args], text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("panicked", result.stderr)

    def test_invalid_keywords_rejected_before_loading_native_library(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dummy = root / "dummy"
            dummy.write_text("not a shared library")
            tokens = root / "tokens.txt"
            tokens.write_text("<blk> 0\n<unk> 1\n▁GO 2\n")
            keywords = root / "keywords.txt"
            config = dict.fromkeys(("library", "encoder", "decoder", "joiner"), str(dummy))
            config.update(tokens=str(tokens), keywords=str(keywords))
            settings = root / "config.json"
            for bad in ("missing @wake", "<unk> @wake", "▁GO :10 @wake", "▁GO @wake\n▁GO @wake", "▁GO @a/b"):
                keywords.write_text(bad)
                settings.write_text(json.dumps(config))
                result = subprocess.run([str(BINARY), str(settings)], text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("invalid ELF", result.stderr)
                self.assertNotIn("panicked", result.stderr)


    def test_invalid_gate_rejected_before_loading_native_library(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dummy = root / "dummy"
            dummy.write_text("not a library")
            settings = root / "config.json"
            config = dict.fromkeys(("library", "encoder", "decoder", "joiner", "tokens", "keywords"), str(dummy))
            for gate in ({"preroll_ms": 999999}, {"hangover_ms": 0}, {"rms": 0}, {"enabled": True},
                         {"align_chunk_ms": 0}, {"align_chunk_ms": 321},
                         {"align_chunk_ms": 1001}, {"align_chunk_ms": 1000, "hangover_ms": 1200}):
                settings.write_text(json.dumps(dict(config, quiet_gate=gate)))
                result = subprocess.run([str(BINARY), str(settings)], text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("invalid ELF", result.stderr)
                self.assertNotIn("panicked", result.stderr)


@unittest.skipUnless(BINARY.is_file() and MODEL and LIBRARY and FIXTURE, "supply explicit KWS library/model and public PCM")
class KwsProbeModelTests(unittest.TestCase):
    def setup_files(self, root):
        model = Path(MODEL)
        keywords = root / "keywords.txt"
        keywords.write_text("▁GO ▁FOR WARD @GO_FORWARD\n")
        config = {key: str(next(model.glob(key + "*.int8.onnx")).resolve()) for key in ("encoder", "decoder", "joiner")}
        config.update(library=str(Path(LIBRARY).resolve()), tokens=str((model / "tokens.txt").resolve()), keywords=str(keywords))
        settings = root / "config.json"
        settings.write_text(json.dumps(config))
        return settings

    def test_repeated_requests_match_reference_and_do_not_load_python(self):
        from scripts.benchmark_kws import probe, reference
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = self.setup_files(root)
            path = root / "speech.raw"
            path.write_bytes(bytes(19200) + Path(FIXTURE).read_bytes() + bytes(48000))
            rows = [{"path": str(path), "name": "repeat-" + str(i), "expected": ["GO_FORWARD"],
                     "seconds": path.stat().st_size / 32000} for i in range(3)]
            measured = probe(BINARY, settings, rows, False)
            config = json.loads(settings.read_text())
            config.update(score=1.0, threshold=.25)
            expected = reference(config, rows)
            self.assertTrue(measured["all_cases_pass"])
            for actual, ref in zip(measured["cases"], expected, strict=True):
                self.assertEqual([{"keyword": e["result"]["keyword"], "audio_seconds": e["audio_seconds"]} for e in actual["hits"]], ref["hits"])
            # /proc maps reflects dynamic loads too, unlike ldd alone.
            with subprocess.Popen([str(BINARY), str(settings)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as process:
                try:
                    self.assertEqual(json.loads(process.stdout.readline())["event"], "ready")
                    maps = Path(f"/proc/{process.pid}/maps").read_text()
                    self.assertIn("libonnxruntime", maps)
                    self.assertIn("libsherpa-onnx-c-api", maps)
                    self.assertNotIn("libpython", maps)
                    self.assertNotIn("_sherpa_onnx.cpython", maps)
                finally:
                    process.stdin.close()
                    process.wait(timeout=10)

    def test_quiet_gate_preserves_onsets_pauses_and_background_recovery(self):
        from scripts.benchmark_kws import probe
        from scripts.benchmark_kws_gate import GATE, gate_cases
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = self.setup_files(root)
            config = json.loads(settings.read_text())
            rows = [r for r in gate_cases(Path(FIXTURE), Path(MODEL), root, 10) if r["group"] == "surrogate"]
            ungated = probe(BINARY, settings, rows, False)
            self.assertIsNone(ungated["ready"]["quiet_gate"], "gate remains disabled by default")
            settings.write_text(json.dumps(dict(config, quiet_gate=GATE)))
            gated = probe(BINARY, settings, rows, False)
            self.assertTrue(ungated["all_cases_pass"])
            self.assertTrue(gated["all_cases_pass"])
            for case in gated["cases"]:
                m = case["measurement"]
                total = round(m["audio_seconds"] * 16000)
                self.assertEqual(m["decoded_samples"] + m["skipped_samples"], total)
                self.assertLessEqual(m["decoded_samples"], total)
                self.assertTrue(all(0 <= h["audio_seconds"] <= m["audio_seconds"] for h in case["hits"]))
                if case["name"] == "silence":
                    self.assertEqual(m["stream_count"], 0)
                    self.assertEqual(m["decoded_samples"], 0)
                if case["name"] == "quiet-recovery":
                    self.assertGreaterEqual(m["stream_count"], 4)
                    self.assertGreater(case["hits"][-1]["stream_origin_seconds"], 30)
                if case["name"] == "long-leading-quiet":
                    self.assertGreater(case["hits"][0]["audio_seconds"], 12)
                    self.assertGreater(case["hits"][0]["stream_origin_seconds"], 10)

    def test_aligned_gate_preserves_original_timeline_and_counts_reopened_context(self):
        from scripts.benchmark_kws import probe
        from scripts.benchmark_kws_gate import GATE, gate_cases
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = self.setup_files(root)
            config = json.loads(settings.read_text())
            settings.write_text(json.dumps(dict(config, quiet_gate=dict(GATE, align_chunk_ms=320))))
            rows = [r for r in gate_cases(Path(FIXTURE), Path(MODEL), root, 10)
                    if r["group"] == "surrogate" and r["name"] in
                    {"natural", "low-volume", "quiet-recovery", "long-leading-quiet"}]
            # Reopen immediately after the hangover expires. The aligned stream
            # reuses original quiet samples, including a partial final PCM chunk.
            path = root / "reopen.raw"
            path.write_bytes(bytes(76800) + (1000).to_bytes(2, "little") * 960
                             + bytes(39360) + (1000).to_bytes(2, "little") * 960 + bytes(48014))
            rows.append(dict(name="reopen", path=str(path), seconds=path.stat().st_size/32000, expected=[]))
            measured = probe(BINARY, settings, rows, False)
            self.assertTrue(measured["all_cases_pass"])
            for case in measured["cases"]:
                m = case["measurement"]
                self.assertEqual(m["decoded_samples"] + m["skipped_samples"], round(m["audio_seconds"]*16000))
                self.assertEqual(m["accepted_samples"], m["decoded_samples"] + m["replayed_samples"])
                self.assertTrue(all(origin % 5120 == 0 for origin in m["stream_origins_samples"]))
                self.assertEqual(m["stream_count"], len(m["stream_origins_samples"]))
                self.assertTrue(all(0 <= h["stream_origin_seconds"] <= h["audio_seconds"] <= m["audio_seconds"]
                                    for h in case["hits"]))
                if case["name"] == "reopen":
                    self.assertEqual(m["stream_count"], 2)
                    self.assertGreater(m["replayed_samples"], 0)

    def test_bad_pcm_and_unknown_requests_fail_without_panicking(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = self.setup_files(root)
            path = root / "odd.raw"
            path.write_bytes(b"x")
            for request in ({"path": str(path)}, {"path": str(path), "capture": True}):
                result = subprocess.run([str(BINARY), str(settings)], input=json.dumps(request) + "\n", text=True, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("panicked", result.stderr)
                self.assertNotIn('"event":"keyword"', result.stdout)


@unittest.skipUnless(HANDOFF_BINARY.is_file(), "build the separate offline handoff probe")
class KwsHandoffProtocolTests(unittest.TestCase):
    def test_worker_faults_recover_on_a_later_wake_and_preserve_failed_exit(self):
        """Synthetic processes check protocol recovery, not speech recognition."""
        for fault in ("exit", "error", "invalid-json"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                detector = root / "detector"
                detector.write_text(f"#!{sys.executable}\n" + '''import json,sys,time
print(json.dumps({'event':'ready'}),flush=True)
for line in sys.stdin:
    time.sleep(.3)
    print(json.dumps({'event':'keyword','audio_seconds':.3}),flush=True)
    time.sleep(2.4)
    print(json.dumps({'event':'keyword','audio_seconds':2.7}),flush=True)
    time.sleep(1.8)
    print(json.dumps({'event':'complete'}),flush=True)
''')
                detector.chmod(0o700)
                worker = root / "worker"
                worker.write_text(f"#!{sys.executable}\n" + f'''import json,sys
from pathlib import Path
marker=Path({str(root / 'attempted')!r})
if not marker.exists():
    marker.touch()
    if {fault!r}=='error': print(json.dumps({{'event':'error','message':'injected load failure'}}),flush=True)
    if {fault!r}=='invalid-json': print('invalid-json',flush=True)
    sys.exit(7)
json.loads(sys.stdin.readline())
print(json.dumps({{'event':'ready'}}),flush=True)
json.loads(sys.stdin.readline())
print(json.dumps({{'event':'result','events':[{{'kind':'command','text':'synthetic'}}]}}),flush=True)
''')
                worker.chmod(0o700)
                payload, dummy, settings = (root / name for name in ("payload.json", "detector.json", "config.json"))
                payload.write_text(json.dumps({"config": {"recognition": {"closest_match": True}}}))
                dummy.write_text("{}")
                config = dict(detector_binary=str(detector), detector_config=str(dummy),
                              voice_binary=str(worker), voice_payload=str(payload), work_dir=folder,
                              settle_ms=0, quiet_endpoint=dict(rms=.001, min_speech_ms=240,
                                                              quiet_ms=600, timeout_ms=5000),
                              continuous=dict(max_windows=2, worker_timeout_ms=5000, recovery_quiet_ms=600))
                settings.write_text(json.dumps(config))
                audio = bytes(76800) + (1000).to_bytes(2, "little") * 11520 + bytes(44160)
                source = root / "input.raw"
                source.write_bytes(audio)
                run = subprocess.run([str(HANDOFF_BINARY), str(settings), str(source)],
                                     text=True, capture_output=True, timeout=12)
                self.assertEqual(run.returncode, 1, run.stderr)
                events = [json.loads(line) for line in run.stdout.splitlines()]
                self.assertEqual(events[-1]["event"], "complete", run.stderr)
                self.assertEqual(events[-1]["worker_failures"], 1)
                self.assertFalse(events[-1]["all_command_workers_succeeded"])
                self.assertEqual(events[-1]["successful_workers"], 1)
                self.assertEqual(events[-1]["aborted_windows"], 0)
                rows = [e for e in events if e["event"] == "window_complete"]
                self.assertEqual(len(rows), 2)
                self.assertTrue(rows[0]["worker_error"])
                self.assertIsNone(rows[0]["command_result"])
                self.assertEqual(rows[1]["release_reason"], "result")
                self.assertEqual(rows[1]["command_result"]["events"][0]["text"], "synthetic")
                for row in rows:
                    self.assertEqual(row["waiting_after_release"]["processes"], 2)
                    self.assertEqual(row["waiting_after_release"]["child_readers"]["active"], 1)
                    self.assertEqual(row["waiting_after_release"]["child_readers"]["joined"], row["window"])
                    if row["buffer_path"]:
                        start = round(row["buffer_start_seconds"] * 32000)
                        end = round(row["endpoint_input_seconds"] * 32000)
                        self.assertEqual(Path(row["buffer_path"]).read_bytes(), audio[start:end])
                for event in events:
                    if event["event"] == "handoff_started":
                        with self.assertRaises(ProcessLookupError):
                            os.kill(event["worker_pid"], 0)

    def test_file_only_arguments_and_bounded_configuration(self):
        self.assertIn("file-only Rust", subprocess.check_output([str(HANDOFF_BINARY), "--version"], text=True))
        for args in ([], ["--microphone"], ["one", "two", "three"]):
            result = subprocess.run([str(HANDOFF_BINARY), *args], text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("panicked", result.stderr)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dummy = root / "dummy"
            dummy.write_text("unused")
            config = dict.fromkeys(("detector_binary", "detector_config", "voice_binary", "voice_payload"), str(dummy))
            config.update(work_dir=folder, max_buffer_ms=10001)
            settings = root / "config.json"
            settings.write_text(json.dumps(config))
            result = subprocess.run([str(HANDOFF_BINARY), str(settings), str(dummy)], text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("1000–10000", result.stderr)
            self.assertFalse(list(root.glob("buffer-*")))

    def test_continuous_mode_rejects_ambiguous_or_unbounded_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            unused = root / "unused"
            unused.write_text("not a native binary")
            payload = root / "payload.json"
            payload.write_text(json.dumps({"config": {"recognition": {"closest_match": True}}}))
            base = dict.fromkeys(("detector_binary", "detector_config", "voice_binary"), str(unused))
            base.update(voice_payload=str(payload), work_dir=folder,
                        quiet_endpoint=dict(rms=.001, min_speech_ms=240, quiet_ms=600, timeout_ms=5000))
            candidate = dict(max_windows=4, worker_timeout_ms=5000, recovery_quiet_ms=600)
            for patch in ({"cycles": 2}, {"quiet_endpoint": None},
                          {"continuous": dict(candidate, max_windows=0)},
                          {"continuous": dict(candidate, max_windows=33)},
                          {"continuous": dict(candidate, worker_timeout_ms=0)},
                          {"continuous": dict(candidate, recovery_quiet_ms=0)}):
                settings = root / "config.json"
                settings.write_text(json.dumps(dict(base, continuous=candidate) | patch))
                result = subprocess.run([str(HANDOFF_BINARY), str(settings), str(unused)],
                                        text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Continuous mode needs", result.stderr)
                self.assertNotIn("panicked", result.stderr)
                self.assertFalse(list(root.glob("continuous-*.raw")))


if __name__ == "__main__":
    unittest.main()
