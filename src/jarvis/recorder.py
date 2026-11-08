"""Supervised capture: GPU formats/HDR and compatible legacy wf-recorder paths."""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import uuid

from .encoding import GPU_CODECS, expected_codec, gpu_video_args, split_codec, validate_encoding, with_auto_bitrate


class RecorderError(RuntimeError):
    """An actionable configuration, capture, or finalization error."""


PARTIAL_GLOB = ".Jarvis_*.part.mkv"
_spawner: ThreadPoolExecutor | None = None
_spawner_lock = threading.Lock()


def _spawn(argv: list[str], **kwargs) -> subprocess.Popen:
    """Start the recorder from one long-lived thread.

    Linux delivers PR_SET_PDEATHSIG when the *thread* that forked the child
    exits, not only the process. Short-lived worker threads would therefore
    stop a healthy capture; a dedicated thread that lives as long as the
    service makes the signal mean "the service died".
    """
    global _spawner
    with _spawner_lock:
        if _spawner is None:
            _spawner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-recorder-spawn")
    return _spawner.submit(subprocess.Popen, argv, **kwargs).result()


def _parent_death_prefix() -> list[str]:
    """If the service dies, SIGINT the recorder so it finalizes and exits
    instead of capturing until the disk is full. util-linux setpriv sets the
    signal and then execs wf-recorder, avoiding preexec_fn in a threaded process."""
    setpriv = shutil.which("setpriv")
    return [setpriv, "--pdeathsig", "INT", "--"] if setpriv else []


def orphaned_recorders() -> list[int]:
    """PIDs of this user's capture processes writing a Jarvis staging file."""
    found = []
    uid = os.getuid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(Path(a.decode(errors="replace")).name in ("wf-recorder", "gpu-screen-recorder") for a in argv[:6]) and \
                any(Path(a.decode(errors="replace")).name.startswith(".Jarvis_") for a in argv):
            found.append(int(entry.name))
    return found


def _query(argv: list[str], timeout: float = 3) -> str:
    from .host import child_options, grouped, tool
    if argv[0] == "ffprobe":
        argv = [str(grouped(tool("ffprobe")) or argv[0]), *argv[1:]]   # in Jarvis's Task Manager row
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                check=False, stdin=subprocess.DEVNULL, **child_options())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecorderError(f"Could not query {argv[0]}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise RecorderError(f"{argv[0]} failed: {detail or result.returncode}")
    return result.stdout


def sources() -> list[dict]:
    """List connected Hyprland monitors without invoking a selection dialog."""
    try:
        monitors = json.loads(_query(["hyprctl", "-j", "monitors"]))
    except (ValueError, TypeError) as exc:
        raise RecorderError("Hyprland returned an invalid monitor list.") from exc
    if not isinstance(monitors, list):
        raise RecorderError("Hyprland returned an invalid monitor list.")
    return [{"name": m["name"], "description": m.get("description", ""),
             "width": m.get("width"), "height": m.get("height"),
             "colorManagementPreset": m.get("colorManagementPreset"),
             "currentFormat": m.get("currentFormat"),
             "focused": bool(m.get("focused"))}
            for m in monitors if isinstance(m, dict) and isinstance(m.get("name"), str)
            and not m.get("disabled", False)]


def audio_sources() -> list[dict]:
    """Pulse/PipeWire-Pulse source names, including system audio monitors."""
    try:
        devices = json.loads(_query(["pactl", "--format=json", "list", "sources"]))
    except (ValueError, TypeError) as exc:
        raise RecorderError("The audio server returned an invalid source list.") from exc
    if not isinstance(devices, list):
        raise RecorderError("The audio server returned an invalid source list.")
    return [{"name": d["name"], "description": d.get("description", d["name"]),
             "monitor": d.get("monitor_of_sink") not in (None, "", 4294967295)}
            for d in devices if isinstance(d, dict) and isinstance(d.get("name"), str)]


def capture_color(cfg: dict, monitor: dict) -> str:
    """Choose once per capture. Ten-bit SDR/WCG alone does not imply HDR.

    Older Hyprland versions omit the preset; manual HDR covers that case and
    fullscreen passthrough. Switching display modes requires restarting capture.
    """
    mode = cfg.get("color_mode", "auto")
    if mode not in ("auto", "hdr", "sdr"):
        raise RecorderError("Capture color mode must be auto, hdr or sdr.")
    hdr = str(monitor.get("colorManagementPreset", "")).lower() in ("hdr", "hdredid")
    if mode == "sdr" and hdr:
        raise RecorderError("This display is in HDR mode. Choose Automatic or Preserve HDR, or disable display HDR before SDR capture. Direct monitor capture does not tone map HDR to SDR.")
    return "hdr" if mode == "hdr" or (mode == "auto" and hdr) else "sdr"



TEN_BIT = ("yuv420p10le", "p010le", "p010be", "yuv420p10be")


def verify_video(path: Path, *, color: str = "sdr", codec: str | None = None):
    if not path.exists() or path.stat().st_size == 0:
        raise RecorderError("The recorder produced no video data.")
    try:
        data = json.loads(_query(["ffprobe", "-v", "error", "-show_entries",
                                 "stream=codec_type,codec_name,width,height,pix_fmt,color_space,color_transfer,color_primaries,color_range",
                                 "-of", "json", str(path)], timeout=10))
        videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video"
                  and s.get("width", 0) > 0 and s.get("height", 0) > 0]
        if not videos:
            raise RecorderError("The output contains no valid video stream.")
        name, ten_bit = split_codec(codec) if codec else (None, False)
        if name and not any(s.get("codec_name") == name for s in videos):
            raise RecorderError(f"Video format verification failed: expected {name}. Check GPU and driver support for the selected format.")
        if ten_bit and not any(s.get("pix_fmt") in TEN_BIT for s in videos):
            raise RecorderError(f"Video format verification failed: expected 10-bit {name}. Check GPU and driver support for the selected format.")
        if color == "hdr" and not any(
                s.get("pix_fmt") in TEN_BIT
                and s.get("color_transfer") == "smpte2084"
                and s.get("color_primaries") == "bt2020"
                and s.get("color_space") == "bt2020nc"
                and s.get("color_range") == "tv" for s in videos):
            raise RecorderError("HDR verification failed: expected 10-bit PQ / BT.2020 limited-range video. The file is retained; check GPU Screen Recorder, driver and display HDR support.")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RecorderError("The recorded file could not be validated.") from exc


