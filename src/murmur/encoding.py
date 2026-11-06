"""Small shared recording choices; legacy settings keep their existing behavior."""
CODECS = ("libx264", "h264_vaapi", "h264", "hevc", "av1", "hevc_10bit", "av1_10bit")
CODEC_LABELS = ("H.264 · CPU (compatible)", "H.264 · VA-API (legacy)",
                "H.264 · GPU (easy sharing)", "HEVC · GPU (smaller files)",
                "AV1 · GPU (best compression, recent GPUs)", "HEVC 10-bit · GPU (no banding)",
                "AV1 10-bit · GPU (best quality, recent GPUs)")
# Formats GPU Screen Recorder encodes. The 10-bit ones record SDR without
# banding; HDR capture is always 10-bit whichever family is chosen.
GPU_CODECS = ("h264", "hevc", "av1", "hevc_10bit", "av1_10bit")
# "ultra" is the highest GPU Screen Recorder offers: larger files, no visible loss.
QUALITY_PRESETS = ("medium", "high", "very_high", "ultra")
# recording.resolution: the largest output size (mirrors capture.rs). Smaller
# displays are never scaled up; larger ones are scaled down keeping aspect.
RESOLUTIONS = {"native": "", "2160": "3840x2160", "1440": "2560x1440", "1080": "1920x1080", "720": "1280x720"}


def validate_encoding(cfg):
    for key, low, high in (("fps", 1, 240), ("quality", 0, 51), ("bitrate_mbps", 1, 500)):
        value = cfg.get(key, {"fps": 30, "quality": 23, "bitrate_mbps": 8}[key])
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Recording {key} must be an integer from {low} to {high}")
    if cfg.get("codec", "libx264") not in CODECS:
        raise ValueError("Choose H.264, HEVC or AV1 recording encoding")
    if cfg.get("rate_control", "quality") not in ("quality", "bitrate"):
        raise ValueError("Recording rate control must be quality or bitrate")
    if cfg.get("quality_preset", "very_high") not in QUALITY_PRESETS:
        raise ValueError("Choose a supported recording quality preset")
    if cfg.get("resolution", "native") not in RESOLUTIONS:
        raise ValueError("Choose native, 2160, 1440, 1080 or 720 recording resolution")


def expected_codec(cfg, color):
    codec = cfg.get("codec")
    if codec not in GPU_CODECS:
        return None  # preserve legacy verification behavior
    return "hevc" if color == "hdr" and codec == "h264" else codec


def split_codec(codec):
    """("av1", True) for "av1_10bit": the FFprobe codec name and whether 10-bit."""
    return codec.removesuffix("_10bit"), codec.endswith("_10bit")


def gpu_video_args(color, cfg=None):
    cfg = cfg or {}
    validate_encoding(cfg)
    codec = cfg.get("codec", "libx264")
    if codec not in GPU_CODECS:
        codec = "h264"
    if color == "hdr":
        codec = "av1_hdr" if codec.startswith("av1") else "hevc_hdr"
    args = ["-k", codec, "-cr", "limited", "-fallback-cpu-encoding", "no"]
    if RESOLUTIONS[cfg.get("resolution", "native")]:
        args += ["-s", RESOLUTIONS[cfg["resolution"]]]
    if cfg.get("rate_control", "quality") == "bitrate":
        args += ["-bm", "cbr", "-q", str(cfg.get("bitrate_mbps", 8) * 1000)]
    else:
        args += ["-bm", "qp", "-q", cfg.get("quality_preset", "very_high")]
    return args


def storage_estimate(cfg, seconds=60):
    """Decimal video MB; audio/container overhead and rate-control variation excluded."""
    return cfg.get("bitrate_mbps", 8) * seconds / 8
