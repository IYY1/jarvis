"""Small, explicit FFmpeg command construction; no capture occurs in this module."""

from functools import lru_cache
from pathlib import Path
import subprocess
from ..encoding import AUTO_CODECS, validate_encoding
from ..host import ENCODER_NAME, child_options, grouped, tool
from ..recorder import RecorderError, capture_color

QUALITY = {"medium": 28, "high": 23, "very_high": 19, "ultra": 15}
VENDORS = {0x10DE: "nvenc", 0x1002: "amf", 0x8086: "qsv"}


def format_for(cfg, color):
    name = cfg.get("codec", "libx264").removesuffix("_10bit")
    codec = name if name in ("h264", "hevc", "av1") else "h264"
    return "hevc" if color == "hdr" and codec == "h264" else codec


# Process loopback (one program's sound, or all but it) needs this build.
PROCESS_LOOPBACK_BUILD = 20348


MIXED_TITLE = "All sound"


GAME = "@game"   # the voice worker follows whichever game runs (app_audio.rs)
GAME_TITLE = "Game audio"


def audio_plan(cfg, replay=None, build=0, labels=None, recording=False):
    """Audio inputs for one capture: [(title or None, worker config)], like
    Medal's "Audio processes".

    sources "all": the desktop's sound (game_audio) and the microphone; with one
    mixed track they share a single input, otherwise each has its own.
    sources "specific": only what is chosen, each on its own input: the game
    running now (game_track, followed as games start and close), the programs
    in separate_apps (a program not running is silent until it starts) and the
    microphone. Nothing else is recorded, so nothing is heard twice.
    volumes: percent per source ("desktop", "game", "microphone", a program).
    A recording uses this layout with its own sound switches: system_audio for
    the desktop, game and programs. ``labels`` names programs."""
    if replay and recording:
        replay = dict(replay, game_audio=bool(cfg.get("system_audio")), microphone=bool(cfg.get("microphone")))
    if not replay or (recording and not (cfg.get("system_audio") or cfg.get("microphone"))):
        return [(None, cfg)] if cfg.get("system_audio") or cfg.get("microphone") else []
    volumes = replay.get("volumes") or {}

    def volume(key):
        return volumes.get(key, 100) / 100

    specific = replay.get("sources", "all") == "specific"
    if specific and 0 < build < PROCESS_LOOPBACK_BUILD:
        # Windows 10 can't record one program's sound: record all of it instead
        # (Specific apps is the default, and must not stop a capture there).
        specific = False
    microphone = replay.get("mic_device", "")   # recordings' own microphone; "" is Windows' default
    outputs = list(replay.get("outputs") or [])   # sound outputs to record; [] is the default output
    if not specific and replay.get("audio_tracks", "mixed") != "separate":
        if not (cfg.get("system_audio") or cfg.get("microphone")):
            return []
        return [(None, dict(cfg, mic_device=microphone, outputs=outputs,
                            volumes={"desktop": volume("desktop"), "microphone": volume("microphone")}))]
    tracks = []
    if not specific:
        if replay.get("game_audio", True):
            if outputs:   # each chosen output on its own track, named as Windows names it
                tracks += [(output.removeprefix("loopback:").removesuffix(" [Loopback]"),
                            {"kind": "output", "device": output, "volume": volume("desktop")}) for output in outputs]
            else:
                tracks.append(("Game and desktop sound", {"kind": "game", "app": "", "volume": volume("desktop")}))
    else:
        from ..replay import valid_app_name

        if build < PROCESS_LOOPBACK_BUILD:
            raise RecorderError("Recording specific apps needs Windows 11 (build 20348 or later). "
                                "Choose All audio in Replay settings.")
        if not recording or cfg.get("system_audio"):
            labels = {k.casefold(): v for k, v in (labels or {}).items()}
            if replay.get("game_track", True):
                tracks.append((GAME_TITLE, {"kind": "app", "app": GAME, "volume": volume("game")}))
            for app in (a for a in replay.get("separate_apps", []) if valid_app_name(a)):
                tracks.append((labels.get(app.casefold(), app), {"kind": "app", "app": app, "volume": volume(app)}))
    if replay.get("microphone", True):
        tracks.append(("Microphone", {"kind": "microphone", "device": microphone, "volume": volume("microphone")}))
    return [(title, dict(cfg, track=track)) for title, track in tracks]


