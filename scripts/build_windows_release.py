#!/usr/bin/env python3
"""Build the Windows installer and portable ZIP from Linux, end to end.

Rust helpers are cross-compiled for x86_64-pc-windows-gnu with MinGW-w64.
PyInstaller runs under Windows CPython in an isolated Wine prefix on a private
Xvfb display, then NSIS packages the frozen folder. No network access is used;
the toolchain, Wine prefix, models and FFmpeg build are explicit local inputs
(see docs/windows.md and packaging/container/Containerfile).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TARGET = "x86_64-pc-windows-gnu"
RUST_HELPERS = (("voice-worker", "jarvis-voice"), ("replay-store", "jarvis-replay-store"),
                ("text-input", "jarvis-type"))


def windows_path(path: Path) -> str:
    return "Z:" + str(path.resolve()).replace("/", "\\")


def run(argv, *, env=None, log=None, cwd=None):
    print("+", " ".join(map(str, argv)), flush=True)
    if log is None:
        subprocess.run(argv, env=env, cwd=cwd, check=True)
        return
    with log.open("w") as stream:
        result = subprocess.run(argv, env=env, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        sys.stderr.write(log.read_text(errors="replace")[-4000:])
        raise SystemExit(f"{argv[0]} failed; see {log}")


def target_dir(root: Path, crate: str) -> Path:
    """Each crate's own target/ unless CARGO_TARGET_DIR redirects all of them."""
    shared = os.environ.get("CARGO_TARGET_DIR")
    return Path(shared) / crate if shared else root / "native" / crate / "target"


def cross_compile(root: Path, cargo: str, offline: bool) -> dict[str, Path]:
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER", "x86_64-w64-mingw32-gcc")
    built = {}
    for crate, binary in RUST_HELPERS:
        manifest = root / "native" / crate / "Cargo.toml"
        command = [cargo, "build", "--release", "--locked", "--target", TARGET, "--manifest-path", str(manifest)]
        run(command + (["--offline"] if offline else []), env=dict(env, CARGO_TARGET_DIR=str(target_dir(root, crate))))
        source = target_dir(root, crate) / TARGET / "release" / (binary + ".exe")
        stripped = root / "build/windows-release" / (binary + ".exe")
        stripped.parent.mkdir(parents=True, exist_ok=True)
        run(["x86_64-w64-mingw32-strip", "-o", str(stripped), str(source)])
        built[binary] = stripped
    return built


