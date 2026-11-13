"""Shared build/install steps for checkout-local Rust executables."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def without_build_paths(env, root):
    """Rust writes source paths into a program (its panic messages), which names the
    builder's user folder. Name them by the project and Cargo instead, so a shipped
    program carries nothing about the PC that built it. CARGO_ENCODED_RUSTFLAGS keeps
    paths with spaces whole; any RUSTFLAGS already set are kept in front."""
    home = Path.home()
    cargo_home = Path(env.get("CARGO_HOME") or home / ".cargo")
    # The last matching prefix wins, so the narrower folders come after the home folder.
    remaps = [f"--remap-path-prefix={home}=~", f"--remap-path-prefix={cargo_home}=cargo",
              f"--remap-path-prefix={Path(root).resolve()}=jarvis"]
    flags = env.get("CARGO_ENCODED_RUSTFLAGS", "").split("\x1f") if env.get("CARGO_ENCODED_RUSTFLAGS") \
        else env.get("RUSTFLAGS", "").split()
    env = dict(env)
    env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join([flag for flag in flags if flag] + remaps)
    return env


def build(description, targets, completion):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--cargo", default="cargo", help="Cargo executable (default: PATH)")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--offline", action="store_true", help="Use cached Cargo dependencies")
    args = parser.parse_args()
    if sys.platform == "win32":
        targets = [(crate, binary + ".exe") for crate, binary in targets]
    root = Path(__file__).resolve().parents[1]
    env = without_build_paths(os.environ, root)
    # Build every target before publishing any executable into the checkout.
    for crate, _binary in targets:
        manifest = root / "native" / crate / "Cargo.toml"
        for action in (["test", "build"] if args.test else ["build"]):
            command = [args.cargo, action, "--release", "--locked", "--manifest-path", str(manifest)]
            if args.offline:
                command.append("--offline")
            subprocess.run(command, check=True, env=env)
    crate, binary = targets[-1]
    source = root / "native" / crate / "target/release" / binary
    subprocess.run([str(source), "--version"], check=True)
    destination = root / "src/jarvis/bin"
    destination.mkdir(exist_ok=True)
    for crate, binary in targets:
        source = root / "native" / crate / "target/release" / binary
        target = destination / binary
        temporary = target.with_suffix(".tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"Built {target}")
    print(completion)
