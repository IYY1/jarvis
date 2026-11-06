"""Freeze and stage the Windows release. Explicit local assets; no downloads."""

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from jarvis import __version__ as VERSION

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument(
        "--tools",
        type=Path,
        required=True,
        help="Directory containing ffmpeg.exe and ffprobe.exe",
    )
    parser.add_argument(
        "--ffmpeg-source",
        type=Path,
        required=True,
        help="Complete corresponding-source tar.xz",
    )
    parser.add_argument("--extra-licenses", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/windows")
    parser.add_argument("--replay-store", type=Path,
                        default=ROOT / "src/jarvis/bin/jarvis-replay-store.exe",
                        help="Rust replay helper built with scripts/build_replay_store.py")
    parser.add_argument("--voice-worker", type=Path,
                        default=ROOT / "src/jarvis/bin/jarvis-voice.exe",
                        help="Rust voice worker for Windows (scripts/build_windows_release.py cross-compiles it)")
    parser.add_argument("--text-helper", type=Path,
                        default=ROOT / "src/jarvis/bin/jarvis-type.exe",
                        help="Dictation text helper for Windows (native/text-input, cross-compiled)")
    parser.add_argument("--vosk-dir", type=Path,
                        help="Folder with libvosk.dll and its MinGW runtime DLLs (default: installed vosk package)")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Run with Windows Python (a real Windows host or isolated Wine)")
    args.output.mkdir(parents=True, exist_ok=True)
    from jarvis.models import MODEL_NAME, ACCURATE_MODELS, DEFAULT_ACCURATE_MODEL

    required = [
        Path(MODEL_NAME) / p
        for p in ("am/final.mdl", "conf/model.conf", "graph/Gr.fst", "graph/HCLr.fst")
    ]
    # Both command models, so Settings can switch to Light without a download.
    whisper_models = [ACCURATE_MODELS[name] for name in (DEFAULT_ACCURATE_MODEL, "whisper-tiny")]
    for spec in whisper_models:
        required += [Path(spec["archive"]) / p for p in spec["files"].values()]
    # The release bundles the optional denoising model as well.
    required.append(Path("gtcrn_simple.onnx"))
    for path in required:
        if not (args.models / path).is_file():
            raise ValueError(f"Missing model file: {path}")
    for tool in ("ffmpeg.exe", "ffprobe.exe"):
        if not (args.tools / tool).is_file():
            raise ValueError(f"Missing Windows tool: {tool}")
    if not args.replay_store.is_file():
        raise ValueError("Build scripts/build_replay_store.py before packaging disk replay")
    if not args.voice_worker.is_file():
        raise ValueError("Build the Rust voice worker for Windows before packaging")
    if args.vosk_dir is None:
        import importlib.util
        spec = importlib.util.find_spec("vosk")
        if spec is None or not spec.origin:
            raise ValueError("Pass --vosk-dir or install the pinned vosk wheel in the build environment")
        args.vosk_dir = Path(spec.origin).parent
    vosk_dlls = sorted(args.vosk_dir.glob("*.dll"))
    if "libvosk.dll" not in {p.name for p in vosk_dlls}:
        raise ValueError(f"libvosk.dll is missing from {args.vosk_dir}")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--distpath",
            str(args.output / "frozen"),
            "--workpath",
            str(ROOT / "build/windows/pyinstaller"),
            str(ROOT / "packaging/windows/jarvis.spec"),
        ],
        check=True,
    )
    bundle = args.output / "frozen/Jarvis"
    shutil.copytree(args.tools, bundle / "tools", dirs_exist_ok=True)
    shutil.copy2(args.replay_store, bundle / "tools/jarvis-replay-store.exe")
    # libvosk.dll resolves its MinGW runtime DLLs from the same folder.
    shutil.copy2(args.voice_worker, bundle / "tools/jarvis-voice.exe")
    if args.text_helper.is_file():   # dictation stays unavailable without it
        shutil.copy2(args.text_helper, bundle / "tools/jarvis-type.exe")
    for dll in vosk_dlls:
        shutil.copy2(dll, bundle / "tools" / dll.name)
    shutil.copytree(
        args.models / MODEL_NAME, bundle / "models" / MODEL_NAME, dirs_exist_ok=True
    )
    for spec in whisper_models:
        # Only the int8 files the worker loads, not fp32 copies from the archive.
        target = bundle / "models" / spec["archive"]
        target.mkdir(parents=True, exist_ok=True)
        for name in spec["files"].values():
            shutil.copy2(args.models / spec["archive"] / name, target / name)
    shutil.copy2(args.models / "gtcrn_simple.onnx", bundle / "models/gtcrn_simple.onnx")
    shutil.copy2(args.ffmpeg_source, bundle / "FFmpeg-Corresponding-Source.tar.xz")
    shutil.copy2(ROOT / "LICENSE", bundle / "LICENSE")
    shutil.copy2(ROOT / "packaging/windows/README.txt", bundle / "README.txt")
    shutil.copy2(
        ROOT / "packaging/windows/THIRD-PARTY-NOTICES.txt",
        bundle / "THIRD-PARTY-NOTICES.txt",
    )
    shutil.copytree(args.extra_licenses, bundle / "licenses", dirs_exist_ok=True)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, bundle / "licenses/Python-LICENSE.txt")
    modules = set(json.loads((ROOT / "build/windows/pyinstaller/jarvis/bundled-modules.json").read_text()))

    def shipped(distribution):
        if distribution.metadata.get("Name", "").casefold() == "vosk":
            return True  # libvosk.dll and its runtimes are in tools/
        tops = (distribution.read_text("top_level.txt") or "").split()
        tops += [Path(f).parts[0].split(".")[0] for f in distribution.files or []
                 if not Path(f).parts[0].endswith((".dist-info", ".egg-info", ".data"))]
        return bool(modules & set(tops))

    distributions = [d for d in metadata.distributions() if shipped(d)]
    for distribution in distributions:
        name = distribution.metadata.get("Name", "unknown")
        for relative in distribution.files or []:
            if any(
                word in relative.name.casefold()
                for word in ("license", "copying", "notice")
            ):
                source = Path(distribution.locate_file(relative))
                if source.is_file():
                    target = (
                        bundle / "licenses" / name / str(relative).replace("..", "_")
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    manifest = {
        "version": VERSION,
        "target": "Windows 10 22H2 / Windows 11 x64",
        "discord_moderation": False,
        "python": sys.version,
        "dependencies": {d.metadata["Name"]: d.version for d in distributions},
        "files": {
            p.relative_to(bundle).as_posix(): digest(p)
            for p in sorted(bundle.rglob("*"))
            if p.is_file()
        },
    }
    (bundle / "BUILD-MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    archive = args.output / f"Jarvis-{VERSION}-Windows-x64-Portable.zip"
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as output:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(bundle.parent))
    (args.output / (archive.name + ".sha256")).write_text(
        digest(archive) + "  " + archive.name + "\n"
    )
    print("Portable release:", archive, flush=True)
    print("Build installer with NSIS, passing BUNDLE=" + str(bundle), flush=True)


if __name__ == "__main__":
    main()
