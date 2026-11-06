"""Small, explicit FFmpeg command construction; no capture occurs in this module."""

from functools import lru_cache
from pathlib import Path
import subprocess
from ..encoding import validate_encoding
from ..host import child_options, tool
from ..recorder import RecorderError, capture_color

QUALITY = {"medium": 28, "high": 23, "very_high": 19, "ultra": 15}
VENDORS = {0x10DE: "nvenc", 0x1002: "amf", 0x8086: "qsv"}


def format_for(cfg, color):
    name = cfg.get("codec", "libx264").removesuffix("_10bit")
    codec = name if name in ("h264", "hevc", "av1") else "h264"
    return "hevc" if color == "hdr" and codec == "h264" else codec


# Process loopback (one program's sound, or all but it) needs this build.
PROCESS_LOOPBACK_BUILD = 20348


def audio_plan(cfg, replay=None, build=0):
    """Audio inputs for one capture: [(title or None, worker config)].

    Recordings and mixed replays use one mixed input. Separate replay tracks
    keep the game, one program and the microphone apart; Windows can exclude
    only one process tree from the game track, so one program is supported."""
    if not replay or replay.get("audio_tracks", "mixed") != "separate":
        return [(None, cfg)] if cfg.get("system_audio") or cfg.get("microphone") else []
    from ..replay import track_names, valid_app_name

    if build < PROCESS_LOOPBACK_BUILD:
        raise RecorderError("Separate replay tracks need Windows 11 (build 20348 or later). "
                            "Choose One mixed track in Replay settings.")
    apps = [a for a in replay.get("separate_apps", []) if valid_app_name(a)]
    if len(apps) > 1:
        raise RecorderError("On Windows, one program can have its own replay track. Keep one in Replay settings.")
    kinds = ([{"kind": "game", "app": apps[0] if apps else ""}] if replay.get("game_audio", True) else [])
    kinds += [{"kind": "app", "app": a} for a in apps]
    kinds += [{"kind": "microphone"}] if replay.get("microphone", True) else []
    return [(name, dict(cfg, track=track)) for name, track in zip(track_names(replay), kinds)]


def encoder_options(cfg, family, codec):
    q = QUALITY[cfg.get("quality_preset", "very_high")]
    bitrate = str(cfg.get("bitrate_mbps", 8) * 1000000)
    budget = cfg.get("rate_control", "quality") == "bitrate"
    ten = cfg.get("_ten_bit", False) or cfg.get("codec", "").endswith("_10bit") or cfg.get("color_mode") == "hdr"
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
                    {"medium": 55, "high": 75, "very_high": 90, "ultra": 100}[
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
    if ten:
        if not cfg.get("_hardware_frames"):
            args += ["-pix_fmt", "p010le"]
        if codec == "hevc":
            args += ["-profile:v", "main10"]
    return args


@lru_cache(maxsize=32)
def _probe(binary, stamp, family, codec, adapter, cpu, ten_bit=False):
    # A synthetic black frame verifies the encoder/driver without capturing a
    # screen or opening audio. Presence in -encoders alone is not sufficient.
    # NVENC refuses HEVC and AV1 below its minimum size, so the frame can't be tiny.
    cfg = {
        "codec": "libx264" if cpu else codec,
        "encoder": "cpu" if cpu else family,
        "rate_control": "bitrate",
        "_ten_bit": ten_bit,
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
        "color=black:s=320x180:r=1",
        "-vf",
        "format=p010le" if ten_bit else "format=nv12",
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
    ten_bit = cfg.get("codec", "").endswith("_10bit") or capture_color(cfg, display) == "hdr"
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
            ten_bit,
        ):
            return binary, family
    raise RecorderError(
        f"No usable {'CPU H.264' if cpu else codec.upper() + ' GPU'} encoder. Update the graphics driver, select another format, or explicitly choose CPU H.264. No CPU fallback was used."
    )


def color_labels(color):
    """The filter that labels frames. FFmpeg takes color details from the frames it
    receives over its own output options, and ddagrab calls its frames full-range RGB."""
    if color == "hdr":
        return "setparams=range=tv:colorspace=bt2020nc:color_primaries=bt2020:color_trc=smpte2084"
    return "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709"


def output_size(cfg, display):
    from ..encoding import RESOLUTIONS
    width, height = int(display.get("width") or 1920), int(display.get("height") or 1080)
    limit = RESOLUTIONS.get(cfg.get("resolution", "native"))
    if not limit:
        return width, height
    max_w, max_h = map(int, limit.split("x"))
    ratio = min(1.0, max_w / width, max_h / height)
    if ratio == 1:
        return width, height
    return max(2, int(width * ratio) // 2 * 2), max(2, int(height * ratio) // 2 * 2)


def command(cfg, display, binary, family, *, audio_pipes=()):
    """``audio_pipes``: [(pipe, title or None)], one audio track each."""
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
    ten_bit = color == "hdr" or cfg.get("codec", "").endswith("_10bit")
    width, height = output_size(cfg, display)
    scaled = (width, height) != (display.get("width", width), display.get("height", height))
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
    for pipe, _ in audio_pipes:
        args += ["-thread_queue_size", "64", "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", pipe]
    args += ["-map", "0:v:0"]
    for index, (_, title) in enumerate(audio_pipes):
        args += ["-map", f"{index + 1}:a:0"]
        if title:
            args += [f"-metadata:s:a:{index}", f"title={title}"]
    args += ["-c:a", "aac", "-b:a", "160k"] if audio_pipes else ["-an"]
    # Native-size 8-bit NVENC/AMF keep D3D11 frames on the GPU. Scaling and
    # SDR-to-10-bit conversion explicitly download/process frames; encoding
    # remains on the requested GPU, with no silent software fallback.
    hardware_frames = not (family in ("qsv", "mf", "cpu") or scaled or (ten_bit and color == "sdr"))
    if hardware_frames:
        args += ["-vf", color_labels(color)]
    else:
        filters = ["hwdownload", "format=x2bgr10le" if color == "hdr" else "format=bgra"]
        resize = f"scale={width}:{height}" if scaled else "scale"
        resize += ":" if scaled else "="
        # Name the conversion, so the labels below are true of the pixels.
        if color == "hdr":
            resize += "in_range=full:out_range=limited:in_color_matrix=bt2020:out_color_matrix=bt2020"
        else:
            resize += "in_range=full:out_range=limited:out_color_matrix=bt709"
        filters.append(resize)
        filters.append("format=" + ("p010le" if ten_bit else "yuv420p" if family == "cpu" else "nv12"))
        filters.append(color_labels(color))
        args += ["-vf", ",".join(filters)]
    if family == "qsv":
        args += [
            "-init_hw_device",
            f"qsv=encoder:hw,child_device={display['adapter']},child_device_type=d3d11va",
        ]
    args += encoder_options(dict(cfg, _ten_bit=ten_bit, _hardware_frames=hardware_frames), family, codec)
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
    verified_codec = codec + "_10bit" if ten_bit and color == "sdr" else codec
    return args, color, verified_codec
