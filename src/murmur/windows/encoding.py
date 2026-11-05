"""Small, explicit FFmpeg command construction; no capture occurs in this module."""

from functools import lru_cache
from pathlib import Path
import subprocess
from ..encoding import validate_encoding
from ..host import child_options, tool
from ..recorder import RecorderError, capture_color

QUALITY = {"medium": 28, "high": 23, "very_high": 19}
VENDORS = {0x10DE: "nvenc", 0x1002: "amf", 0x8086: "qsv"}


def format_for(cfg, color):
    # 10-bit SDR is not implemented on Windows yet; those settings record 8-bit.
    name = cfg.get("codec", "libx264").removesuffix("_10bit")
    codec = name if name in ("h264", "hevc", "av1") else "h264"
    return "hevc" if color == "hdr" and codec == "h264" else codec


def encoder_options(cfg, family, codec):
    q = QUALITY[cfg.get("quality_preset", "very_high")]
    bitrate = str(cfg.get("bitrate_mbps", 8) * 1000000)
    budget = cfg.get("rate_control", "quality") == "bitrate"
    if family == "cpu":
        args = [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
        ]
        if budget:
            return args + [
                "-b:v",
                bitrate,
                "-maxrate",
                bitrate,
                "-bufsize",
                str(int(bitrate) * 2),
            ]
        return args + ["-crf", str(q)]
    args = ["-c:v", f"{codec}_{family}"]
    if family == "mf":
        args += [
            "-hw_encoding",
            "0"
            if cfg.get("encoder") == "cpu" or cfg.get("codec") == "libx264"
            else "1",
            "-rate_control",
            "cbr" if budget else "quality",
        ]
        if not budget:
            args += [
                "-quality",
                str(
                    {"medium": 55, "high": 75, "very_high": 90}[
                        cfg.get("quality_preset", "very_high")
                    ]
                ),
            ]
    elif family == "nvenc":
        args += ["-preset", "p4", "-tune", "hq", "-rc", "cbr" if budget else "constqp"]
        if not budget:
            args += ["-qp", str(q)]
    elif family == "amf":
        args += ["-quality", "speed", "-rc", "cbr" if budget else "cqp"]
        if not budget:
            args += ["-qp_i", str(q), "-qp_p", str(q)]
    elif family == "qsv":
        args += ["-preset", "veryfast"]
        if not budget:
            args += ["-global_quality", str(q)]
    else:
        raise RecorderError("Unsupported Windows encoder family")
    if budget:
        args += [
            "-b:v",
            bitrate,
            "-maxrate",
            bitrate,
            "-bufsize",
            str(int(bitrate) * 2),
        ]
    return args


@lru_cache(maxsize=32)
def _probe(binary, stamp, family, codec, adapter, cpu):
    # A synthetic black frame verifies the encoder/driver without capturing a
    # screen or opening audio. Presence in -encoders alone is not sufficient.
    cfg = {
        "codec": "libx264" if cpu else codec,
        "encoder": "cpu" if cpu else family,
        "rate_control": "bitrate",
    }
    args = [binary, "-hide_banner", "-loglevel", "error"]
    if family == "qsv":
        args += [
            "-init_hw_device",
            f"qsv=encoder:hw,child_device={adapter},child_device_type=d3d11va",
        ]
    args += [
        "-f",
        "lavfi",
        "-i",
        "color=black:s=128x128:r=1",
        "-vf",
        "format=nv12",
        "-frames:v",
        "1",
    ]
    args += encoder_options(cfg, family, codec) + ["-f", "null", "-"]
    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
            **child_options(),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def select_encoder(cfg, display, codec):
    binary = tool("ffmpeg")
    if not binary or not tool("ffprobe"):
        raise RecorderError(
            "FFmpeg/ffprobe is missing; reinstall the complete Windows package"
        )
    requested = cfg.get("encoder", "auto")
    cpu = cfg.get("codec", "libx264") == "libx264" or requested == "cpu"
    if cpu and codec != "h264":
        raise RecorderError(
            "CPU compatibility mode supports H.264 SDR; choose GPU encoding for HEVC, AV1 or HDR"
        )
    if cpu:
        families = ["cpu"]
    elif requested == "auto":
        preferred = VENDORS.get(display.get("vendor"))
        families = [preferred, "mf"] if preferred else ["nvenc", "amf", "qsv", "mf"]
    else:
        families = [requested]
    for family in families:
        if _probe(
            binary,
            Path(binary).stat().st_mtime_ns,
            family,
            codec,
            display["adapter"],
            cpu,
        ):
            return binary, family
    raise RecorderError(
        f"No usable {'CPU H.264' if cpu else codec.upper() + ' GPU'} encoder. Update the graphics driver, select another format, or explicitly choose CPU H.264. No CPU fallback was used."
    )


def command(cfg, display, binary, family, *, audio_pipe=None):
    validate_encoding(cfg)
    color = capture_color(cfg, display)
    if (
        color == "hdr"
        and display.get("hdr_known")
        and display.get("colorManagementPreset") != "hdr"
    ):
        raise RecorderError(
            "Enable HDR for the selected display in Windows Settings before HDR capture"
        )
    if not display.get("hdr_known") and cfg.get("color_mode", "auto") == "auto":
        raise RecorderError(
            "Windows could not report the display color mode. Check HDR settings and explicitly choose SDR or HDR."
        )
    codec = format_for(cfg, color)
    fps = cfg.get("fps", 30)
    args = [
        binary,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-n",
        "-init_hw_device",
        f"d3d11va=capture:{display['adapter']}",
        "-filter_hw_device",
        "capture",
        "-f",
        "lavfi",
        "-i",
        f"ddagrab=output_idx={display['output']}:framerate={fps}:output_fmt={'10bit' if color == 'hdr' else '8bit'}:allow_fallback=0",
    ]
    if audio_pipe:
        args += [
            "-thread_queue_size",
            "64",
            "-f",
            "s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-i",
            audio_pipe,
        ]
    args += ["-map", "0:v:0"] + (
        ["-map", "1:a:0", "-c:a", "aac", "-b:a", "160k"] if audio_pipe else ["-an"]
    )
    # NVIDIA/AMD consume the captured D3D11 frames directly. Compatibility and
    # QSV use an explicit download/conversion, never an accidental CPU encoder.
    if family in ("qsv", "mf", "cpu"):
        if color == "hdr":
            args += [
                "-vf",
                "hwdownload,format=x2bgr10le,scale=in_range=full:out_range=limited:in_color_matrix=bt2020:out_color_matrix=bt2020,format=p010le",
            ]
        else:
            args += [
                "-vf",
                "hwdownload,format=bgra,format="
                + ("yuv420p" if family == "cpu" else "nv12"),
            ]
    if family == "qsv":
        args += [
            "-init_hw_device",
            f"qsv=encoder:hw,child_device={display['adapter']},child_device_type=d3d11va",
        ]
    args += encoder_options(cfg, family, codec)
    args += ["-g", str(fps), "-bf", "0", "-force_key_frames", "expr:gte(t,n_forced*1)"]
    if color == "hdr":
        args += [
            "-color_primaries",
            "bt2020",
            "-color_trc",
            "smpte2084",
            "-colorspace",
            "bt2020nc",
            "-color_range",
            "tv",
        ]
    return args, color, codec
