import os
import unittest
from unittest.mock import patch

from jarvis.desktop_process import desktop_argv
from jarvis.commands import CommandError


class DesktopLifetimeTests(unittest.TestCase):
    def test_manual_launch_needs_no_systemd_scope(self):
        with patch.dict(os.environ, {"JARVIS_MANAGED_SERVICE": "0"}):
            self.assertEqual(desktop_argv(["gio", "launch", "/tmp/app.desktop"]),
                             ["gio", "launch", "/tmp/app.desktop"])

    def test_service_launch_uses_independent_scope_without_argument_expansion(self):
        with patch.dict(os.environ, {"JARVIS_MANAGED_SERVICE": "1"}), patch("shutil.which", return_value="/usr/bin/systemd-run"):
            result = desktop_argv(["gio", "launch", "/tmp/$value app.desktop"])
        self.assertIn("--scope", result)
        self.assertIn("--expand-environment=no", result)
        self.assertEqual(result[-3:], ["gio", "launch", "/tmp/$value app.desktop"])

    def test_service_launch_fails_clearly_without_scope_runner(self):
        with patch.dict(os.environ, {"JARVIS_MANAGED_SERVICE": "1"}), patch("shutil.which", return_value=None):
            with self.assertRaises(CommandError):
                desktop_argv(["gio", "launch", "/tmp/app.desktop"])
