"""Windows policies run on Linux too; no live audio, desktop actions or services."""

import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import struct
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from jarvis import config, host
from jarvis.recorder import RecorderError
from jarvis.windows import encoding, transport
from jarvis.windows.audio import PCMStream, select_device
from jarvis.windows.apps import WindowsApp
from jarvis.windows.capture import Replay, ReplayBuffer, replay_budget_warning

try:
    import tkinter  # noqa: F401  (the Windows settings window needs Tk)
    TK = True
except ImportError:
    TK = False


@unittest.skipUnless(TK, "Tk is not installed")
class WindowRequestTests(unittest.TestCase):
    def test_requests_keep_the_client_per_operation_timeouts(self):
        from jarvis.windows.ui import Window
        expected = {"execute": 45, "record_start": 20, "status": 15,
                    "record_stop": 60, "save_config": 60}
        for op, timeout in expected.items():
            with self.subTest(op=op):
                runs = []
                window = types.SimpleNamespace(submit=lambda run, callback: runs.append(run), received=None)
                Window.call(window, op)
                with patch("jarvis.windows.ui.request", return_value={"ok": True}) as request:
                    runs[0]()
                self.assertEqual(request.call_args.kwargs["timeout"], timeout)


@unittest.skipUnless(TK, "Tk is not installed")
class SettingsWindowTests(unittest.TestCase):
    """The redesigned window: sidebar pages, settings that save on their own, Wada colors."""

    def setUp(self):
        import tkinter
        try:
            self.root = tkinter.Tk()
        except tkinter.TclError:
            self.skipTest("no display for Tk")
        self.root.withdraw()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        environment = patch.dict(os.environ, {"JARVIS_CONFIG": str(Path(folder.name) / "config.json")})
        environment.start()
        self.addCleanup(environment.stop)
        offline = patch("jarvis.windows.ui.request", side_effect=OSError("service offline"))
        offline.start()
        self.addCleanup(offline.stop)
        from jarvis.windows.ui import Window
        with patch.object(Window, "observe", lambda _window: None):   # no status watcher
            self.window = Window(self.root)
        self.addCleanup(self.root.destroy)

    def finish(self):
        """Let queued work end and deliver its results, as the Tk loop would."""
        self.window.executor.shutdown(wait=True)
        while not self.window.events.empty():
            callback, result, error = self.window.events.get_nowait()
            self.assertIsNone(error)
            if callback:
                callback(result)

    def test_every_page_opens_and_older_links_still_work(self):
        from jarvis.windows.ui import NAVIGATION
        for key, _title in NAVIGATION:
            self.window.show(key)
            self.assertEqual(self.window.current, key)
        for old, new in (("activation", "voice"), ("replay", "recording"), ("privacy", "general"), ("nope", "dashboard"),
                         ("applications", "commands"), ("diagnostics", "general")):
            self.window.show(old)
            self.assertEqual(self.window.current, new)

    def test_programs_with_sound_are_ticked_into_their_own_tracks(self):
        from jarvis.windows.ui import MAX_SEPARATE_APPS
        window = self.window
        found = [{"name": "Marvel-Win64-Shipping", "label": "Marvel Rivals", "playing": True},
                 {"name": "Discord", "label": "Discord", "playing": True},
                 {"name": "Spotify", "label": "Spotify", "playing": False}]
        window.separate_app.set("steam")   # ticked before, not playing now: still listed first
        window.show_sound_apps(found)
        boxes = window.sound_apps.winfo_children()
        self.assertEqual([b.cget("text") for b in boxes],
                         ["steam", "Marvel Rivals  · playing now", "Discord  · playing now", "Spotify"])
        self.assertEqual([str(b.instate(["selected"])) for b in boxes], ["True", "False", "False", "False"])
        with patch.object(window, "changed") as changed:
            boxes[1].invoke()   # tick Marvel Rivals
            self.assertEqual(window.separate_app.get(), "steam, Marvel-Win64-Shipping")
            boxes[0].invoke()   # untick steam
            self.assertEqual(window.separate_app.get(), "Marvel-Win64-Shipping")
            changed.assert_called()
        # Saving keeps the names, which validate.
        self.assertEqual(window.settings()["replay"]["separate_apps"], ["Marvel-Win64-Shipping"])
        # No more than the settings allow.
        window.separate_app.set(", ".join(f"app{i}" for i in range(MAX_SEPARATE_APPS)))
        window.show_sound_apps(found)
        marvel = next(b for b in window.sound_apps.winfo_children() if b.cget("text").startswith("Marvel"))
        with patch.object(window, "changed"), patch.object(window, "say") as said:
            marvel.invoke()
        self.assertFalse(marvel.instate(["selected"]))
        self.assertNotIn("Marvel", window.separate_app.get())
        said.assert_called()

    def status(self, **changes):
        return {"status": {"state": "idle", "muted": False, "microphone_open": True, "message": "",
                           "recording": {"state": "idle"}, "replay": {"state": "off"}, "dictation": {}, **changes}}

    def test_home_offers_the_one_action_that_fits_the_voice_state(self):
        home = self.window.home
        self.window.disconnected(None)
        self.assertEqual(home.action.cget("text"), "Start Jarvis")
        for changes, label in (({}, "Listen now"), ({"state": "listening"}, "Cancel"), ({"state": "paused", "muted": True}, "Unmute")):
            with self.subTest(changes=changes):
                self.window.received(self.status(**changes))
                self.assertEqual(home.action.cget("text"), label)

    def test_home_buttons_follow_recording_and_replay(self):
        home = self.window.home
        self.window.received(self.status())
        self.assertEqual(home.record_button.cget("text"), "Start")
        self.assertEqual(home.replay_button.cget("text"), "Start")
        self.assertFalse(home.replay_stop.winfo_manager())
        self.window.received(self.status(recording={"state": "recording"}, replay={"state": "on", "message": "Replay running"}))
        self.assertEqual(home.record_button.cget("text"), "Stop")
        self.assertEqual(home.replay_button.cget("text"), "Save clip")
        self.assertTrue(home.replay_stop.winfo_manager())

    def test_home_shows_when_the_budget_shortens_replay(self):
        warning = "About 46 of 120 seconds fit in this budget; raise the budget or lower the bitrate"
        self.window.received(self.status(replay={"state": "on", "message": f"Replay running · AV1 · {warning}"}))
        self.assertEqual(self.window.home.replay_text.get(), warning)

    def test_allowing_the_microphone_leaves_no_empty_heading(self):
        self.assertEqual(self.window.onboard_card.winfo_manager(), "grid")
        self.window.onboard_card.grid_remove()
        self.window.received(self.status())
        self.assertFalse(self.window.config["privacy"]["onboarded"])
        self.assertTrue(self.window.onboard_card.winfo_children()[0].cget("text"), "the heading lives inside the card")

    def test_hidden_pages_do_not_wrap_their_text(self):
        # A hidden page has no width of its own: wrapping there never settles.
        label = next(item for item in self.window.wrapped if not item.winfo_viewable())
        before = label.cget("wraplength")
        self.window.wrap(label)
        self.assertEqual(label.cget("wraplength"), before)

    def test_technical_video_options_stay_folded_until_asked_for(self):
        video = self.window.video
        self.assertTrue(all(not frame.grid_info() for frame in video.folds))
        video.fold()
        self.assertTrue(all(frame.grid_info() for frame in video.folds))
        self.assertEqual(video.toggle.cget("text"), "Hide options")
        video.fold()
        self.assertTrue(all(not frame.grid_info() for frame in video.folds))

    def test_saved_settings_name_their_preset(self):
        video, variable = self.window.video, self.window.variable
        for key, value in dict(codec="av1", resolution="native", fps=120, rate_control="bitrate", bitrate_auto=True).items():
            variable("recording", key).set(value)
        video.match("recording")
        self.assertEqual(video.presets["recording"].get(), "High frame rate · 120 fps")

    def test_replay_page_says_whether_the_budget_holds_the_length(self):
        video, variable = self.window.video, self.window.variable
        video.display = {"width": 2560, "height": 1440}
        for key, value in dict(codec="av1", fps=165, rate_control="bitrate", bitrate_auto=True).items():
            variable("recording", key).set(value)
        variable("replay", "own_video").set(False)
        variable("replay", "seconds").set("120")
        variable("replay", "max_mb").set("256")
        video.show_replay_fit()
        self.assertIn("only about 46 of 120 seconds", self.window.replay_fit.get())
        variable("replay", "max_mb").set("2048")
        video.show_replay_fit()
        self.assertIn("holds the full 120 seconds", self.window.replay_fit.get())

    def test_the_app_list_loads_itself_once_when_first_opened(self):
        self.window.online = True
        with patch.object(self.window, "call") as call:
            self.window.show("commands")
            self.window.show("dashboard")
            self.window.show("commands")
        call.assert_called_once_with("apps", self.window.show_apps)

    def test_changing_colors_redraws_without_errors(self):
        from jarvis import palettes
        for name in palettes.ALL_PRESETS:
            with self.subTest(name=name):
                self.window.palette = name
                self.window.apply_theme()
                self.root.update_idletasks()

    def test_clips_only_this_survives_late_silence_detection(self):
        from jarvis.clipedit import Source
        panel = self.window.clips
        pending = []
        panel.work = lambda run, done: pending.append((run, done))
        source = Source("clip.mkv", 3, {}, [
            {"name": "Firefox", "codec_name": "opus"},
            {"name": "Discord", "codec_name": "opus"}], [0])
        panel.loaded((source, None))
        panel.only(1)
        pending[0][1](([-91, -91], None))
        self.assertEqual(panel.selection(), [1], "a late scan must keep the user's choice")
        panel.keep_all(False)
        self.assertEqual(panel.selection(), [])
        panel.keep_all(True)
        self.assertEqual(panel.selection(), [0, 1])

    def test_clips_export_keeps_discord_and_mutes_firefox(self):
        import hashlib
        import shutil
        import subprocess
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("needs FFmpeg")
        with tempfile.TemporaryDirectory(prefix="jarvis-clip-ui-") as folder:
            path = Path(folder) / "clip's العربية.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=160x90:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
                "-t", "1", "-map", "0:v", "-map", "1:a", "-map", "2:a",
                "-c:v", "libx264", "-threads", "1", "-c:a", "aac", str(path)], check=True)
            (path.parent / f".{path.name}.tracks.json").write_text(json.dumps({"tracks": ["Firefox", "Discord"]}))
            original = hashlib.sha256(path.read_bytes()).hexdigest()
            panel = self.window.clips
            panel.work = lambda run, done: done((run(), None))
            panel.open(path)
            panel.only(1)
            panel.export_copy()
            exported = next(path.parent.glob("* edit 1.mkv"))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)
            data = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(exported)],
                                             capture_output=True, text=True, check=True).stdout)
            self.assertEqual(len([s for s in data["streams"] if s["codec_type"] == "audio"]), 1)
            def sound(file, track):
                return subprocess.run(["ffmpeg", "-v", "error", "-i", str(file), "-map", f"0:a:{track}",
                                       "-f", "hash", "-"], capture_output=True, check=True).stdout
            self.assertEqual(sound(exported, 0), sound(path, 1))
            self.assertNotEqual(sound(exported, 0), sound(path, 0))
            panel.keep_all(True)
            panel.mix.set(True)
            panel.export_copy()
            mixed = next(path.parent.glob("* edit 2.mkv"))
            metadata = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(mixed)],
                                                 capture_output=True, text=True, check=True).stdout)
            audio = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
            self.assertEqual(len(audio), 1)
            self.assertEqual(audio[0]["codec_name"], "aac")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)



    def test_windows_gpu_format_button_reports_all_probed_formats(self):
        display = {'name': self.window.variable('recording', 'monitor').get(), 'width': 2560, 'height': 1440}
        immediate = lambda **kw: types.SimpleNamespace(start=kw['target'])
        with patch('jarvis.windows.displays.sources', return_value=[display]), \
             patch('jarvis.windows.encoding.select_encoder', return_value=('ffmpeg', 'amf')) as probe, \
             patch('jarvis.windows.video_settings.threading.Thread', side_effect=immediate):
            self.window.video.test_formats()
        self.finish()
        self.assertEqual(len(probe.call_args_list), 5)
        self.assertTrue(self.window.video.formats['av1_10bit'])
        self.assertIn('Passed GPU tests', self.window.video.info.get())

    def test_start_recording_saves_reviewed_settings_before_capture(self):
        self.window.online = True
        self.window.variable('recording', 'fps').set('240')
        calls = []
        with patch('jarvis.windows.ui.save_config_restarting', side_effect=lambda value: calls.append(('save', value)) or {'ok': True}), \
             patch('jarvis.windows.ui.request', side_effect=lambda op, **kw: calls.append((op, kw)) or {'ok': True}):
            self.window.start_recording()
            self.finish()
        self.assertEqual([c[0] for c in calls], ['save', 'record_start'])
        self.assertTrue(calls[0][1]['recording']['configured'])
        self.assertEqual(calls[0][1]['recording']['fps'], 240)

    def test_windows_240fps_preset_and_replay_profile_save_independently(self):
        video = self.window.video
        video.formats = {"av1": True, "av1_10bit": True}
        video.presets['recording'].set('High frame rate · 240 fps')
        video.apply('recording')
        self.window.variable('replay', 'own_video').set(True)
        video.presets['replay'].set('Small files · 1080p')
        video.apply('replay')
        self.window.save()
        self.finish()
        saved = config.load()
        self.assertEqual(saved['recording']['fps'], 240)
        self.assertEqual(saved['recording']['codec'], 'auto')   # the GPU's best, chosen when capture starts
        self.assertTrue(saved['recording']['bitrate_auto'])
        self.assertTrue(saved['replay']['own_video'])
        self.assertEqual((saved['replay']['fps'], saved['replay']['resolution']), (30, '1080'))

    def test_windows_arabic_dictation_controls_save_and_dispatch(self):
        self.window.arabic_enabled.set(True)
        with patch('jarvis.windows.voice_controls.models.arabic_model_ready', return_value=True):
            self.window.voice_controls.toggle_arabic()
        self.window.variable('dictation', 'language').set('ar')
        self.window.voice_controls.stop_phrases.set('stop dictation, وقف الكتابة')
        self.window.variable('privacy', 'onboarded').set(True)
        self.window.save()
        self.finish()
        saved = config.load()
        self.assertEqual(saved['recognition']['language'], 'ar_en')
        self.assertEqual(saved['dictation']['language'], 'ar')
        self.assertEqual(saved['dictation']['stop_phrases'], ['stop dictation', 'وقف الكتابة'])
        with patch.object(self.window, 'call') as call:
            self.window.voice_controls.toggle()
        call.assert_called_once_with('dictation', action='toggle')

    def test_windows_clip_sections_split_remove_join_and_undo(self):
        from jarvis.clipedit import Source
        panel = self.window.clips
        panel.work = lambda run, done: None
        source = Source('clip.mkv', 10, {}, [], [0, 2, 4, 6, 8])
        panel.loaded((source, None))
        panel.start.set('2'); panel.end.set('4'); panel.change('remove')
        self.assertAlmostEqual(panel.edit.duration, 8)
        panel.change('undo')
        self.assertAlmostEqual(panel.edit.duration, 10)
        panel.start.set('5'); panel.end.set('10'); panel.change('split')
        self.assertEqual(len(panel.edit.segments), 2)
        panel.edit.append(source); panel.edited()
        self.assertAlmostEqual(panel.edit.duration, 20)


    def test_a_change_saves_on_its_own_while_the_service_is_off(self):
        self.window.variable("replay", "sound").set(False)
        self.window.variable("replay", "seconds").set("300")
        self.window.save()
        self.finish()
        saved = config.load()
        self.assertEqual((saved["replay"]["sound"], saved["replay"]["seconds"]), (False, 300))
        self.assertIn("Saved.", self.window.notice.get())

    def test_shown_values_and_invalid_ones_are_not_saved(self):
        self.window.loading = True
        self.window.changed(0)
        self.assertIsNone(self.window.save_timer)
        self.window.loading = False
        self.window.variable("replay", "seconds").set("ten")
        self.window.save()
        self.assertIn("Not saved", self.window.notice.get())

    def test_shortcuts_are_a_list_not_json(self):
        self.window.new_phrase.set("Capture That")
        self.window.new_command.set("start recording")
        self.window.add_shortcut()
        self.finish()
        self.assertEqual(config.load()["commands"], {"capture that": "start recording"})

    def test_color_presets_restyle_the_window(self):
        from jarvis import palettes
        self.window.choose_palette("190")
        self.assertEqual(self.window.style.lookup("TFrame", "background"), palettes.roles("190")["bg"])
        self.assertEqual(palettes.chosen(), "190")


class EncoderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.validate({})["recording"]
        self.cfg.update(codec="h264", rate_control="bitrate", bitrate_mbps=8)
        self.display = {
            "name": r"\\.\DISPLAY2",
            "adapter": 1,
            "output": 2,
            "vendor": 0x10DE,
            "hdr_known": True,
            "colorManagementPreset": "srgb",
        }

    def test_families_use_budget_and_never_implicit_software_fallback(self):
        for family in ("nvenc", "amf", "qsv", "mf", "cpu"):
            args = encoding.encoder_options(self.cfg, family, "h264")
            self.assertEqual(args[args.index("-b:v") + 1], "8000000")
            if family == "mf":
                self.assertEqual(args[args.index("-hw_encoding") + 1], "1")
            if family == "cpu":
                self.assertIn("libx264", args)

    def test_nvenc_keeps_one_reference_frame_in_every_codec_and_mode(self):
        for codec in ("h264", "hevc", "av1"):
            for mode in ("bitrate", "quality"):
                args = encoding.encoder_options(dict(self.cfg, rate_control=mode), "nvenc", codec)
                self.assertEqual(args[args.index("-dpb_size") + 1], "1")
                # NVENC H.264 rejects one reference frame while B-frames are on,
                # and the encoder test uses these options alone.
                self.assertEqual(args[args.index("-bf") + 1], "0")
        self.assertNotIn("-dpb_size", encoding.encoder_options(self.cfg, "amf", "h264"))

    def test_capture_uses_dxgi_adapter_and_output_not_monitor_number(self):
        args, color, codec = encoding.command(
            self.cfg, self.display, "ffmpeg.exe", "nvenc"
        )
        self.assertIn("d3d11va=capture:1", args)
        self.assertIn(
            "ddagrab=output_idx=2:framerate=60:output_fmt=8bit:allow_fallback=0", args
        )
        # Frames stay on the GPU, and carry the color they really have, not ddagrab's RGB.
        filters = args[args.index("-vf") + 1]
        self.assertNotIn("hwdownload", filters)
        self.assertIn("colorspace=bt709", filters)
        self.assertNotIn("-pix_fmt", args)
        self.assertEqual((color, codec), ("sdr", "h264"))

    def test_hdr_frames_are_labeled_as_hdr(self):
        display = dict(self.display, hdr_known=True, colorManagementPreset="hdr")
        args, color, _ = encoding.command(dict(self.cfg, codec="av1", color_mode="hdr"), display, "ffmpeg.exe", "nvenc", screen_pipe="p")
        filters = args[args.index("-vf") + 1]
        self.assertEqual(color, "hdr")
        self.assertIn("color_trc=smpte2084", filters)
        self.assertIn("colorspace=bt2020nc", filters)

    def test_software_conversion_names_the_matrix_it_labels(self):
        args, _, _ = encoding.command(dict(self.cfg, resolution="720"), dict(self.display, width=2560, height=1440), "ffmpeg.exe", "nvenc")
        filters = args[args.index("-vf") + 1]
        self.assertIn("hwdownload", filters)
        self.assertIn("out_color_matrix=bt709", filters)
        self.assertIn("colorspace=bt709", filters)

    def test_audio_is_only_added_for_explicit_pipe(self):
        args, _, _ = encoding.command(
            self.cfg,
            self.display,
            "ffmpeg.exe",
            "amf",
            audio_pipes=[(r"\\.\pipe\Jarvis-test", None)],
        )
        self.assertIn("s16le", args)
        self.assertIn("aac", args)
        self.assertEqual(args.count("-i"), 2)
        self.assertNotIn("-metadata:s:a:0", args)

    def test_separate_replay_tracks_get_their_own_inputs_and_titles(self):
        replay = config.validate({"replay": {"audio_tracks": "separate", "separate_apps": ["firefox"]}})["replay"]
        cfg = dict(self.cfg, system_audio=True, microphone=True)
        plan = encoding.audio_plan(cfg, replay, build=22631, labels={"Firefox": "Firefox browser"})
        self.assertEqual([title for title, _ in plan], ["Everything else", "Firefox browser", "Microphone"])
        self.assertEqual([c["track"] for _, c in plan],
                         [{"kind": "game", "app": "firefox", "in_mix": True},
                          {"kind": "app", "app": "firefox", "in_mix": True}, {"kind": "microphone", "in_mix": True}])
        pipes = [(rf"\\.\pipe\Jarvis-t-audio{i}", title) for i, (title, _) in enumerate(plan)]
        mix = encoding.mixed_inputs(plan)
        args, _, _ = encoding.command(self.cfg, self.display, "ffmpeg.exe", "amf", audio_pipes=pipes, mix=mix)
        self.assertEqual(args.count("-i"), 4)
        # First track: all three added up; then each on its own, named.
        graph = args[args.index("-filter_complex") + 1]
        self.assertTrue(graph.startswith("[1:a][2:a][3:a]amix=inputs=3:normalize=0"), graph)
        maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
        self.assertEqual(maps, ["0:v:0", "[mix]", "1:a:0", "2:a:0", "3:a:0"])
        self.assertEqual(args[args.index("-metadata:s:a:0") + 1], f"title={encoding.MIXED_TITLE}")
        self.assertEqual(args[args.index("-metadata:s:a:2") + 1], "title=Firefox browser")
        self.assertEqual(args[args.index("-aac_coder") + 1], "fast")   # a third less CPU per track
        # Without a program, the game track is all system sound.
        alone = dict(replay, separate_apps=[])
        self.assertEqual(encoding.audio_plan(cfg, alone, build=22631)[0][1]["track"],
                         {"kind": "game", "app": "", "in_mix": True})
        # Mixed replays and recordings keep one input with the existing mix, and no mixed copy.
        self.assertEqual(encoding.audio_plan(cfg, dict(replay, audio_tracks="mixed"), build=19045), [(None, cfg)])
        self.assertEqual(encoding.audio_plan(cfg), [(None, cfg)])
        self.assertEqual(encoding.mixed_inputs([(None, cfg)]), [])

    def test_several_programs_each_get_a_track(self):
        replay = config.validate({"replay": {"audio_tracks": "separate",
                                             "separate_apps": ["Discord", "Marvel-Win64-Shipping"]}})["replay"]
        cfg = dict(self.cfg, system_audio=True, microphone=True)
        plan = encoding.audio_plan(cfg, replay, build=26300, labels={"marvel-win64-shipping": "Marvel Rivals"})
        self.assertEqual([title for title, _ in plan], ["All desktop sound", "Discord", "Marvel Rivals", "Microphone"])
        self.assertEqual([c["track"]["kind"] for _, c in plan], ["game", "app", "app", "microphone"])
        # The desktop track already holds the programs: the mix adds desktop and microphone only.
        self.assertEqual(encoding.mixed_inputs(plan), [0, 3])
        # Game sound off: only the ticked programs and the microphone, all in the mix.
        only = encoding.audio_plan(cfg, dict(replay, game_audio=False), build=26300)
        self.assertEqual([title for title, _ in only], ["Discord", "Marvel-Win64-Shipping", "Microphone"])
        self.assertEqual(encoding.mixed_inputs(only), [0, 1, 2])

    def test_recordings_use_the_track_layout_with_their_own_switches(self):
        replay = config.validate({"replay": {"audio_tracks": "separate", "separate_apps": ["Discord"]}})["replay"]
        quiet = dict(self.cfg, system_audio=False, microphone=False)
        self.assertEqual(encoding.audio_plan(quiet, replay, build=26300, recording=True), [])
        voice = dict(self.cfg, system_audio=False, microphone=True)
        plan = encoding.audio_plan(voice, replay, build=26300, recording=True)
        self.assertEqual([title for title, _ in plan], ["Discord", "Microphone"])

    def test_separate_tracks_refuse_windows_10(self):
        replay = config.validate({"replay": {"audio_tracks": "separate", "separate_apps": ["firefox"]}})["replay"]
        with self.assertRaisesRegex(RecorderError, "Windows 11"):
            encoding.audio_plan(self.cfg, replay, build=19045)

    def test_app_audio_is_relayed_and_its_error_reported(self):
        from jarvis.windows import audio
        import subprocess
        import sys
        popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as folder:
            # A Python stand-in for jarvis-voice.exe, so this runs on Linux and Windows.
            worker = Path(folder) / "jarvis-voice.py"
            worker.write_text(
                "import sys\n"
                "open(sys.argv[0] + '.request', 'w').write(sys.stdin.readline())\n"
                "sys.stdout.buffer.write(b'PCMDATA!'); sys.stdout.flush()\n"
                "sys.stderr.write('{\"event\":\"error\",\"message\":\"firefox is not running.\"}\\n')\n"
                "sys.exit(1)\n")
            pipe = MagicMock()
            with patch("jarvis.native_worker.binary_path", return_value=worker), \
                    patch("jarvis.windows.pipes.PipeWriter", pipe), \
                    patch("subprocess.Popen", side_effect=lambda argv, **kw: popen([sys.executable, *argv], **kw)), \
                    patch("sys.stdout", io.StringIO()) as told:
                with self.assertRaisesRegex(RuntimeError, "firefox is not running"):
                    audio.track_to_pipe({"track": {"kind": "game", "app": "firefox"}}, r"\\.\pipe\Jarvis-x")
            self.assertEqual(told.getvalue(), "ready\n")   # it did flow, then the worker failed
            pipe.return_value.close.assert_called_once()
            self.assertEqual(json.loads((Path(folder) / "jarvis-voice.py.request").read_text()),
                             {"process": "firefox", "include": False})

    def test_audio_failure_names_the_track_and_reason(self):
        from jarvis.windows.capture import audio_failure
        process = types.SimpleNamespace(stderr=io.BytesIO(
            b"Traceback ...\nRuntimeError: firefox is not running. Start it before instant replay.\n"))
        self.assertEqual(audio_failure(process, "firefox"),
                         "The firefox track stopped: firefox is not running. Start it before instant replay.")
        self.assertIn("unexpectedly", audio_failure(types.SimpleNamespace(stderr=io.BytesIO(b"")), None))
        # Found in a real game test: the packaged worker's last line is PyInstaller's.
        packaged = types.SimpleNamespace(stderr=io.BytesIO(
            b"Traceback ...\nRuntimeError: Audio capture overflowed; reduce system load and try again\n"
            b"[PYI-32356:ERROR] Failed to execute script 'console' due to unhandled exception!\n"))
        self.assertEqual(audio_failure(packaged, None),
                         "Recording audio stopped: Audio capture overflowed; reduce system load and try again")
        # A program's track runs on the native worker, which reports in JSON.
        native = types.SimpleNamespace(stderr=io.BytesIO(
            b'{"event":"error","message":"Windows refused per-app audio (x). It needs Windows 11."}\n'))
        self.assertEqual(audio_failure(native, "Discord"),
                         "The Discord track stopped: Windows refused per-app audio (x). It needs Windows 11.")

    def test_program_tracks_start_the_native_worker_on_the_pipe(self):
        from jarvis.windows import audio
        started = []

        class Process:
            def __init__(self, argv, **options):
                started.append(argv)
                self.stdin = io.BytesIO()
                self.stdin.close = lambda: started.append(json.loads(self.stdin.getvalue()))

        with patch("jarvis.native_worker.binary_path", return_value=Path("jarvis-voice.exe")), \
                patch("jarvis.host.grouped", side_effect=lambda path, *_: path), \
                patch("subprocess.Popen", Process):
            audio.start_app_audio("Discord", True, r"\\.\pipe\Jarvis-t-audio1")
        self.assertEqual(started, [[str(Path("jarvis-voice.exe")), "--app-audio"],
                                   {"process": "Discord", "include": True, "pipe": r"\\.\pipe\Jarvis-t-audio1"}])

    def test_240fps_scaled_ten_bit_sdr_is_explicit_and_verified(self):
        self.cfg.update(codec='av1_10bit', fps=240, resolution='1080')
        self.display.update(width=3840, height=2160)
        args, color, codec = encoding.command(self.cfg, self.display, 'ffmpeg.exe', 'amf')
        self.assertTrue(any('framerate=480' in x for x in args))   # asks for more, keeps 240
        self.assertTrue(any(x.startswith('fps=240,hwdownload') for x in args))
        filters = args[args.index('-vf') + 1]
        self.assertIn('scale=1920:1080', filters)
        self.assertIn('format=p010le', filters)
        self.assertEqual(args[args.index('-pix_fmt') + 1], 'p010le')
        self.assertEqual((color, codec), ('sdr', 'av1_10bit'))
        self.assertEqual(args[args.index('-c:v') + 1], 'av1_amf')

    def test_resolution_limits_preserve_portrait_shape_and_never_upscale(self):
        self.assertEqual(encoding.output_size({'resolution': '1080'}, {'width': 1080, 'height': 1920}), (606, 1080))
        self.assertEqual(encoding.output_size({'resolution': '2160'}, {'width': 1280, 'height': 720}), (1280, 720))

    def test_hdr_promotes_h264_and_refuses_known_sdr_display(self):
        self.cfg["color_mode"] = "hdr"
        with self.assertRaises(RecorderError):
            encoding.command(self.cfg, self.display, "ffmpeg.exe", "nvenc")
        self.display["colorManagementPreset"] = "hdr"
        args, color, codec = encoding.command(
            self.cfg, self.display, "ffmpeg.exe", "nvenc", screen_pipe="p"
        )
        self.assertEqual((color, codec), ("hdr", "hevc"))
        for metadata in ("bt2020", "smpte2084", "bt2020nc"):
            self.assertIn(metadata, args)
        self.assertEqual(args[args.index("-pixel_format") + 1], "p010le")

    def test_auto_never_guesses_unknown_hdr_state(self):
        self.display["hdr_known"] = False
        with self.assertRaises(RecorderError):
            encoding.command(self.cfg, self.display, "ffmpeg.exe", "nvenc")

    def test_auto_prefers_capture_adapter_vendor_then_hardware_mf(self):
        with (
            patch.object(encoding, "tool", return_value=__file__),
            patch.object(encoding, "_probe", side_effect=[False, True]) as probe,
        ):
            _, family = encoding.select_encoder(self.cfg, self.display, "h264")
        self.assertEqual(family, "mf")
        self.assertEqual(
            [call.args[2] for call in probe.call_args_list], ["nvenc", "mf"]
        )

    def test_no_driver_does_not_silently_switch_to_cpu(self):
        with (
            patch.object(encoding, "tool", return_value=__file__),
            patch.object(encoding, "_probe", return_value=False) as probe,
        ):
            with self.assertRaises(RecorderError):
                encoding.select_encoder(self.cfg, self.display, "h264")
        self.assertNotIn("cpu", [call.args[2] for call in probe.call_args_list])

    def test_cpu_choice_is_explicit_and_sdr_h264_only(self):
        self.cfg["encoder"] = "cpu"
        with (
            patch.object(encoding, "tool", return_value=__file__),
            patch.object(encoding, "_probe", return_value=True) as probe,
        ):
            self.assertEqual(
                encoding.select_encoder(self.cfg, self.display, "h264")[1], "cpu"
            )
            self.assertEqual(probe.call_args.args[2], "cpu")
            with self.assertRaises(RecorderError):
                encoding.select_encoder(self.cfg, self.display, "hevc")


