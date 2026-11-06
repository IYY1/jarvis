"""Which video formats a display's GPU can actually encode, tested, not assumed.

Each GPU is checked by encoding 15 synthetic frames with FFmpeg (VA-API on
AMD/Intel, NVENC on NVIDIA). Nothing is captured from the screen. Listings
such as `ffmpeg -encoders` or `gpu-screen-recorder --info` only describe what
was compiled in, or (on multi-GPU systems) only the first GPU, so they are not
used to decide. Results are cached for the process.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import shutil
import subprocess

DRM = Path("/sys/class/drm")
VENDORS = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA"}
# Formats the recorder offers. 10-bit variants are what HDR capture needs.
CHECKS = {"h264": ("h264", "nv12", []), "hevc": ("hevc", "nv12", []), "av1": ("av1", "nv12", []),
          "hevc_10bit": ("hevc", "p010", ["-profile:v", "main10"]), "av1_10bit": ("av1", "p010", [])}


def render_nodes() -> list[str]:
    return sorted(str(Path("/dev/dri") / p.name) for p in DRM.glob("renderD*"))


def _card_of(node: str) -> Path | None:
    name = Path(node).name
    for card in DRM.glob("card[0-9]*"):
        if "-" not in card.name and (card / "device/drm" / name).exists():
            return card
    return None


def gpu_name(node: str) -> str:
    card = _card_of(node)
    vendor = (card / "device/vendor").read_text().strip() if card else ""
    return f"{VENDORS.get(vendor, 'GPU')} ({Path(node).name})"


def node_for_monitor(monitor: str) -> str | None:
    """The render node of the GPU the named output (e.g. DP-3) is connected to."""
    for connector in DRM.glob(f"card*-{monitor}"):
        card = DRM / connector.name.split("-", 1)[0]
        nodes = sorted((card / "device/drm").glob("renderD*"))
        if nodes:
            return str(Path("/dev/dri") / nodes[0].name)
    return None


def _encode(argv: list[str]) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@lru_cache(maxsize=8)
def probe_node(node: str) -> dict[str, bool]:
    """{format: works} for one GPU; empty when FFmpeg is missing."""
    if not shutil.which("ffmpeg"):
        return {}
    card = _card_of(node)
    nvidia = card is not None and (card / "device/vendor").read_text().strip() == "0x10de"
    result = {}
    for name, (codec, pixel, extra) in CHECKS.items():
        source = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-frames:v", "15"]
        if nvidia:
            argv = ["ffmpeg", "-hide_banner", "-v", "error", *source, "-vf", f"format={'p010le' if pixel == 'p010' else 'yuv420p'}",
                    "-c:v", f"{codec}_nvenc", *extra, "-f", "null", "-"]
        else:
            argv = ["ffmpeg", "-hide_banner", "-v", "error", "-vaapi_device", node, *source,
                    "-vf", f"format={pixel},hwupload", "-c:v", f"{codec}_vaapi", *extra, "-f", "null", "-"]
        result[name] = _encode(argv)
    return result


def formats_for_monitor(monitor: str) -> dict:
    """{"gpu": name, "node": path, "formats": {...}} for the display's GPU."""
    node = node_for_monitor(monitor) if monitor else None
    if node is None:
        nodes = render_nodes()
        node = nodes[0] if nodes else None
    if node is None:
        return {"gpu": "", "node": "", "formats": {}}
    return {"gpu": gpu_name(node), "node": node, "formats": probe_node(node)}


def recommended(formats: dict[str, bool]) -> str | None:
    """AV1 when the GPU encodes it, else HEVC, else H.264 (all on the GPU)."""
    return next((c for c in ("av1", "hevc", "h264") if formats.get(c)), None)
