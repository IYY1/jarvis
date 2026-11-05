"""User-added apps and discovery filters; all in temporary folders."""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from murmur import app_catalog
from murmur.apps import AppRegistry, utility


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
            found = app_catalog.appimage_candidates(known_exec=str(folder / "Known.AppImage"))
        self.assertEqual([p.name for p in found], ["New.AppImage"])

    def test_installers_and_uninstallers_are_not_apps(self):
        for name in ("Uninstall Wine Software", "Steam Setup", "Foo Updater", "Bar-Uninstaller"):
            self.assertTrue(utility(name), name)
        for name in ("Steam", "Settings", "Install Manager Pro"):
            self.assertFalse(utility(name), name)


if __name__ == "__main__":
    unittest.main()