def mixed_inputs(plan):
    """The plan's tracks a first, mixed track adds up: all of them, from two on
    (none overlap). A clip then plays everything chosen in any player, and the
    separate tracks after it stay for editing."""
    return list(range(len(plan))) if len(plan) > 1 else []


def audio_maps(audio_pipes, mix=(), volumes=None, separate=True):
    """FFmpeg arguments for the sound tracks: a first "All sound" track adding up
    ``mix`` (summed like the system mixer, with a limiter so loud moments together
    don't clip), then, when ``separate``, each input on its own named track.
    Each input plays at its ``volumes`` share (1.0 = as it is)."""
    volumes = list(volumes or [1.0] * len(audio_pipes))
    mixed = len(mix) > 1
    graph, mixed_in, outputs = [], [], []
    for i, (_, title) in enumerate(audio_pipes):
        source, level = f"[{i + 1}:a]", volumes[i]
        to_mix, alone = mixed and i in mix, separate or not mixed
        gain = f"volume={level:.2f}" if level != 1.0 else ""
        if to_mix and alone:
            graph.append(f"{source}{gain + ',' if gain else ''}asplit=2[m{i}][s{i}]")
            mixed_in.append(f"[m{i}]")
            outputs.append((f"[s{i}]", title))
        elif to_mix:
            if gain:
                graph.append(f"{source}{gain}[m{i}]")
            mixed_in.append(f"[m{i}]" if gain else source)
        elif alone:
            if gain:
                graph.append(f"{source}{gain}[s{i}]")
            outputs.append((f"[s{i}]" if gain else f"{i + 1}:a:0", title))
    args = []
    if mixed:
        graph.append(f"{''.join(mixed_in)}amix=inputs={len(mixed_in)}:normalize=0:dropout_transition=0,"
                     "alimiter=limit=0.97:level=0[mix]")
        outputs.insert(0, ("[mix]", MIXED_TITLE))
    if graph:
        args += ["-filter_complex", ";".join(graph)]
    for index, (label, title) in enumerate(outputs):
        args += ["-map", label]
        if title:
            args += [f"-metadata:s:a:{index}", f"title={title}"]
    return args


def plan_volumes(plan):
    """Each input's volume for FFmpeg; a single mixed input applies its own."""
    return [c.get("track", {}).get("volume", 1.0) for _, c in plan]


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
        # One reference frame: about 40 MB less video memory at 2K, and on game
        # footage the picture measured the same (SSIM 0.9884 vs 0.9886). H.264
        # refuses it while B-frames are on, so the encoder test turns them off too,
        # as every capture does.
        args += ["-preset", "p4", "-tune", "hq", "-rc", "cbr" if budget else "constqp", "-bf", "0", "-dpb_size", "1"]
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
    # Capture runs as long as recording does: inside Jarvis's row, under a clear name.
    binary = grouped(tool("ffmpeg"), ENCODER_NAME)
    if not binary or not tool("ffprobe"):
        raise RecorderError(
            "FFmpeg/ffprobe is missing; reinstall the complete Windows package"
        )
    ten_bit = cfg.get("codec", "").endswith("_10bit") or windows_color(cfg, display) == "hdr"
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


def resolve_codec(cfg, display):
    """``cfg`` with "auto" replaced by the best format this display's GPU encodes:
    AV1, then HEVC, then H.264, each tested with a one-frame encode (cached). HDR
    video needs 10-bit HEVC or AV1. With no GPU encoder at all, SDR falls back to
    CPU H.264, as choosing it by hand would."""
    if cfg.get("codec") != "auto":
        return cfg
    for codec in AUTO_CODECS:
        candidate = dict(cfg, codec=codec)
        color = windows_color(candidate, display)
        name = format_for(candidate, color)
        if color == "hdr" and name == "h264":
            continue
        try:
            select_encoder(candidate, display, name)
            return candidate
        except RecorderError:
            continue
    if windows_color(dict(cfg, codec="h264"), display) == "hdr":
        raise RecorderError("No GPU encoder for HDR video here (it needs HEVC or AV1). Update the graphics driver, or record ordinary video.")
    return dict(cfg, codec="libx264", encoder="cpu")


def in_hdr_mode(display):
    return str(display.get("colorManagementPreset", "")).lower() == "hdr"


