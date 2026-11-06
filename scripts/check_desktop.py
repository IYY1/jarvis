#!/usr/bin/env python3
"""Opt-in real desktop integration checks, using an isolated test directory.

Launches a harmless .desktop fixture that writes a marker. Optional short,
silent monitor capture is deleted when verification finishes. It does not
change Hyprland configuration, existing windows, workspace or volume.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import resource

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.apps import AppRegistry
from jarvis.hyprland import Hyprland
from jarvis.recorder import Recorder, RecorderError


def recording_check(a, base, desktop):
    before = desktop.query("activewindow").get("address")
    recorder = Recorder({"configured": True, "monitor": a.monitor,
                         "output_dir": str(base / "recordings"), "system_audio": False,
                         "microphone": False, "fps": 30, "codec": a.codec, "quality": 23,
                         "vaapi_device": a.vaapi_device})
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu0 = r0.ru_utime + r0.ru_stime
    t0 = time.monotonic()
    checked = {"codec": a.codec, "requested_seconds": a.seconds, "audio": False,
               "finalized_and_ffprobe_validated": False, "test_video_deleted": False}
    try:
        recorder.start()
        time.sleep(a.seconds)
        stopped = recorder.stop()
        if stopped["state"] != "idle" or not stopped.get("file"):
            raise RecorderError(stopped.get("message", "Recorder did not save a file"))
        video = Path(stopped["file"])
        checked.update(state=stopped["state"], bytes=video.stat().st_size,
                       finalized_and_ffprobe_validated=video.suffix == ".mkv" and ".part" not in video.name)
    except (RecorderError, OSError) as exc:
        checked.update(state="error", error=str(exc), recorder_status=recorder.status())
    finally:
        try:
            recorder.close()
        except RecorderError as exc:
            checked.update(state="error", cleanup_error=str(exc))
        elapsed = time.monotonic() - t0
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = usage.ru_utime + usage.ru_stime - cpu0
        checked.update(lifecycle_wall_seconds=elapsed,
                       cpu_percent_one_logical_core=cpu / elapsed * 100,
                       peak_child_rss_mib=usage.ru_maxrss / 1024,
                       resource_scope="Test subprocesses (wf-recorder, hyprctl, ffprobe), lifecycle including startup/finalization; excludes compositor, game and shared audio service. Static/changing desktop, no controlled game.",
                       limitation="Structural video validation; no visual content, gaming FPS or permission persistence claim.")
        try:
            checked["focus_unchanged"] = before == desktop.query("activewindow").get("address")
        except Exception as exc:
            checked["focus_check_error"] = str(exc)
    return checked


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--record", action="store_true", help="Actually capture the selected monitor, without audio, then delete the test video")
    p.add_argument("--monitor", help="Explicit monitor name, required with --record")
    p.add_argument("--seconds", type=float, default=3)
    p.add_argument("--codec", choices=["libx264", "h264_vaapi"], default="libx264")
    p.add_argument("--vaapi-device", default="/dev/dri/renderD128")
    p.add_argument("--output", type=Path, help="Write JSON checks, never video, to this file")
    a = p.parse_args()
    if a.record and not a.monitor:
        p.error("Specify --monitor explicitly")
    if not 1 <= a.seconds <= 10:
        p.error("Use 1–10 seconds")
    result = {}
    try:
        desktop = Hyprland()
        monitors = desktop.query("monitors")
        result.update(monitors_discovered=len(monitors), apps_discovered=len(AppRegistry().apps))
        with tempfile.TemporaryDirectory(prefix="jarvis-desktop-check-") as temp:
            base = Path(temp)
            marker = base / "launched"
            launcher = base / "launch.py"
            launcher.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('ok')\n")
            (base / "jarvis-integration.desktop").write_text(
                '[Desktop Entry]\nType=Application\nName=Jarvis Integration Check\n'
                f'Exec="/usr/bin/python3" "{launcher}"\nTerminal=false\n')
            registry = AppRegistry(roots=[base])
            registry.launch(registry.resolve("jarvis integration check"))
            until = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < until:
                time.sleep(.05)
            if not marker.exists():
                raise RuntimeError("Desktop entry launched but did not produce its marker")
            result["gio_desktop_launch"] = "passed"
            if a.record:
                result["recording"] = recording_check(a, base, desktop)
        if "recording" in result:
            result["recording"]["test_video_deleted"] = True
    except Exception as exc:
        result["error"] = str(exc)
    print(json.dumps(result, indent=2))
    if a.output:
        a.output.write_text(json.dumps(result, indent=2) + "\n")
    if "error" in result or result.get("recording", {}).get("state") == "error":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
