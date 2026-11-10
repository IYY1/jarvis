import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_build", ROOT / "scripts" / "native_build.py")
native_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native_build)


class BuildPathTests(unittest.TestCase):
    def test_rust_programs_name_the_project_not_the_builders_folders(self):
        env = native_build.without_build_paths({"RUSTFLAGS": "-Ctarget-feature=+crt-static",
                                                "CARGO_HOME": "/opt/my cargo"}, ROOT)
        flags = env["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
        self.assertEqual(flags[0], "-Ctarget-feature=+crt-static")   # flags already set are kept
        self.assertIn(f"--remap-path-prefix={Path('/opt/my cargo')}=cargo", flags)   # a space stays inside one flag
        self.assertEqual(flags[-1], f"--remap-path-prefix={ROOT}=jarvis")   # the narrowest folder wins: last
        self.assertEqual(flags[1], f"--remap-path-prefix={Path.home()}=~")


if __name__ == "__main__":
    unittest.main()