def windows_color(cfg, display):
    """A display in HDR mode can't be grabbed directly: Windows' 8-bit and 10-bit
    formats clip everything above 80 nits. The worker's screen mode reads the true
    picture instead, and makes ordinary video from it unless HDR video was asked for.
    Ordinary video looks right in every player, HDR screen or not."""
    if in_hdr_mode(display) and cfg.get("color_mode", "auto") in ("auto", "sdr"):
        return "sdr"
    return capture_color(cfg, display)


def screen_request(cfg, display, pipe, color):
    """What the worker's screen mode needs. The picture it sends is exactly what
    command() tells FFmpeg to expect."""
    width, height = output_size(cfg, display)
    return {
        "pipe": pipe,
        "adapter": display["adapter"],
        "output": display["output"],
        "width": width // 2 * 2,
        "height": height // 2 * 2,
        "fps": cfg.get("fps", 30),
        "hdr": color == "hdr",
        "ten_bit": color == "hdr" or cfg.get("codec", "").endswith("_10bit"),
    }


def capture_rate(fps):
    """What to ask the screen grabber for, so ``fps`` new frames really come out.

    ddagrab sleeps between frames, and Windows sleeps coarsely, so asking for exactly
    144 delivered about 80. Asking for more and letting FFmpeg's fps filter pick
    evenly gets close. Measured on 144 and 240 Hz displays: 480 was steady, while
    720 and above collapsed to about 85 new frames a second.
    """
    return fps * 2 if fps <= 60 else 480


def capped_fps(fps, display):
    """A display can't show more new frames than its refresh rate; the rest would
    only be repeats that use the encoder and fill the disk."""
    refresh = display.get("refresh") or 0
    return min(fps, refresh) if refresh >= 1 else fps


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


def command(cfg, display, binary, family, *, audio_pipes=(), screen_pipe=None, mix=(), volumes=None, separate=True):
    """``audio_pipes``: [(pipe, title or None)], one audio track each.
    ``screen_pipe``: where the worker's screen mode sends a display in HDR mode.
    ``mix``: indexes into ``audio_pipes`` added up into a first track before them."""
    validate_encoding(cfg)
    color = windows_color(cfg, display)
    if in_hdr_mode(display) and not screen_pipe:
        raise RecorderError("A display in HDR mode needs the worker's screen capture")
    if color == "hdr" and not in_hdr_mode(display):
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
    args = [binary, "-hide_banner", "-loglevel", "warning", "-n"]
    if screen_pipe:
        # Planar video, already in its final size and color, one frame per pipe read.
        args += [
            "-f",
            "rawvideo",
            "-pixel_format",
            "p010le" if ten_bit else "nv12",
            "-video_size",
            f"{width // 2 * 2}x{height // 2 * 2}",
            "-framerate",
            str(fps),
            "-thread_queue_size",
            "8",
            "-i",
            screen_pipe,
        ]
    else:
        args += [
            "-init_hw_device",
            f"d3d11va=capture:{display['adapter']}",
            "-filter_hw_device",
            "capture",
            "-f",
            "lavfi",
            "-i",
            f"ddagrab=output_idx={display['output']}:framerate={capture_rate(fps)}:output_fmt=8bit:allow_fallback=0",
        ]
    for pipe, _ in audio_pipes:
        args += ["-thread_queue_size", "64", "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", pipe]
    args += ["-map", "0:v:0"]
    args += audio_maps(audio_pipes, mix, volumes, separate)
    # The fast AAC coder spends about a third less CPU per track than the default
    # two-loop search, with no difference to hear at 160 kb/s.
    args += ["-c:a", "aac", "-aac_coder", "fast", "-b:a", "160k"] if audio_pipes else ["-an"]
    # Native-size 8-bit NVENC/AMF keep D3D11 frames on the GPU. Scaling and
    # SDR-to-10-bit conversion explicitly download/process frames; encoding
    # remains on the requested GPU, with no silent software fallback.
    hardware_frames = not (screen_pipe or family in ("qsv", "mf", "cpu") or scaled or (ten_bit and color == "sdr"))
    if screen_pipe:
        args += ["-vf", color_labels(color)]
    elif hardware_frames:
        args += ["-vf", f"fps={fps},{color_labels(color)}"]
    else:
        filters = [f"fps={fps}", "hwdownload", "format=bgra"]
        resize = f"scale={width}:{height}" if scaled else "scale"
        resize += ":" if scaled else "="
        # Name the conversion, so the labels below are true of the pixels.
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
