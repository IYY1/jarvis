"""Shared build/install steps for checkout-local Rust executables."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def build(description, targets, completion):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--cargo", default="cargo", help="Cargo executable (default: PATH)")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--offline", action="store_true", help="Use cached Cargo dependencies")
    args = parser.parse_args()
    if sys.platform == "win32":
        targets = [(crate, binary + ".exe") for crate, binary in targets]
    root = Path(__file__).resolve().parents[1]
    # Build every target before publishing any executable into the checkout.
    for crate, _binary in targets:
        manifest = root / "native" / crate / "Cargo.toml"
        for action in (["test", "build"] if args.test else ["build"]):
            command = [args.cargo, action, "--release", "--locked", "--manifest-path", str(manifest)]
            if args.offline:
                command.append("--offline")
            subprocess.run(command, check=True)
    crate, binary = targets[-1]
    source = root / "native" / crate / "target/release" / binary
    subprocess.run([str(source), "--version"], check=True)
    destination = root / "src/murmur/bin"
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
