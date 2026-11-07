"""Local command-model discovery and explicit, checksum-verified setup.

Discovery never downloads. Installation is a separate user action.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import stat

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = "https://alphacephei.com/vosk/models/" + MODEL_NAME + ".zip"
# SHA-256 of the official HTTPS artifact independently downloaded during setup.
MODEL_SHA256 = "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MODEL_BYTES = 256 * 1024 * 1024
REQUIRED = ("am/final.mdl", "conf/model.conf", "graph/Gr.fst", "graph/HCLr.fst")


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def default_model_path() -> Path:
    from .host import app_dir, data_dir, is_windows
    bundled = app_dir() / "models" / MODEL_NAME
    if is_windows() and valid_model(bundled):
        return bundled
    return data_dir() / "models" / MODEL_NAME


# Moonshine Streaming Tiny Arabic (MIT), exported by scripts/export_moonshine_arabic.py.
ARABIC_NAME = "moonshine-streaming-tiny-ar"
ARABIC_FILES = ("encoder.onnx", "cross.onnx", "decoder.onnx", "tokenizer.json")
ARABIC_SHA256 = "df959ea0723c07733ac5996f0f7580a8bdf5612f09caf381332a7debd946cfd5"
ARABIC_URL = "https://github.com/abdullaho21/jarvis/releases/download/models-arabic-1/moonshine-streaming-tiny-ar.tar.bz2"
MAX_ARABIC_BYTES = 160 * 1024 * 1024


def arabic_model_dir() -> Path:
    from .host import app_dir, data_dir, is_windows
    bundled = app_dir() / "models" / ARABIC_NAME
    if is_windows() and all((bundled / f).is_file() for f in ARABIC_FILES):
        return bundled
    return data_dir() / "models" / ARABIC_NAME


def arabic_model_ready() -> bool:
    folder = arabic_model_dir()
    return all((folder / name).is_file() for name in ARABIC_FILES)


def install_arabic(archive: Path | None = None) -> Path:
    """Explicit setup of the Arabic model: a local .tar.bz2 or the pinned download."""
    import tempfile
    destination = arabic_model_dir()
    if arabic_model_ready():
        return destination
    if destination.exists():
        raise RuntimeError(f"Incomplete Arabic model at {destination}; review and remove it first")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".model-", dir=destination.parent) as temporary:
        if archive is None:
            import urllib.request
            archive = Path(temporary) / "arabic.tar.bz2"
            request = urllib.request.Request(ARABIC_URL, headers={"User-Agent": "Jarvis-model-setup/0.3"})
            with urllib.request.urlopen(request, timeout=30) as response, archive.open("wb") as output:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARABIC_BYTES:
                        raise RuntimeError("Arabic model download exceeds its expected size")
                    output.write(chunk)
        if archive.stat().st_size > MAX_ARABIC_BYTES or _sha256(archive) != ARABIC_SHA256:
            raise RuntimeError("Arabic model archive checksum mismatch")
        _extract_arabic(archive, Path(temporary), destination)
    return destination


def _extract_arabic(archive: Path, temporary: Path, destination: Path) -> None:
    import tarfile
    with tarfile.open(archive, "r:bz2") as bundle:
        wanted = {f"{ARABIC_NAME}/{name}" for name in (*ARABIC_FILES, "LICENSE", "README.md")}
        for member in bundle.getmembers():
            if member.name in wanted and member.isfile():
                bundle.extract(member, temporary, filter="data")
        staged = Path(temporary) / ARABIC_NAME
        if not all((staged / name).is_file() for name in ARABIC_FILES):
            raise RuntimeError("Arabic model archive is incomplete")
        staged.rename(destination)


def valid_model(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in REQUIRED)


def describe(config: dict) -> dict:
    settings = config.get("recognition", {})
    from .native_worker import binary_path, vosk_library
    rust = binary_path() is not None
    accurate = (rust or importlib.util.find_spec("sherpa_onnx") is not None) and accurate_model_ready(selected_model(config))
    requested = settings.get("backend", "auto")
    path = Path(settings.get("model_path") or default_model_path()).expanduser()
    ready = (vosk_library() is not None if rust else importlib.util.find_spec("vosk") is not None) and valid_model(path)
    selected = "vosk" if requested == "vosk" or (requested == "auto" and ready) else "pocketsphinx"
    return {"backend": selected, "model_path": str(path), "model_ready": ready,
            "accurate_model_ready": accurate, "accurate_model_on": bool(settings.get("accurate", True)),
            "speech_model": settings.get("speech_model", "light"),
            "note": "Vosk: better with accents; about 200 MB RAM while the microphone is on." if selected == "vosk" else
            "Basic PocketSphinx recognizer. Install the improved Vosk model (40 MB) for better accuracy."}


def install(archive: Path | None = None, destination: Path | None = None) -> Path:
    """Explicit setup; accept a local verified ZIP or download the pinned model.

    Existing valid models are reused. Unknown/incomplete directories are never
    overwritten. Staging and atomic publication share the destination filesystem.
    """
    import tempfile
    import urllib.request
    import zipfile
    destination = destination or default_model_path()
    if destination.exists():
        if valid_model(destination):
            return destination
        raise RuntimeError(f"Incomplete model at {destination}; choose another location or remove it after review")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".model-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        zipped = staging / "model.zip"
        if archive is None:
            request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "Jarvis-model-setup/0.1"})
            with urllib.request.urlopen(request, timeout=30) as response, zipped.open("wb") as output:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ValueError("Model download exceeds 64 MiB")
                    output.write(chunk)
        else:
            if archive.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ValueError("Model archive exceeds 64 MiB")
            shutil.copyfile(archive, zipped)
        if _sha256(zipped) != MODEL_SHA256:
            raise ValueError("Model archive checksum mismatch; nothing was installed")
        with zipfile.ZipFile(zipped) as files:
            entries = files.infolist()
            if sum(item.file_size for item in entries) > MAX_MODEL_BYTES or len(entries) > 128:
                raise ValueError("Model archive is too large")
            names = set()
            for item in entries:
                path = Path(item.filename)
                if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != MODEL_NAME or item.filename in names:
                    raise ValueError("Invalid model archive path")
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("Model archive contains a symbolic link")
                names.add(item.filename)
            files.extractall(staging)
        model = staging / MODEL_NAME
        if not valid_model(model):
            raise ValueError("Model archive is missing required files")
        # Do not replace a model installed concurrently by another settings window.
        if destination.exists():
            if valid_model(destination):
                return destination
            raise RuntimeError("Another installation created an incomplete model destination")
        model.rename(destination)
    return destination


# --- Accurate command model (sherpa-onnx, CPU, offline) ----------------------
# Used only to transcribe the few seconds after the wake word. Much better than
# the small Vosk model at accents and unusual names. Archives come from the
# sherpa-onnx GitHub releases; SHA-256 values were taken from those files.
SHERPA_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/{section}/{name}"
ACCURATE_MODELS = {
    "whisper-small": {
        "archive": "sherpa-onnx-whisper-small.en", "kind": "whisper", "megabytes": 636,
        "sha256": "0cdba2b8aaab69e04847f3427cc9709574112e67913a1a84b7fec3a8729faa9a",
        "files": {"encoder": "small.en-encoder.int8.onnx", "decoder": "small.en-decoder.int8.onnx",
                  "tokens": "small.en-tokens.txt"},
    },
    # Light: about 93 MiB resident instead of 320 MiB. The int8 files match the
    # hashes pinned in docs/benchmarks/whisper-model-provenance-2026-10-26.json.
    "whisper-tiny": {
        "archive": "sherpa-onnx-whisper-tiny.en", "kind": "whisper", "megabytes": 113,
        "sha256": "2bd6cf965c8bb3e068ef9fa2191387ee63a9dfa2a4e37582a8109641c20005dd",
        "files": {"encoder": "tiny.en-encoder.int8.onnx", "decoder": "tiny.en-decoder.int8.onnx",
                  "tokens": "tiny.en-tokens.txt"},
    },
    "parakeet": {
        "archive": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8", "kind": "nemo_transducer", "megabytes": 483,
        "sha256": "157c157bc51155e03e37d2466522a3a737dd9c72bb25f36eb18912964161e1ad",
        "files": {"encoder": "encoder.int8.onnx", "decoder": "decoder.int8.onnx",
                  "joiner": "joiner.int8.onnx", "tokens": "tokens.txt"},
    },
}
DEFAULT_ACCURATE_MODEL = "whisper-small"
SPEECH_MODELS = {"small": "whisper-small", "light": "whisper-tiny"}


def selected_model(config: dict) -> str:
    """Catalog name of the Whisper model chosen in settings."""
    return SPEECH_MODELS[config.get("recognition", {}).get("speech_model", "light")]


def usable_model(config: dict) -> str:
    """The chosen Whisper model, or the other one if only that is installed
    (an install updated without downloading the new model)."""
    chosen = selected_model(config)
    if accurate_model_ready(chosen):
        return chosen
    other = next(name for name in SPEECH_MODELS.values() if name != chosen)
    return other if accurate_model_ready(other) else chosen
DENOISER = {"name": "gtcrn_simple.onnx", "section": "speech-enhancement-models", "megabytes": 1,
            "sha256": "e77603ac0c23dac3227dd2d7135b3a585cbee2679048aecfa886657d3ae1b534"}


def models_root() -> Path:
    return default_model_path().parent


def accurate_model_dir(name: str = DEFAULT_ACCURATE_MODEL) -> Path:
    return models_root() / ACCURATE_MODELS[name]["archive"]


def accurate_model_ready(name: str = DEFAULT_ACCURATE_MODEL) -> bool:
    folder = accurate_model_dir(name)
    return all((folder / f).is_file() for f in ACCURATE_MODELS[name]["files"].values())


def denoiser_path() -> Path:
    return models_root() / DENOISER["name"]


def _download(url: str, target: Path, sha256: str, limit: int, progress=None) -> None:
    """Stream to ``target`` while hashing; refuse oversize or mismatching files."""
    import hashlib
    import urllib.request
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "Jarvis-model-setup/0.2"})
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
        expected = int(response.headers.get("Content-Length") or 0)
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError("Download is larger than expected; stopped")
            digest.update(chunk)
            output.write(chunk)
            if progress and expected:
                progress(total / expected)
    if digest.hexdigest() != sha256:
        raise ValueError("Checksum mismatch; nothing was installed")


def install_accurate(name: str = DEFAULT_ACCURATE_MODEL, archive: Path | None = None, progress=None) -> Path:
    """Download (or take ``archive``), verify and unpack only the int8 model files."""
    import tarfile
    import tempfile
    spec = ACCURATE_MODELS[name]
    destination = accurate_model_dir(name)
    if accurate_model_ready(name):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    limit = int(spec["megabytes"] * 1.1 * 1024 * 1024)
    with tempfile.TemporaryDirectory(prefix=".model-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        packed = staging / "model.tar.bz2"
        if archive is None:
            url = SHERPA_URL.format(section="asr-models", name=spec["archive"] + ".tar.bz2")
            _download(url, packed, spec["sha256"], limit, progress)
        else:
            shutil.copyfile(archive, packed)
            if _sha256(packed) != spec["sha256"]:
                raise ValueError("Checksum mismatch; nothing was installed")
        wanted = {f"{spec['archive']}/{f}": f for f in spec["files"].values()}
        unpacked = staging / spec["archive"]
        unpacked.mkdir()
        with tarfile.open(packed, "r:bz2") as tar:
            for member in tar:
                if member.name in wanted:
                    if not member.isfile():
                        raise ValueError("Model archive entry is not a regular file")
                    source = tar.extractfile(member)
                    with source, (unpacked / wanted[member.name]).open("wb") as output:
                        shutil.copyfileobj(source, output)
        missing = [f for f in spec["files"].values() if not (unpacked / f).is_file()]
        if missing:
            raise ValueError("Model archive is missing: " + ", ".join(missing))
        if destination.exists():
            shutil.rmtree(destination)   # an incomplete earlier attempt; ours is verified
        unpacked.rename(destination)
    return destination


def install_denoiser(progress=None) -> Path:
    import tempfile
    target = denoiser_path()
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".model-", dir=target.parent) as temporary:
        staged = Path(temporary) / DENOISER["name"]
        _download(SHERPA_URL.format(section=DENOISER["section"], name=DENOISER["name"]), staged,
                  DENOISER["sha256"], 4 * 1024 * 1024, progress)
        staged.rename(target)
    return target
