"""Screenshots: one frame of the recording display, saved as PNG or JPG.

The frame comes from the same Desktop Duplication path as video (FFmpeg's
ddagrab), in 8-bit: on an HDR display that is Windows' own SDR version of it.
The bundled FFmpeg has no PNG encoder, so a PNG is written here from its raw
RGB frame; a JPG comes straight from FFmpeg's MJPEG encoder.
"""
from __future__ import annotations

import os
from pathlib import Path
import struct
import subprocess
import time
import zlib

from ..recorder import RecorderError

FORMATS = ("png", "jpg")
DEFAULT_FOLDER = "~/Pictures/Jarvis"


def png_bytes(width: int, height: int, rgb: bytes) -> bytes:
    """A PNG of 8-bit RGB pixels, rows top to bottom, no filtering."""
    stride = width * 3
    if len(rgb) != stride * height:
        raise RecorderError("The screenshot frame was incomplete")
    rows = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)   # 8-bit RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b"")


def folder_for(recording: dict) -> Path:
    folder = Path(recording.get("screenshot_dir") or DEFAULT_FOLDER).expanduser()
    if recording.get("screenshot_game_folders", True):
        from ..games import focused_game
        folder = folder / (focused_game() or "")   # the game focused now gets its own folder
    return folder


def capture_args(display: dict, ffmpeg: str) -> list[str]:
    """FFmpeg reading one frame of ``display``, without the mouse pointer."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-init_hw_device", f"d3d11va=capture:{display['adapter']}", "-filter_hw_device", "capture",
            "-f", "lavfi", "-i",
            f"ddagrab=output_idx={display['output']}:framerate=30:output_fmt=8bit:draw_mouse=0:allow_fallback=0",
            "-frames:v", "1"]


def screens_in_order(displays: list[dict]) -> list[dict]:
    """Screen one is the main display (at the desktop's origin); the others follow
    from left to right, as they sit on the desk."""
    main = [d for d in displays if (d.get("left"), d.get("top")) == (0, 0)]
    rest = sorted((d for d in displays if d not in main), key=lambda d: (d.get("left", 0), d.get("top", 0)))
    return main[:1] + rest + main[1:]


def take(config: dict, screen: int | None = None) -> Path:
    """Save a screenshot of the recording display, or of screen ``screen`` (1 = main); its path."""
    from ..host import ENCODER_NAME, child_options, grouped, tool
    from .capture import chosen_display
    from .displays import sources

    recording = config["recording"]
    displays = sources()
    if screen is not None:
        ordered = screens_in_order(displays)
        if not 1 <= screen <= len(ordered):
            raise RecorderError(f"There is no screen {screen}: this PC has {len(ordered)}")
        display = ordered[screen - 1]
    else:
        display = chosen_display(displays, recording["monitor"], recording.get("monitor_name", ""))
    if display is None:
        raise RecorderError("Choose a connected display for screenshots")
    ffmpeg = tool("ffmpeg")
    if not ffmpeg:
        raise RecorderError("FFmpeg is missing. Reinstall the complete Jarvis app.")
    kind = recording.get("screenshot_format", "png")
    folder = folder_for(recording)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    path = folder / f"Screenshot_{stamp}.{kind}"
    n = 2
    while path.exists():
        path = folder / f"Screenshot_{stamp} ({n}).{kind}"
        n += 1
    staging = path.with_name(f".{path.name}.part")
    argv = capture_args(display, str(grouped(ffmpeg, ENCODER_NAME)))
    try:
        if kind == "jpg":
            argv += ["-vf", "hwdownload,format=bgra,format=yuvj444p", "-c:v", "mjpeg", "-q:v", "2",
                     "-f", "image2", "-y", str(staging)]
            result = subprocess.run(argv, capture_output=True, timeout=15, stdin=subprocess.DEVNULL, **child_options())
            if result.returncode or not staging.is_file():
                raise RecorderError("The screenshot failed: " + result.stderr.decode(errors="replace").strip()[-300:])
        else:
            argv += ["-vf", "hwdownload,format=bgra,format=rgb24", "-f", "rawvideo", "-"]
            result = subprocess.run(argv, capture_output=True, timeout=15, stdin=subprocess.DEVNULL, **child_options())
            if result.returncode:
                raise RecorderError("The screenshot failed: " + result.stderr.decode(errors="replace").strip()[-300:])
            staging.write_bytes(png_bytes(int(display["width"]), int(display["height"]), result.stdout))
        os.replace(staging, path)
    except subprocess.TimeoutExpired as exc:
        raise RecorderError("The screenshot took too long; try again") from exc
    finally:
        staging.unlink(missing_ok=True)
    return path
