"""Clip editor: edit operations, keyframe-aware Fast exports and real FFmpeg runs.

The FFmpeg tests generate small synthetic clips in a temporary folder; they
never read or write the user's recordings.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from jarvis.clipedit import Edit, EditError, Export, Source, command, probe


def source(duration=30.0, keyint=2.0, audio=1, codec="av1"):
    return Source(path="/clips/a.mp4", duration=duration,
                  video={"codec_name": codec, "width": 2560, "height": 1440, "pix_fmt": "yuv420p10le"},
                  audio=[{"codec_name": "opus", "channels": 2, "sample_rate": "48000", "name": f"Audio {i + 1}"}
                         for i in range(audio)],
                  keyframes=[i * keyint for i in range(int(duration / keyint) + 1)])


class EditTests(unittest.TestCase):
    def test_trim_cut_split_join_and_undo(self):
        edit = Edit.of(source())
        edit.trim(3.0, 20.0)
        edit.remove_range(5.0, 7.0)
        self.assertEqual([(s.start, s.end) for s in edit.segments], [(3.0, 8.0), (10.0, 20.0)])
        self.assertAlmostEqual(edit.duration, 15.0)
        edit.split(12.0)
        self.assertEqual(len(edit.segments), 3)
        edit.remove_segment(1)
        edit.append(source(duration=10.0))
        self.assertAlmostEqual(edit.duration, 5.0 + 3.0 + 10.0)
        while edit.undo():
            pass
        self.assertEqual([(s.start, s.end) for s in edit.segments], [(0.0, 30.0)])

    def test_refuses_to_remove_everything(self):
        edit = Edit.of(source())
        with self.assertRaises(EditError):
            edit.remove_range(0.0, 30.0)
        with self.assertRaises(EditError):
            edit.remove_segment(0)

    def test_fast_mode_starts_on_keyframes_and_reports_the_real_length(self):
        edit = Edit.of(source())
        edit.trim(3.0, 20.0)
        edit.remove_range(5.0, 7.0)   # keeps 3–8 and 10–20
        self.assertEqual(edit.fast_starts(), [2.0, 10.0])
        self.assertAlmostEqual(edit.fast_duration(), 16.0)

    def test_fast_mode_refuses_mismatched_clips_and_names_why(self):
        edit = Edit.of(source())
        edit.append(source(codec="h264"))
        problems = edit.fast_problems([0])
        self.assertTrue(any("codec" in p for p in problems))
        with tempfile.TemporaryDirectory() as work, self.assertRaisesRegex(EditError, "Choose Exact"):
            command(edit, Path(work) / "out.mp4", "fast", [0], False, Path(work))

    def test_fast_track_selection_copies_streams_and_mix_reencodes_audio_only(self):
        edit = Edit.of(source(audio=3))
        with tempfile.TemporaryDirectory() as work:
            argv = command(edit, Path(work) / "o.mp4", "fast", [0, 2], False, Path(work))
            self.assertIn("0:a:2", argv)
            self.assertNotIn("0:a:1", argv)
            self.assertEqual(argv[argv.index("-c") + 1], "copy")
            mixed = command(edit, Path(work) / "o.mp4", "fast", [0, 2], True, Path(work))
            self.assertIn("copy", mixed[mixed.index("-c:v") + 1])
            self.assertTrue(any("amix=inputs=2" in a for a in mixed))
            self.assertIn("-n", argv, "never overwrite")

    def test_exported_tracks_keep_their_names_and_a_mix_names_its_parts(self):
        clip = source(audio=3)
        for stream, name in zip(clip.audio, ("All sound", "Discord", "Microphone")):
            stream["name"] = name
        edit = Edit.of(clip)
        with tempfile.TemporaryDirectory() as work:
            argv = command(edit, Path(work) / "o.mp4", "fast", [1, 2], False, Path(work))
            self.assertEqual(argv[argv.index("-metadata:s:a:1") + 1], "title=Microphone")
            mixed = command(edit, Path(work) / "o.mp4", "fast", [1, 2], True, Path(work))
            self.assertEqual(mixed[mixed.index("-metadata:s:a:0") + 1], "title=Discord + Microphone")
            self.assertNotIn("-metadata:s:a:1", mixed)

    def test_tracks_are_named_from_the_file_when_no_names_were_saved(self):
        from jarvis.clipedit import track_names
        with tempfile.TemporaryDirectory() as work:
            clip = Path(work) / "recording.mkv"
            # A recording carries its own titles; a missing one falls back to its number.
            self.assertEqual(track_names(clip, 3, ["All sound", "", None]), ["All sound", "Audio 2", "Audio 3"])
            (Path(work) / ".recording.mkv.tracks.json").write_text(json.dumps({"tracks": ["A", "B", "C"]}))
            self.assertEqual(track_names(clip, 3, ["x", "y", "z"]), ["A", "B", "C"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs FFmpeg")
class ExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="jarvis-clip-test-"))
        self.addCleanup(shutil.rmtree, self.folder)

    def clip(self, name, size="320x240", tones=(440, 880)):
        path = self.folder / name
        argv = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30"]
        for f in tones:
            argv += ["-f", "lavfi", "-i", f"sine={f}:sample_rate=48000"]
        argv += ["-t", "6", "-map", "0", *sum((["-map", str(i + 1)] for i in range(len(tones))), []),
                 "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "libopus", "-shortest", str(path)]
        subprocess.run(argv, check=True)
        return path

    def streams(self, path):
        data = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout)
        return float(data["format"]["duration"]), [s["codec_type"] for s in data["streams"]]

    def test_fast_export_keeps_the_original_and_selected_tracks(self):
        original = self.clip("a.mp4")
        before = original.read_bytes()
        (self.folder / ".a.mp4.tracks.json").write_text(json.dumps({"tracks": ["Game", "Firefox"]}))
        clip = probe(original)
        self.assertEqual([a["name"] for a in clip.audio], ["Game", "Firefox"])
        edit = Edit.of(clip)
        edit.remove_range(2.0, 3.0)
        progress = []
        out = Export(edit, "fast", [0], False, progress=progress.append).run()
        self.assertEqual(out.name, "a edit 1.mp4")
        duration, kinds = self.streams(out)
        self.assertEqual(kinds, ["video", "audio"])
        self.assertAlmostEqual(duration, edit.fast_duration(), delta=0.2)
        self.assertEqual(progress[-1], 1.0)
        self.assertEqual(original.read_bytes(), before, "the original is never modified")
        again = Export(edit, "fast", [0, 1], False).run()
        self.assertEqual(again.name, "a edit 2.mp4", "an existing export is never overwritten")
        self.assertEqual(self.streams(again)[1], ["video", "audio", "audio"])

    def test_silent_tracks_are_found_in_one_pass(self):
        from jarvis.clipedit import SILENT_DB, loudness
        peaks = loudness(self.clip("quiet.mp4", tones=(440, 0, 880)), 3)   # a 0 Hz "tone" is silence
        self.assertEqual([peak <= SILENT_DB for peak in peaks], [False, True, False])
        self.assertEqual(loudness(self.folder / "quiet.mp4", 0), [])

    def test_tracks_are_described_like_losslesscut(self):
        try:
            from jarvis.clips_ui import describe
        except (ImportError, ValueError):
            self.skipTest("GTK is not available")
        self.assertEqual(describe({"codec_name": "opus", "channels": 2, "sample_rate": "48000"}), "Opus · stereo · 48 kHz")
        self.assertEqual(describe({"codec_name": "pcm_s16le", "channels": 1}), "PCM_S16LE · mono")

    def test_exact_export_converts_and_joins_different_clips(self):
        first, second = probe(self.clip("a.mp4")), probe(self.clip("b.mp4", size="640x360"))
        edit = Edit.of(first)
        edit.trim(0.5, 3.5)
        edit.append(second)
        out = Export(edit, "exact", [0, 1], True).run()
        duration, kinds = self.streams(out)
        self.assertEqual(kinds, ["video", "audio"])
        self.assertAlmostEqual(duration, 3.0 + second.duration, delta=0.3)


if __name__ == "__main__":
    unittest.main()