class Display:
    """A private X server so Wine never touches the user's desktop session."""

    def __init__(self, xvfb: Path, number: int):
        self.number, self.process = number, None
        self.xvfb = xvfb

    def __enter__(self):
        if Path(f"/tmp/.X11-unix/X{self.number}").exists():
            return self
        env = dict(os.environ)
        if not str(self.xvfb).startswith("/usr/"):
            env["LD_LIBRARY_PATH"] = str(self.xvfb.parents[1] / "lib")  # unpacked package
        self.process = subprocess.Popen([str(self.xvfb), f":{self.number}", "-nolisten", "tcp",
                                         "-screen", "0", "1280x800x24"], env=env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not Path(f"/tmp/.X11-unix/X{self.number}").exists():
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise SystemExit("Xvfb did not start")
            time.sleep(0.1)
        return self

    def __exit__(self, *_):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Checkout to build (default: this one)")
    parser.add_argument("--work", type=Path, default=ROOT / "build/windows",
                        help="Default folder for wine/, nsis/, xvfb/, ffmpeg-tools/ and FFmpeg-Corresponding-Source.tar.xz")
    parser.add_argument("--wine-prefix", type=Path, help="Wine prefix with Windows CPython in C:\\JarvisPython")
    parser.add_argument("--makensis", type=Path)
    parser.add_argument("--nsis-dir", type=Path, help="NSIS data folder (NSISDIR)")
    parser.add_argument("--xvfb", type=Path)
    parser.add_argument("--ffmpeg-tools", type=Path, help="Folder with ffmpeg.exe and ffprobe.exe")
    parser.add_argument("--ffmpeg-source", type=Path)
    parser.add_argument("--models", type=Path, default=Path.home() / ".local/share/jarvis/models")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/windows")
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--offline", action="store_true", help="Use cached Cargo dependencies")
    parser.add_argument("--no-voice-worker", action="store_true",
                        help="Older checkouts only: package without the Rust voice worker")
    parser.add_argument("--display", type=int, default=98)
    args = parser.parse_args()
    root, work, output = args.root.resolve(), args.work.resolve(), args.output.resolve()
    prefix = args.wine_prefix or work / "wine"
    makensis = args.makensis or work / "nsis/usr/bin/makensis"
    nsis_dir = args.nsis_dir or work / "nsis/usr/share/nsis"
    xvfb = args.xvfb or work / "xvfb/usr/bin/Xvfb"
    ffmpeg_tools = args.ffmpeg_tools or work / "ffmpeg-tools/bin"
    ffmpeg_source = args.ffmpeg_source or work / "FFmpeg-Corresponding-Source.tar.xz"
    sys.path.insert(0, str(root / "src"))
    from jarvis import __version__ as version
    logs = root / "build/windows-release"
    logs.mkdir(parents=True, exist_ok=True)
    if args.no_voice_worker:
        helpers = {}
        crate = root / "native/replay-store/Cargo.toml"
        env = dict(os.environ, CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER="x86_64-w64-mingw32-gcc")
        env["CARGO_TARGET_DIR"] = str(target_dir(root, "replay-store"))
        run([args.cargo, "build", "--release", "--locked", "--target", TARGET, "--manifest-path", str(crate)]
            + (["--offline"] if args.offline else []), env=env)
        helpers["jarvis-replay-store"] = target_dir(root, "replay-store") / TARGET / "release/jarvis-replay-store.exe"
    else:
        helpers = cross_compile(root, args.cargo, args.offline)
    licenses = logs / "licenses"
    if licenses.exists():
        shutil.rmtree(licenses)
    shutil.copytree(root / "packaging/windows/licenses", licenses)
    if "jarvis-voice" in helpers:
        run([sys.executable, str(root / "scripts/collect_rust_licenses.py"), "--cargo", args.cargo,
             str(root / "native/voice-worker/Cargo.toml"), str(licenses / "voice-worker")])
    output.mkdir(parents=True, exist_ok=True)
    for stale in output.iterdir():  # the folder itself may be a mount point
        shutil.rmtree(stale) if stale.is_dir() else stale.unlink()
    env = dict(os.environ, WINEPREFIX=str(prefix), WINEDEBUG="-all", WINEDLLOVERRIDES="mscoree,mshtml=",
               DISPLAY=f":{args.display}", WAYLAND_DISPLAY="", PYTHONPATH=windows_path(root / "src"))
    command = ["wine", r"C:\JarvisPython\python.exe", windows_path(root / "scripts/build_windows.py"),
               "--models", windows_path(args.models), "--tools", windows_path(ffmpeg_tools),
               "--ffmpeg-source", windows_path(ffmpeg_source),
               "--extra-licenses", windows_path(licenses),
               "--output", windows_path(output),
               "--replay-store", windows_path(helpers["jarvis-replay-store"])]
    if "jarvis-voice" in helpers:
        command += ["--voice-worker", windows_path(helpers["jarvis-voice"])]
    if "jarvis-type" in helpers:
        command += ["--text-helper", windows_path(helpers["jarvis-type"])]
    with Display(xvfb, args.display):
        run(command, env=env, log=logs / "freeze.log", cwd=root)
    setup = output / f"Jarvis-{version}-Windows-x64-Setup.exe"
    nsis = dict(os.environ, NSISDIR=str(nsis_dir))
    run([str(makensis), "-V3", f"-DBUNDLE={output / 'frozen/Jarvis'}",
         f"-DOUTPUT={setup}", f"-DVERSION={version}", str(root / "packaging/windows/installer.nsi")],
        env=nsis, log=logs / "nsis.log")
    import hashlib
    with setup.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    setup.with_name(setup.name + ".sha256").write_text(f"{digest}  {setup.name}\n")
    print(f"Built {setup} ({setup.stat().st_size / 1048576:.1f} MiB)\nSHA-256 {digest}")


if __name__ == "__main__":
    main()
