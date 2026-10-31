#!/usr/bin/env python3
"""Copy upstream license files for every crate linked into a Rust executable.

Reads `cargo metadata` for one target platform, follows normal (non-dev,
non-build) dependencies from the root crate and copies each crate's LICENSE,
COPYING and NOTICE files plus a summary of names, versions and SPDX licenses.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess


def closure(metadata: dict) -> list[dict]:
    packages = {p["id"]: p for p in metadata["packages"]}
    nodes = {n["id"]: n for n in metadata["resolve"]["nodes"]}
    pending, seen = [metadata["resolve"]["root"]], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for dep in nodes[current]["deps"]:
            if any(kind["kind"] is None for kind in dep["dep_kinds"]):
                pending.append(dep["pkg"])
    seen.discard(metadata["resolve"]["root"])
    return sorted((packages[i] for i in seen), key=lambda p: (p["name"], p["version"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--target", default="x86_64-pc-windows-gnu")
    parser.add_argument("--cargo", default="cargo")
    args = parser.parse_args()
    metadata = json.loads(subprocess.run(
        [args.cargo, "metadata", "--format-version", "1", "--locked", "--offline",
         "--filter-platform", args.target, "--manifest-path", str(args.manifest)],
        check=True, capture_output=True, text=True).stdout)
    args.output.mkdir(parents=True, exist_ok=True)
    summary = []
    for package in closure(metadata):
        folder = Path(package["manifest_path"]).parent
        files = [p for p in sorted(folder.iterdir()) if p.is_file()
                 and p.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE", "UNLICENSE"))]
        if package["source"] is None:
            continue  # Jarvis' own workspace crates (MIT, LICENSE at the root)
        target = args.output / f"{package['name']}-{package['version']}"
        target.mkdir(exist_ok=True)
        for path in files:
            shutil.copyfile(path, target / path.name)  # registry files can predate 1980 (ZIP limit)
        summary.append(f"{package['name']} {package['version']}: {package['license'] or 'see files'}"
                       + ("" if files else " (no license file in the crate; SPDX expression only)"))
    (args.output / "CRATES.txt").write_text("\n".join(summary) + "\n")
    print(f"{len(summary)} crates -> {args.output}")


if __name__ == "__main__":
    main()
