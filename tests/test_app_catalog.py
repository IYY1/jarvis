"""User-added apps and discovery filters; all in temporary folders."""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from jarvis import app_catalog
from jarvis.apps import AppRegistry, utility


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="jarvis-apps-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home))
        patcher = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.home / "share")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def program(self, name, executable=True):
        path = self.home / name
        path.write_text("#!/bin/sh\n")
        if executable:
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_added_app_is_discovered_like_any_installed_app(self):
        path = self.program("My $App (x86).AppImage")
        identity = app_catalog.add(path, "Obsidian")
        registry = AppRegistry({}, roots=[self.home / "share/applications"])
        app = registry.resolve("obsidian")
        self.assertEqual(app.id, identity)
        # GIO launches the entry: GKeyFile unescapes the value, then the Exec
        # quoting is shell-unquoted. Both must give back exactly the file.
        try:
            from gi.repository import GLib
        except ImportError:
            self.skipTest("GLib is not available")
        keys = GLib.KeyFile()
        keys.load_from_file(app.path, GLib.KeyFileFlags.NONE)
        ok, argv = GLib.shell_parse_argv(keys.get_string("Desktop Entry", "Exec"))
        self.assertEqual(argv, [str(path)])
        self.assertEqual([a["name"] for a in app_catalog.added()], ["Obsidian"])
        with self.assertRaisesRegex(ValueError, "already added"):
            app_catalog.add(path, "Obsidian")
        app_catalog.remove(identity)
        self.assertEqual(app_catalog.added(), [])
        self.assertTrue(path.exists(), "removing the launcher never deletes the program")

    def test_refuses_non_executables_and_foreign_entries(self):
        with self.assertRaisesRegex(ValueError, "not executable"):
            app_catalog.add(self.program("tool", executable=False), "Tool")
        other = self.home / "share/applications/firefox.desktop"
        other.parent.mkdir(parents=True)
        other.write_text("[Desktop Entry]\nType=Application\nName=Firefox\nExec=firefox\n")
        with self.assertRaisesRegex(ValueError, "Only apps added in Jarvis"):
            app_catalog.remove("firefox.desktop")
        self.assertTrue(other.exists())

    def test_appimage_suggestions_skip_files_that_already_have_a_launcher(self):
        folder = self.home / "Applications"
        folder.mkdir()
        (folder / "Known.AppImage").write_text("")
        (folder / "New.AppImage").write_text("")
        with patch("pathlib.Path.home", return_value=self.home):
            found = app_catalog.program_candidates(known_exec=str(folder / "Known.AppImage"))
        self.assertEqual([p.name for p in found], ["New.AppImage"])

    def test_windows_suggests_portable_programs_but_not_installers(self):
        portable = self.home / "PortableApps" / "FirefoxPortable"
        portable.mkdir(parents=True)
        for name in ("FirefoxPortable.exe", "unins000.exe", "FirefoxSetup 120.exe", "notes.txt"):
            (portable / name).write_text("")
        (self.home / "Downloads").mkdir()
        (self.home / "Downloads" / "Tool.exe").write_text("")
        (self.home / "Downloads" / "Known.exe").write_text("")
        with patch("jarvis.app_catalog.is_windows", return_value=True), \
                patch("pathlib.Path.home", return_value=self.home), \
                patch.dict(os.environ, {"SystemDrive": str(self.home / "none")}):
            found = app_catalog.program_candidates(known_exec=str(self.home / "Downloads" / "Known.exe"))
        self.assertEqual([p.name for p in found], ["Tool.exe", "FirefoxPortable.exe"])

    def test_installers_and_uninstallers_are_not_apps(self):
        for name in ("Uninstall Wine Software", "Steam Setup", "Foo Updater", "Bar-Uninstaller"):
            self.assertTrue(utility(name), name)
        for name in ("Steam", "Settings", "Install Manager Pro"):
            self.assertFalse(utility(name), name)

    def test_add_app_command_adds_lists_and_removes_with_a_rescan(self):
        from argparse import Namespace
        import contextlib
        import io
        from jarvis.cli import run_add_app
        calls = []
        request = lambda op, **_: calls.append(op) or {"ok": True}
        path = self.program("Tool.AppImage")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            run_add_app(Namespace(target=str(path), name=None, remove=False), request)
            run_add_app(Namespace(target=None, name=None, remove=False), request)
        self.assertIn("Tool  ->", out.getvalue())
        self.assertEqual(calls, ["apps"])
        with self.assertRaisesRegex(ValueError, "No added program"):
            run_add_app(Namespace(target="Other", name=None, remove=True), request)
        with contextlib.redirect_stdout(io.StringIO()):
            run_add_app(Namespace(target="tool", name=None, remove=True), request)
        self.assertEqual(app_catalog.added(), [])

    def test_games_are_recognized_like_the_rust_controller_does(self):
        from jarvis import games
        self.assertEqual(games.game_from_paths("/home/u/.local/share/Steam/steamapps/common/Deadlock/game/bin/deadlock"), "Deadlock")
        self.assertEqual(games.game_from_paths("Z:\\home\\u\\steamapps\\common\\Elden Ring\\Game\\eldenring.exe"), "Elden Ring")
        self.assertEqual(games.game_from_paths("/home/u/Games/Heroic/Hades/Hades.exe"), "Hades")
        self.assertEqual(games.game_from_paths("C:\\Program Files\\Epic Games\\Fortnite\\FortniteClient.exe"), "Fortnite")
        self.assertIsNone(games.game_from_paths("/usr/lib/firefox/firefox"))
        # Store programs beside the games are not games.
        self.assertIsNone(games.game_from_paths(
            "C:\\Program Files\\Epic Games\\Launcher\\Portal\\Binaries\\Win64\\EpicGamesLauncher.exe"))
        self.assertIsNone(games.game_from_paths("C:\\Program Files (x86)\\Epic Games\\Epic Online Services\\x\\EOSOverlayRenderer.exe"))
        self.assertIsNone(games.game_from_paths("C:\\Steam\\steamapps\\common\\SteamVR\\bin\\win64\\vrmonitor.exe"))
        self.assertEqual(games.game_from_paths("C:\\Program Files\\Epic Games\\SomeGame\\Binaries\\Win64\\SomeGame.exe"), "SomeGame")
        self.assertEqual(games.game_folder("Half-Life: Alyx"), "Half-Life Alyx")
        self.assertIsNone(games.game_folder(" ../ "))
        clip = self.home / "Replay.mkv"
        clip.write_text("video")
        with patch("jarvis.games.focused_game", return_value="Deadlock"):
            moved = games.into_game_folder(clip)
        self.assertEqual((moved.parent.name, moved.read_text()), ("Deadlock", "video"))
        with patch("jarvis.games.focused_game", return_value=None):
            other = self.home / "Desktop clip.mkv"; other.write_text("x")
            self.assertEqual(games.into_game_folder(other), other)
        from jarvis.ui import clips
        self.assertEqual(sorted(c["name"] for c in clips(self.home)), ["Deadlock / Replay.mkv", "Desktop clip.mkv"])


if __name__ == "__main__":
    unittest.main()