class FoundOnRealHardwareTests(unittest.TestCase):
    def test_the_encoder_probe_is_not_below_nvencs_minimum_size(self):
        encoding._probe.cache_clear()
        with patch("jarvis.windows.encoding.subprocess.run", return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertTrue(encoding._probe("ffmpeg", 1, "nvenc", "av1", 0, False, True))
        size = next(a for a in run.call_args.args[0] if a.startswith("color=black")).split("s=")[1].split(":")[0]
        width, height = map(int, size.split("x"))
        self.assertGreaterEqual(width, 160)
        self.assertGreaterEqual(height, 120)
        encoding._probe.cache_clear()

    def test_store_app_windows_are_recognized_by_package_across_updates(self):
        from jarvis.windows.apps import WindowsApp
        from jarvis.windows.desktop import package_family
        path = r"C:\Program Files\WindowsApps\Microsoft.WindowsNotepad_11.2607.14.0_x64__8wekyb3d8bbwe\Notepad\Notepad.exe"
        family = package_family(path)
        self.assertEqual(family, "microsoft.windowsnotepad_8wekyb3d8bbwe")
        app = WindowsApp("Microsoft.WindowsNotepad_8wekyb3d8bbwe!App", "Notepad", "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App", "")
        self.assertIn(family, app.classes())

    def test_ordinary_programs_have_no_package(self):
        from jarvis.windows.desktop import package_family
        self.assertIsNone(package_family(r"C:\Program Files\Steam\steam.exe"))
        self.assertIsNone(package_family(r"C:\Program Files\WindowsApps\odd-folder\app.exe"))

    def test_unmute_is_never_mistaken_for_mute(self):
        from jarvis.commands import Intent, Parser
        for phrase in ("unmute volume", "unmute the volume", "unmute", "unmute the sound"):
            with self.subTest(phrase=phrase):
                self.assertEqual(Parser().parse(phrase), Intent("volume_unmute"))
        self.assertEqual(Parser().parse("mute volume"), Intent("volume_mute"))


class BufferTests(unittest.TestCase):
    def test_out_of_order_pipes_are_reassembled_in_capture_order(self):
        buffer = ReplayBuffer(10)
        buffer.append(b"b", 1)
        buffer.append(b"c", 2)
        with self.assertRaises(RecorderError):
            buffer.snapshot()
        buffer.append(b"a", 0)
        self.assertEqual(buffer.snapshot(), (b"a", b"b", b"c"))

    def test_duration_and_ram_caps_evict_oldest(self):
        buffer = ReplayBuffer(2, max_bytes=5)
        for index, block in enumerate((b"aa", b"bb", b"ccc")):
            buffer.append(block, index)
        self.assertEqual(buffer.snapshot(), (b"bb", b"ccc"))
        self.assertEqual(buffer.size, 5)
        buffer.append(b"dddd", 3)
        self.assertEqual(buffer.snapshot(), (b"dddd",))

    def test_snapshot_shares_immutable_blocks_and_survives_eviction(self):
        buffer = ReplayBuffer(1)
        block = bytes(bytearray(b"x" * 20))
        buffer.append(block, 0)
        saved = buffer.snapshot()
        buffer.append(b"new", 1)
        self.assertIs(saved[0], block)
        self.assertEqual(saved, (block,))

    def test_duplicates_large_gaps_and_oversized_segments_fail(self):
        for sequence in (-1, 3):
            with self.assertRaises(RecorderError):
                ReplayBuffer(10).append(b"a", sequence)
        buffer = ReplayBuffer(10, max_bytes=2)
        buffer.append(b"a", 0)
        with self.assertRaises(RecorderError):
            buffer.append(b"a", 0)
        with self.assertRaises(RecorderError):
            buffer.append(b"big", 1)

    def test_save_waits_for_current_segment_and_has_a_deadline(self):
        buffer = ReplayBuffer(10)
        buffer.append(b"old", 0)
        after = time.monotonic()
        thread = threading.Thread(
            target=lambda: (time.sleep(0.02), buffer.append(b"current", 1))
        )
        thread.start()
        self.assertEqual(buffer.snapshot(after=after, timeout=1)[-1], b"current")
        thread.join()
        with self.assertRaises(RecorderError):
            buffer.snapshot(after=time.monotonic() + 1, timeout=0.01)


class ReplaySaveTests(unittest.TestCase):
    def test_failed_verification_reports_the_retained_partial_clip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            segment = root / 'segment-0000.mkv'
            segment.write_bytes(b'generated segment')
            capture = Replay(config.validate({}))
            capture._buffer, capture._folder, capture._process = MagicMock(), root, MagicMock()
            capture._buffer.files.return_value.__enter__.return_value = (segment,)

            def remux(argv, **kwargs):
                Path(argv[-1]).write_bytes(b'completed clip retained for inspection')
                return types.SimpleNamespace(returncode=0)

            with patch('jarvis.windows.capture.tool', return_value='fake-ffmpeg'), \
                    patch('jarvis.windows.capture.subprocess.run', side_effect=remux), \
                    patch('jarvis.windows.capture.verify_video', side_effect=RecorderError('invalid HDR metadata')):
                with self.assertRaisesRegex(RecorderError, 'invalid HDR metadata'):
                    capture.save()
            status = capture.status()
            self.assertIsNotNone(status['file'])
            self.assertEqual(Path(status['file']).read_bytes(), b'completed clip retained for inspection')
            self.assertIn('retained', status['message'])

    def saved(self, folder, segments, captured, seconds=None, length=120):
        """Save a fake replay; return its status message."""
        root = Path(folder)
        paths = [root / f'segment-{index:04d}.mkv' for index in range(segments)]
        for path in paths:
            path.write_bytes(b'segment')
        settings = config.validate({'replay': {'seconds': length}, 'recording': {'game_folders': False}})
        capture = Replay(settings)
        capture._buffer, capture._folder, capture._process = MagicMock(next=captured), root, MagicMock()
        capture._buffer.files.return_value.__enter__.return_value = tuple(paths)

        def remux(argv, **kwargs):
            Path(argv[-1]).write_bytes(b'clip')
            return types.SimpleNamespace(returncode=0)

        with patch('jarvis.windows.capture.tool', return_value='fake-ffmpeg'), \
                patch('jarvis.windows.capture.subprocess.run', side_effect=remux), \
                patch('jarvis.windows.capture.verify_video'), \
                patch('jarvis.windows.capture.write_track_names'):
            return capture.save(seconds)['message']

    def test_a_clip_the_budget_shortened_says_so(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIn('kept only 47 of 120 seconds', self.saved(folder, 47, captured=300))

    def test_full_clips_and_warm_up_do_not_warn_about_the_budget(self):
        for segments, captured, seconds in ((120, 300, None), (47, 47, None), (31, 300, 30)):
            with self.subTest(segments=segments, captured=captured, seconds=seconds), \
                    tempfile.TemporaryDirectory() as folder:
                self.assertNotIn('kept only', self.saved(folder, segments, captured, seconds))

    def test_a_requested_length_is_judged_against_that_length(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIn('kept only 47 of 60 seconds', self.saved(folder, 47, captured=300, seconds=60))

    def test_segments_are_joined_one_second_apart(self):
        # Found on the real card: left to step by each segment's longest stream, the
        # join slipped the picture ~25 ms a second behind the sound.
        with tempfile.TemporaryDirectory() as folder:
            self.saved(folder, 3, captured=3)
            listing = (Path(folder) / 'segments.txt').read_text(encoding='utf-8').splitlines()
        self.assertEqual(listing, ["file 'segment-0000.mkv'", 'duration 1', "file 'segment-0001.mkv'", 'duration 1',
                                   "file 'segment-0002.mkv'", 'duration 1'])


class ReplayBudgetWarningTests(unittest.TestCase):
    replay = {'max_mb': 256, 'seconds': 120}

    def test_a_budget_too_small_for_the_length_is_reported_at_start(self):
        warning = replay_budget_warning({'rate_control': 'bitrate', 'bitrate_mbps': 46}, self.replay, 2)
        self.assertIn('About 46 of 120 seconds', warning)

    def test_a_budget_that_fits_is_silent(self):
        for mbps, replay in ((8, self.replay), (17, {'max_mb': 512, 'seconds': 120})):
            with self.subTest(mbps=mbps):
                self.assertEqual(replay_budget_warning({'rate_control': 'bitrate', 'bitrate_mbps': mbps}, replay, 2), '')

    def test_quality_rate_control_makes_no_prediction(self):
        self.assertEqual(replay_budget_warning({'rate_control': 'quality', 'bitrate_mbps': 500}, self.replay, 2), '')


class TaskManagerGroupTests(unittest.TestCase):
    """Helpers run under the service's file name, so Task Manager shows one Jarvis row."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / "tools").mkdir()
        self.worker = self.root / "tools" / "jarvis-voice.exe"
        self.worker.write_bytes(b"MZ worker")
        self.patches = [patch.object(host, "is_windows", return_value=True),
                        patch.object(host, "data_dir", return_value=self.root / "data"),
                        patch.object(host.sys, "frozen", True, create=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.folder.cleanup()

    def test_a_helper_runs_as_a_hard_link_named_like_the_service(self):
        link = host.grouped(self.worker)
        self.assertEqual(link, self.root / "data" / "bin" / "jarvis-voice" / "JarvisConsole.exe")
        self.assertTrue(os.path.samefile(link, self.worker))
        self.assertEqual(host.grouped(self.worker), link)   # kept while it is the same file

    def test_a_replaced_program_gets_a_fresh_link_and_a_foreign_file_is_never_run(self):
        link = host.grouped(self.worker)
        self.worker.unlink()
        self.worker.write_bytes(b"MZ new version")
        self.assertTrue(os.path.samefile(host.grouped(self.worker), self.worker))
        link.unlink()
        link.write_bytes(b"MZ something else")   # not a link to the program any more
        self.assertTrue(os.path.samefile(host.grouped(self.worker), self.worker))
        self.assertEqual(link.read_bytes(), b"MZ new version")

    def test_a_program_without_a_name_gets_a_named_copy_and_the_original_never_changes(self):
        import shutil
        import sys
        import win32api
        program = self.root / "tools" / "ffmpeg.exe"
        shutil.copyfile(getattr(sys, "_base_executable", sys.executable), program)   # any real program
        original = program.read_bytes()
        old = host.grouped(program)   # an earlier version linked it: the same file as the original
        copy = host.grouped(program, "Jarvis video encoder")
        self.assertEqual(copy, old)
        self.assertEqual(program.read_bytes(), original)
        self.assertFalse(os.path.samefile(copy, program))
        lang, page = win32api.GetFileVersionInfo(str(copy), "\\VarFileInfo\\Translation")[0]
        self.assertEqual(win32api.GetFileVersionInfo(str(copy), f"\\StringFileInfo\\{lang:04X}{page:04X}\\FileDescription"),
                         "Jarvis video encoder")
        with patch.object(host, "_set_description") as stamped:
            self.assertEqual(host.grouped(program, "Jarvis video encoder"), copy)
        stamped.assert_not_called()   # kept until the program changes
        os.utime(program, ns=(1, 1))
        with patch.object(host, "_set_description") as stamped:
            host.grouped(program, "Jarvis video encoder")
        stamped.assert_called_once()

    def test_no_link_possible_means_the_original_path(self):
        with patch.object(host.os, "link", side_effect=OSError("cross-device")):
            self.assertEqual(host.grouped(self.worker), self.worker)
        with patch.object(host.sys, "frozen", False, create=True):
            self.assertEqual(host.grouped(self.worker), self.worker)


class AutomaticFormatTests(unittest.TestCase):
    """codec "auto": the best format the GPU encodes, so older GPUs record without a setting change."""
    sdr = {"name": r"\\.\DISPLAY2", "adapter": 0, "output": 1, "vendor": 0x10DE, "colorManagementPreset": "srgb",
           "hdr_known": True, "width": 2560, "height": 1440, "left": -2560, "top": 0}
    hdr = dict(sdr, colorManagementPreset="hdr")

    def resolve(self, display, works, **cfg):
        def select(candidate, _display, name):
            if name not in works:
                raise RecorderError(f"No usable {name} GPU encoder")
            return "ffmpeg.exe", "nvenc"
        with patch.object(encoding, "select_encoder", side_effect=select) as tried:
            return encoding.resolve_codec({"codec": "auto", "encoder": "auto", "color_mode": "auto", **cfg}, display), tried

    def test_newest_gpu_gets_av1_and_older_ones_step_down(self):
        for works, expected in ((("av1", "hevc", "h264"), "av1"), (("hevc", "h264"), "hevc"), (("h264",), "h264")):
            with self.subTest(works=works):
                cfg, _ = self.resolve(self.sdr, works)
                self.assertEqual(cfg["codec"], expected)
                self.assertEqual(cfg["encoder"], "auto")

    def test_no_gpu_encoder_falls_back_to_cpu_h264_for_ordinary_video_only(self):
        cfg, _ = self.resolve(self.sdr, ())
        self.assertEqual((cfg["codec"], cfg["encoder"]), ("libx264", "cpu"))
        with self.assertRaisesRegex(RecorderError, "HDR"):
            self.resolve(self.hdr, ("h264",), color_mode="hdr")

    def test_real_hdr_skips_h264(self):
        cfg, tried = self.resolve(self.hdr, ("hevc", "h264"), color_mode="hdr")
        self.assertEqual(cfg["codec"], "hevc")
        self.assertNotIn("h264", [call.args[2] for call in tried.call_args_list])

    def test_a_chosen_format_is_left_alone(self):
        with patch.object(encoding, "select_encoder") as tried:
            self.assertEqual(encoding.resolve_codec({"codec": "hevc_10bit"}, self.sdr)["codec"], "hevc_10bit")
        tried.assert_not_called()

    def test_new_windows_installs_default_to_auto_and_it_validates(self):
        defaults = config.validate({})
        self.assertEqual((defaults["recording"]["codec"], defaults["replay"]["codec"]), ("auto", "auto"))
        config.validate({"replay": {"codec": "auto", "own_video": True}})


class ClipExportTests(unittest.TestCase):
    """Exact export on Windows: the graphics card decodes and encodes, in the source's format."""

    def edit(self, folder, codec, transfer=None):
        from jarvis.clipedit import Edit, Source
        clip = Path(folder) / "clip.mkv"
        clip.write_bytes(bytes(2_000_000))
        video = {"codec_name": codec, "width": 2560, "height": 1440, "pix_fmt": "yuv420p", "r_frame_rate": "60/1"}
        if transfer:
            video["color_transfer"] = transfer
        return Edit.of(Source(path=str(clip), duration=8.0, video=video, audio=[], keyframes=[0.0]))

    def test_av1_is_decoded_and_encoded_on_the_card(self):
        from jarvis import clipedit
        nvenc = encoding.encoder_options({"rate_control": "bitrate", "bitrate_mbps": 2}, "nvenc", "av1")
        with tempfile.TemporaryDirectory() as folder, patch("jarvis.host.is_windows", return_value=True), \
                patch.object(clipedit, "_windows_encoder", return_value=nvenc) as chosen:
            argv = clipedit.command(self.edit(folder, "av1"), Path(folder) / "o.mkv", "exact", [], False, Path(folder))
        chosen.assert_called_once_with("av1", False, 2200)
        self.assertEqual(argv[argv.index("-i") - 2:argv.index("-i")], ["-hwaccel", "d3d11va"])
        self.assertEqual(argv[argv.index("-c:v") + 1], "av1_nvenc")
        self.assertNotIn("libx264", argv)
        self.assertNotIn("libsvtav1", argv)   # not in the Windows FFmpeg

    def test_hdr_without_a_card_encoder_says_so_instead_of_a_missing_encoder(self):
        from jarvis import clipedit
        with tempfile.TemporaryDirectory() as folder, patch("jarvis.host.is_windows", return_value=True), \
                patch.object(clipedit, "_windows_encoder", return_value=None):
            with self.assertRaisesRegex(clipedit.EditError, "graphics card"):
                clipedit.command(self.edit(folder, "hevc", "smpte2084"), Path(folder) / "o.mkv", "exact", [], False,
                                 Path(folder))


class IconTests(unittest.TestCase):
    def test_packaged_icon_is_the_window_mark_in_every_size(self):
        import struct
        from jarvis.windows.look import ICON_SIZES, logo_ico

        icon = logo_ico()
        stored = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "jarvis.ico"
        self.assertEqual(stored.read_bytes(), icon, "regenerate packaging/windows/jarvis.ico from look.logo_ico()")
        reserved, kind, count = struct.unpack("<HHH", icon[:6])
        self.assertEqual((reserved, kind, count), (0, 1, len(ICON_SIZES)))
        for index, size in enumerate(ICON_SIZES):
            width, height, *_, length, offset = struct.unpack("<BBBBHHII", icon[6 + 16 * index:22 + 16 * index])
            self.assertEqual((width or 256, height or 256), (size, size))
            png = icon[offset:offset + length]
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", png[16:24]), (size, size))
            self.assertEqual(png[25], 6, "RGBA, so the corners are see-through")


class ServiceMemoryTests(unittest.TestCase):
    def releases(self, state, storage, rounds=3):
        import asyncio
        import gc
        from jarvis import daemon

        service = types.SimpleNamespace(replay=MagicMock(), config={'replay': {'storage': storage}})
        service.replay.status.return_value = {'state': state}
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > rounds:
                raise asyncio.CancelledError

        released = MagicMock()
        with patch('jarvis.windows.system.release_unused_memory', released), \
                patch.object(daemon.asyncio, 'sleep', sleep), patch.object(gc, 'freeze'):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(daemon.keep_memory_low(service))
        return released.call_count

    def test_released_at_start_and_every_ten_minutes_without_a_ram_replay(self):
        self.assertEqual(self.releases('off', 'ram'), 3)
        self.assertEqual(self.releases('on', 'disk'), 3)

    def test_never_released_while_replay_holds_the_screen_in_ram(self):
        for state in ('on', 'starting', 'error'):
            with self.subTest(state=state):
                self.assertEqual(self.releases(state, 'ram'), 0)


class TransportTests(unittest.TestCase):
    def test_auth_is_removed_and_non_string_or_incorrect_secret_is_rejected(self):
        token = transport.new_token()
        self.assertEqual(len(token), 64)
        payload = {"op": "status", "auth": token}
        self.assertTrue(transport.authenticate(payload, token))
        self.assertNotIn("auth", payload)
        for auth in (None, True, token + "x", "wrong", "é" * 64):
            self.assertFalse(transport.authenticate({"auth": auth}, token))

    def test_endpoint_cannot_redirect_client_to_remote_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "endpoint.json"
            path.write_text(
                json.dumps({"host": "evil.example", "port": 1234, "token": "a" * 64})
            )
            with (
                patch.object(transport, "endpoint_path", return_value=path),
                patch.object(
                    transport.socket, "create_connection", return_value=object()
                ) as connect,
            ):
                transport.connect(2)
            connect.assert_called_once_with(("127.0.0.1", 1234), 2)

    def test_invalid_endpoint_never_connects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "endpoint.json"
            for data in (
                [],
                {},
                {"port": True, "token": "a" * 64},
                {"port": 65536, "token": "a" * 64},
                {"port": 1234, "token": "short"},
            ):
                path.write_text(json.dumps(data))
                with (
                    patch.object(transport, "endpoint_path", return_value=path),
                    patch.object(transport.socket, "create_connection") as connect,
                ):
                    with self.assertRaises((ConnectionError, ValueError)):
                        transport.connect(1)
                    connect.assert_not_called()
            path.write_bytes(b"x" * 1025)
            with patch.object(transport, "endpoint_path", return_value=path):
                with self.assertRaises(ConnectionError):
                    transport.connect(1)


class HostTests(unittest.TestCase):
    def test_new_windows_settings_are_gpu_budgeted_and_require_setup(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(host, "is_windows", return_value=True),
        ):
            value = config.load(Path(directory) / "absent.json")
        self.assertEqual(value["recording"]["codec"], "h264")
        self.assertEqual(value["recording"]["rate_control"], "bitrate")
        self.assertFalse(value["recording"]["configured"])
        self.assertFalse(value["privacy"]["onboarded"])

    def test_existing_settings_are_not_replaced_by_windows_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps({"recording": {"codec": "libx264", "quality": 17}})
            )
            with patch.object(host, "is_windows", return_value=True):
                value = config.load(path)
        self.assertEqual(value["recording"]["codec"], "libx264")
        self.assertEqual(value["recording"]["quality"], 17)

    def test_frozen_workers_use_console_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "JarvisConsole.exe").touch()
            with (
                patch.object(host.sys, "frozen", True, create=True),
                patch.object(host, "app_dir", return_value=root),
            ):
                self.assertEqual(
                    host.command("audio", "pipe"),
                    [str(root / "JarvisConsole.exe"), "--internal", "audio", "pipe"],
                )
                (root / "JarvisConsole.exe").unlink()
                with self.assertRaises(RuntimeError):
                    host.command("voice")

    def test_browser_shortcuts_never_match_every_window_of_the_browser(self):
        plain = WindowsApp("firefox", "Firefox", "", r"C:\Firefox\firefox.exe")
        site = WindowsApp(
            "webapp-id",
            "Web App",
            "",
            r"C:\Firefox\firefox.exe",
            wm_class="--app=example.com",
        )
        self.assertIn(r"c:\firefox\firefox.exe", plain.classes())
        self.assertEqual(site.classes(), {"webapp-id"})

    def test_discord_moderation_is_absent_from_shipped_source(self):
        root = Path(__file__).resolve().parents[1]
        for file in (
            "src/jarvis/discord_control.py",
            "src/jarvis/discord_commands.py",
            "native/controller/src/discord.rs",
        ):
            self.assertFalse((root / file).exists())
        self.assertNotIn("discord", config.DEFAULTS)


class StartupTests(unittest.TestCase):
    """Setup's start-at-sign-in component uses the same entry and setting as Settings."""

    def test_running_service_applies_the_change_itself(self):
        from jarvis.windows import startup
        with patch("jarvis.client.request", return_value={"ok": True}) as request, \
                patch.object(startup, "set_startup") as registry:
            self.assertEqual(startup.configure_startup(True), {"ok": True, "startup": True})
        request.assert_called_once_with("set_startup", enabled=True, timeout=5)
        registry.assert_not_called()

    def test_stopped_service_updates_run_entry_and_saved_setting(self):
        from jarvis.windows import startup
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("os.environ", JARVIS_CONFIG=str(Path(directory) / "config.json")), \
                patch("jarvis.client.request", side_effect=ConnectionRefusedError), \
                patch.object(startup, "set_startup") as registry:
            startup.configure_startup(True)
            self.assertTrue(config.load()["startup"])
            startup.configure_startup(False)
            self.assertFalse(config.load()["startup"])
        self.assertEqual([c.args for c in registry.call_args_list], [(True,), (False,)])

    def test_service_refusal_is_reported(self):
        from jarvis.windows import startup
        with patch("jarvis.client.request", return_value={"ok": False, "error": "nope"}), \
                patch.object(startup, "set_startup") as registry:
            with self.assertRaisesRegex(RuntimeError, "nope"):
                startup.configure_startup(False)
        registry.assert_not_called()


class AudioTests(unittest.TestCase):
    def stream(self, source_channels, target_channels):
        # Build the conversion stage directly; no PortAudio instance/device.
        stream = object.__new__(PCMStream)
        stream.source_channels = source_channels
        stream.target_channels = target_channels
        stream.source_rate = stream.target_rate = 48000
        stream.silence_on_idle = False
        stream.last_samples = stream.last_sound = time.monotonic()
        stream.state = None
        stream.pending = bytearray()
        stream.error = None
        stream.queue = queue.Queue()
        return stream

    @unittest.skipUnless(
        importlib.util.find_spec("audioop"),
        "Windows audio conversion dependency is optional on Linux",
    )
    def test_recording_preserves_left_and_right_channels(self):
        stream = self.stream(2, 2)
        data = struct.pack("<hhhh", 1000, -2000, 3000, -4000)
        stream.queue.put(data)
        self.assertEqual(stream.read(2), data)

    @unittest.skipUnless(
        importlib.util.find_spec("audioop"),
        "Windows audio conversion dependency is optional on Linux",
    )
    def test_microphone_stereo_is_downmixed_for_speech(self):
        stream = self.stream(2, 1)
        stream.queue.put(struct.pack("<hhhh", 1000, 3000, -1000, -3000))
        self.assertEqual(struct.unpack("<hh", stream.read(2)), (2000, -2000))

    @unittest.skipUnless(
        importlib.util.find_spec("audioop"),
        "Windows audio conversion dependency is optional on Linux",
    )
    def test_mono_recording_input_is_duplicated_to_both_channels(self):
        stream = self.stream(1, 2)
        stream.queue.put(struct.pack("<hh", 1000, -1000))
        self.assertEqual(
            struct.unpack("<hhhh", stream.read(2)), (1000, 1000, -1000, -1000)
        )

    @unittest.skipUnless(
        importlib.util.find_spec("audioop"),
        "Windows audio conversion dependency is optional on Linux",
    )
    def test_callback_error_is_not_silently_replaced_with_silence(self):
        stream = self.stream(1, 1)
        stream.error = RuntimeError("overflow")
        with self.assertRaisesRegex(RuntimeError, "overflow"):
            stream.read(1)

    def test_duplicate_device_names_are_refused(self):
        class Engine:
            def get_host_api_info_by_type(self, _):
                return {"index": 2}

            def get_device_count(self):
                return 2

            def get_device_info_by_index(self, i):
                return {"index": i, "name": "Mic", "hostApi": 2, "maxInputChannels": 1}

        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            select_device(Engine(), types.SimpleNamespace(paWASAPI=1), "input:Mic")

    def test_quiet_loopback_adds_timed_silence_but_microphone_never_does(self):
        stream = self.stream(2, 2)
        stream.silence_on_idle = True
        stream.last_samples = 10
        with (
            patch("jarvis.windows.audio._audioop", return_value=object()),
            patch("jarvis.windows.audio.time.monotonic", return_value=10.2),
            patch.object(stream.queue, "get", side_effect=queue.Empty),
        ):
            self.assertEqual(stream.read(960), bytes(960 * 4))
        self.assertEqual(len(stream.pending), (9600 - 960) * 4)
        microphone = self.stream(1, 1)
        with (
            patch("jarvis.windows.audio._audioop", return_value=object()),
            patch.object(microphone.queue, "get", side_effect=queue.Empty),
        ):
            with self.assertRaisesRegex(RuntimeError, "no audio"):
                microphone.read(480)

    def test_a_display_in_hdr_mode_makes_ordinary_video_unless_asked(self):
        from jarvis.windows.encoding import windows_color
        hdr = {"colorManagementPreset": "hdr", "hdr_known": True}
        self.assertEqual(windows_color({"color_mode": "auto"}, hdr), "sdr")
        self.assertEqual(windows_color({"color_mode": "sdr"}, hdr), "sdr")
        self.assertEqual(windows_color({"color_mode": "hdr"}, hdr), "hdr")
        self.assertEqual(windows_color({"color_mode": "auto"}, {"colorManagementPreset": "srgb"}), "sdr")

    def test_hdr_mode_reads_the_workers_picture_instead_of_grabbing_the_screen(self):
        from jarvis.windows.encoding import command, screen_request
        from jarvis.recorder import RecorderError
        hdr = {"name": "d", "output": 1, "adapter": 0, "width": 2560, "height": 1440, "hdr_known": True, "colorManagementPreset": "hdr"}
        cfg = {"codec": "av1", "fps": 165, "bitrate_mbps": 40, "rate_control": "bitrate", "color_mode": "auto", "resolution": "native"}
        with self.assertRaisesRegex(RecorderError, "screen capture"):
            command(cfg, hdr, "ffmpeg.exe", "nvenc")
        args, color, codec = command(cfg, hdr, "ffmpeg.exe", "nvenc", screen_pipe="the-pipe")
        self.assertEqual((color, codec), ("sdr", "av1"))
        self.assertFalse(any("ddagrab" in a for a in args))
        self.assertEqual(args[args.index("-pixel_format") + 1], "nv12")
        self.assertEqual(args[args.index("-video_size") + 1], "2560x1440")
        self.assertEqual(args[args.index("-framerate") + 1], "165")
        self.assertEqual(args[args.index("-i") + 1], "the-pipe")
        self.assertIn("colorspace=bt709", args[args.index("-vf") + 1])
        self.assertEqual(screen_request(cfg, hdr, "the-pipe", color), {
            "pipe": "the-pipe", "adapter": 0, "output": 1, "width": 2560, "height": 1440, "fps": 165, "hdr": False, "ten_bit": False})

        real = dict(cfg, color_mode="hdr")
        args, color, _ = command(real, hdr, "ffmpeg.exe", "nvenc", screen_pipe="the-pipe")
        self.assertEqual(color, "hdr")
        self.assertEqual(args[args.index("-pixel_format") + 1], "p010le")
        self.assertIn("smpte2084", args[args.index("-vf") + 1])
        self.assertTrue(screen_request(real, hdr, "the-pipe", color)["hdr"])

        small = dict(cfg, resolution="1080")
        args, _, _ = command(small, hdr, "ffmpeg.exe", "nvenc", screen_pipe="the-pipe")
        self.assertEqual(args[args.index("-video_size") + 1], "1920x1080")
        self.assertEqual(screen_request(small, hdr, "p", "sdr")["width"], 1920)

    def test_capture_can_prepare_on_either_kind_of_display(self):
        from jarvis import config as settings
        from jarvis.windows import capture
        cfg = settings.validate({})
        cfg["recording"].update(monitor="d", configured=True, codec="av1", fps=240, game_folders=False)
        for preset, refresh, fps in (("hdr", 240, 240), ("srgb", 144, 144)):
            display = {"name": "d", "output": 0, "adapter": 0, "width": 2560, "height": 1440, "left": 0, "top": 0, "vendor": 0x10DE,
                       "hdr_known": True, "refresh": refresh, "colorManagementPreset": preset}
            with tempfile.TemporaryDirectory() as folder, patch.object(capture, "sources", return_value=[display]), \
                    patch.object(capture.encoding, "select_encoder", return_value=("ffmpeg.exe", "nvenc")) as select:
                cfg["recording"]["output_dir"] = folder
                ready, chosen, binary, family, _ = capture.Recorder(cfg)._validated()
            self.assertIs(chosen, display)
            self.assertEqual(ready["fps"], fps)
            self.assertEqual(select.call_args.args[2], "av1")

    def test_screen_mode_failures_say_why(self):
        from jarvis.windows.capture import screen_failure, wait_for_screen
        from jarvis.recorder import RecorderError
        said = b'{"event":"error","message":"Capture of a rotated display isn\'t supported"}\n'
        self.assertIn("rotated", screen_failure(types.SimpleNamespace(stderr=io.BytesIO(said))))
        self.assertIn("unexpectedly", screen_failure(types.SimpleNamespace(stderr=io.BytesIO(b""))))
        ready = types.SimpleNamespace(stdout=io.BytesIO(b"ready\n"))
        wait_for_screen(ready)
        self.assertTrue(ready.stdout.closed)
        failed = types.SimpleNamespace(stdout=io.BytesIO(b""), stderr=io.BytesIO(said), wait=lambda timeout: 1)
        with self.assertRaisesRegex(RecorderError, "rotated"):
            wait_for_screen(failed)

    def test_no_chosen_display_means_the_primary_one(self):
        from jarvis.windows.capture import chosen_display
        left = {"name": "two", "left": -2560, "top": 0}
        primary = {"name": "one", "left": 0, "top": 0}
        self.assertIs(chosen_display([left, primary], ""), primary)
        self.assertIs(chosen_display([left, primary], "two"), left)
        self.assertIsNone(chosen_display([left, primary], "gone"))   # still says to choose one
        self.assertIs(chosen_display([left], ""), left)
        self.assertIsNone(chosen_display([], ""))

    def test_capture_asks_for_more_frames_than_it_keeps(self):
        from jarvis.windows.encoding import capture_rate, capped_fps
        self.assertEqual(capture_rate(60), 120)
        self.assertEqual(capture_rate(30), 60)
        for fps in (61, 144, 165, 240):
            self.assertEqual(capture_rate(fps), 480)   # 720 and above collapse
        self.assertEqual(capped_fps(240, {"refresh": 144}), 144)
        self.assertEqual(capped_fps(165, {"refresh": 240}), 165)
        self.assertEqual(capped_fps(240, {}), 240)   # unknown: trust the setting
        self.assertEqual(capped_fps(240, {"refresh": 0}), 240)

    def test_silence_keeps_the_sound_within_ffmpegs_limit(self):
        # FFmpeg discards video frames once video is 100 ms ahead of the sound.
        from jarvis.windows.audio import silence_wait
        self.assertLess(silence_wait(5), 0.1)
        self.assertGreater(silence_wait(0.1), silence_wait(5))   # a packet merely running late

    def test_discard_drops_what_nobody_is_listening_to(self):
        stream = self.stream(2, 2)
        stream.queue.put(b"old")
        stream.pending.extend(b"old")
        stream.last_samples = 1
        stream.discard()
        self.assertTrue(stream.queue.empty())
        self.assertEqual(stream.pending, bytearray())
        self.assertGreater(stream.last_samples, 1)

    def test_reader_gets_sound_at_once_when_it_connects(self):
        from jarvis.windows.audio import wait_for_reader
        stream = self.stream(2, 2)
        output = MagicMock(connected=True)
        with patch("sys.stdout", io.StringIO()) as told:
            wait_for_reader(output, [stream])
        self.assertEqual(told.getvalue(), "ready\n")
        output.connect.assert_called_once()
        output.write.assert_called_once_with(bytes(3840))

    def test_ffmpeg_waits_for_the_audio_worker(self):
        from jarvis.windows.capture import wait_for_audio
        from jarvis.recorder import RecorderError
        ready = types.SimpleNamespace(stdout=io.BytesIO(b"ready\n"))
        wait_for_audio(ready, None)
        self.assertTrue(ready.stdout.closed)

        failed = types.SimpleNamespace(stdout=io.BytesIO(b""), stderr=io.BytesIO(b"RuntimeError: no device\n"), wait=lambda timeout: 1)
        with self.assertRaisesRegex(RecorderError, "Recording audio stopped: no device"):
            wait_for_audio(failed, None)

        class Silent:
            def readline(self):
                time.sleep(5)
                return b""
        with self.assertRaisesRegex(RecorderError, "The firefox track did not start"):
            wait_for_audio(types.SimpleNamespace(stdout=Silent()), "firefox", seconds=0.05)


if __name__ == "__main__":
    unittest.main()