class Recorder:
    """One recorder child; idle instances have no worker thread or timer.

    ``config`` may be the application config or its ``recording`` section.
    ``on_change(status)`` executes on the caller or monitor thread; UI clients
    must marshal it to their main loop. Opening a settings window is deliberately
    outside this adapter. Configuration is read anew on each start.
    """

    def __init__(self, config: dict, on_change=None):
        self.config = config
        self.on_change = on_change
        self._lock = threading.RLock()
        self._process = None
        self._done = threading.Event()
        self._done.set()
        self._stop_requested = False
        self._forced = False
        self._color = "sdr"
        self._codec = None
        self._status = {"state": "idle", "file": None, "message": "Recorder ready."}

    def _settings(self) -> dict:
        return self.config.get("recording", self.config)

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def sources(self) -> list[dict]:
        return sources()

    def inspect_previous(self, orphans=None) -> None:
        """Report captures left behind by an earlier service that crashed or was killed."""
        orphans = orphaned_recorders() if orphans is None else orphans
        folder = Path(str(self._settings().get("output_dir", "~/Videos/Jarvis"))).expanduser()
        try:
            partials = sorted(folder.glob(PARTIAL_GLOB)) if folder.is_dir() else []
        except OSError:
            partials = []
        with self._lock:
            if self._process is not None:
                return
            if orphans:
                self._set("error", "A recorder from an earlier Jarvis session is still running "
                          f"(PID {', '.join(map(str, orphans))}). Stop it with: kill -INT {orphans[0]}. "
                          "Its partial file stays in the output folder.")
            elif partials:
                self._set("idle", f"Found {len(partials)} unfinished recording(s) from an earlier session in "
                          f"{folder} ({PARTIAL_GLOB}). See docs/recording.md for recovery.", partials[-1])
            else:
                return
        self._notify()

    def _notify(self):
        if self.on_change:
            try:
                self.on_change(self.status())
            except Exception:
                # A disconnected UI must not orphan a capture or lose a file.
                pass

    def _set(self, state, message, file=None):
        self._status = {"state": state, "message": message,
                        "file": str(file) if file else None}

    def _validate(self) -> tuple[dict, str, list[str]]:
        cfg = self._settings()
        if not cfg.get("configured", False):
            raise RecorderError("Open Recording settings and finish source, audio, folder, and permission setup first.")
        monitor = str(cfg.get("monitor", "")).strip()
        if not monitor:
            raise RecorderError("Choose a monitor in Recording settings first.")
        display = next((item for item in sources() if item["name"] == monitor), None)
        if display is None:
            raise RecorderError(f"Capture source {monitor!r} is unavailable. Choose a connected monitor.")
        color = capture_color(cfg, display)
        cfg = with_auto_bitrate(cfg, display)
        gpu = color == "hdr" or cfg.get("codec") in GPU_CODECS
        if not gpu and cfg.get("resolution", "native") != "native":
            raise RecorderError("The legacy CPU/VA-API recorder records the display's own resolution only. "
                                "Choose a GPU format or the display's resolution.")
        backend_name = "gpu-screen-recorder" if gpu else "wf-recorder"
        backend = shutil.which(backend_name)
        if not backend:
            raise RecorderError(f"{backend_name} is missing. Install it using your distribution's package manager.")
        if not shutil.which("ffprobe"):
            raise RecorderError("ffprobe is missing. Install FFmpeg to validate saved recordings.")
        try:
            validate_encoding(cfg)
        except ValueError as exc:
            raise RecorderError(str(exc)) from exc
        fps, quality = cfg.get("fps", 30), cfg.get("quality", 23)
        codec = cfg.get("codec", "libx264")
        if gpu:
            argv = [backend, "-w", monitor, "-f", str(fps), "-c", "mkv", "-v", "no"] + gpu_video_args(color, cfg)
        else:
            argv = [backend, "--output", monitor, "--muxer", "matroska", "--codec", codec,
                    "--framerate", str(fps)]
        if not gpu and codec == "libx264":
            argv += ["--pixel-format", "yuv420p", "--codec-param", "preset=ultrafast",
                     "--codec-param", "threads=2"]
        elif not gpu:
            device = str(cfg.get("vaapi_device") or "/dev/dri/renderD128")
            if not device.startswith("/dev/dri/renderD") or not os.access(device, os.R_OK | os.W_OK):
                raise RecorderError("The selected VA-API render device is unavailable or not accessible.")
            # Leave pixel format/filter selection to wf-recorder's VA-API path.
            # It supplies scale_vaapi=nv12 and hwupload when necessary. Forcing
            # software yuv420p here is incompatible with h264_vaapi's VAAPI frames.
            argv += ["--device", device]
        if not gpu:
            params = ([f"b={cfg.get('bitrate_mbps', 8) * 1000000}",
                       f"maxrate={cfg.get('bitrate_mbps', 8) * 1000000}",
                       f"bufsize={cfg.get('bitrate_mbps', 8) * 2000000}"]
                      if cfg.get("rate_control", "quality") == "bitrate"
                      else [f"{'crf' if codec == 'libx264' else 'qp'}={quality}"])
            for param in params:
                argv += ["--codec-param", param]
        system, mic = bool(cfg.get("system_audio", False)), bool(cfg.get("microphone", False))
        if system or mic:
            device = str(cfg.get("audio_device", "")).strip()
            if system and mic and not cfg.get("combined_audio_source", False):
                raise RecorderError("wf-recorder accepts one audio source. Configure a combined source explicitly, or select one audio input.")
            if not device:
                raise RecorderError("Choose an explicit recording audio source in Recording settings.")
            available = {item["name"]: item for item in audio_sources()}
            if device not in available:
                raise RecorderError("The chosen recording audio source is unavailable. Check Recording settings.")
            if not (system and mic):
                is_monitor = available[device]["monitor"]
                if system and not is_monitor:
                    raise RecorderError("System audio requires a sink monitor source; choose its .monitor source.")
                if mic and is_monitor:
                    raise RecorderError("Microphone recording requires an input source, not a sink monitor.")
            if gpu:
                argv += ["-a", device, "-ac", "aac"]
            else:
                argv += ["--audio-backend", "pulse", f"--audio={device}", "--audio-codec", "aac"]
        return {**cfg, "active_color": color, "gpu": gpu}, monitor, argv

    def start(self) -> dict:
        with self._lock:
            if self._process is not None:
                if self._status["state"] == "stopping":
                    raise RecorderError("The previous recording is still being finalized.")
                return self.status()  # duplicate voice commands cannot spawn a second capture
            try:
                orphans = orphaned_recorders()
                if orphans:
                    raise RecorderError("A recorder from an earlier Jarvis session is still running "
                                        f"(PID {orphans[0]}). Stop it with: kill -INT {orphans[0]}")
                cfg, _monitor, argv = self._validate()
                folder = Path(str(cfg.get("output_dir", "~/Videos/Jarvis"))).expanduser().resolve()
                if cfg.get("game_folders"):
                    from .games import focused_game
                    folder = folder / (focused_game() or "")   # the game focused when recording starts
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                tag = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "_" + uuid.uuid4().hex[:10]
                final = folder / f"Jarvis_{tag}.mkv"
                staging = folder / f".Jarvis_{tag}.part.mkv"
                fd = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                try:
                    # Always name the source and muxer. Never invoke a shell or interactive selector.
                    output_args = ["-o", str(staging)] if cfg["gpu"] else ["--overwrite", "--file", str(staging)]
                    process = _spawn(_parent_death_prefix() + argv + output_args,
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.PIPE, start_new_session=True)
                except OSError:
                    staging.unlink(missing_ok=True)
                    raise
            except (RecorderError, OSError) as exc:
                self._set("error", str(exc))
                self._notify()
                raise RecorderError(str(exc)) from exc
            self._process = process
            self._color = cfg["active_color"]
            self._codec = expected_codec(cfg, self._color)
            self._stop_requested = self._forced = False
            self._done.clear()
            self._set("recording", f"Recorder running · {self._color.upper()}. Stop to finalize and verify the file.", staging)
            threading.Thread(target=self._watch, args=(process, staging, final),
                             name="jarvis-recorder", daemon=True).start()
        self._notify()
        return self.status()

    def _watch(self, process, staging: Path, final: Path):
        # Block on the pipe/process, keeping at most 16 KiB of diagnostics in memory.
        tail = deque(maxlen=4)
        try:
            if process.stderr:
                while chunk := process.stderr.read(4096):
                    tail.append(chunk)
                process.stderr.close()
            code = process.wait()
            with self._lock:
                requested, forced = self._stop_requested, self._forced
            if not requested or forced or code != 0:
                detail = b"".join(tail).decode("utf-8", errors="replace").strip()[-1600:]
                raise RecorderError(f"Recorder exited with status {code}. {detail}".strip())
            verify_video(staging, color=self._color, codec=self._codec)
            with staging.open("rb") as handle:
                os.fsync(handle.fileno())
            # Same-directory hard link publishes a complete name atomically and never overwrites.
            os.link(staging, final)
            staging.unlink()
            directory_fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            with self._lock:
                self._set("idle", f"Saved recording: {final.name}", final)
        except (RecorderError, OSError) as exc:
            if staging.exists() and staging.stat().st_size == 0:
                staging.unlink(missing_ok=True)
            partial = staging if staging.exists() else (final if final.exists() else None)
            message = f"{exc}"
            if partial:
                message += f" Partial video retained at {partial}. See docs/recording.md for recovery."
            with self._lock:
                self._set("error", message, partial)
        finally:
            with self._lock:
                self._process = None
                self._done.set()
            self._notify()

    def stop(self, timeout: float = 12) -> dict:
        with self._lock:
            process = self._process
            if process is None:
                result = self.status()
                if result["state"] == "error":
                    raise RecorderError(result["message"])
                return result
            if not self._stop_requested:
                self._stop_requested = True
                self._set("stopping", "Finishing recording…", self._status.get("file"))
                try:
                    process.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
        self._notify()
        if not self._done.wait(timeout):
            with self._lock:
                self._forced = True
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            if not self._done.wait(2):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                self._done.wait(2)
        result = self.status()
        if result["state"] == "error":
            raise RecorderError(result["message"])
        if result["state"] == "stopping":
            raise RecorderError("The recorder has not exited. The partial file is retained; inspect the recorder process.")
        return result

    def close(self):
        """Gracefully stop an owned capture during daemon shutdown."""
        with self._lock:
            if self._process is None:
                return
        self.stop()
