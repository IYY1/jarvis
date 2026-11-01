"""Small command entry point; the normal user experience is the GTK window."""
import argparse
import json
import sys
import threading


def waybar_line(event: dict | None) -> str:
    """One Waybar JSON line; ``None`` means the service is not reachable."""
    if event is None:
        return json.dumps({"text": "◌ off", "tooltip": "Jarvis service is not running",
                           "class": "offline"})
    s = event.get("status", {})
    recording = s.get("recording", {}).get("state") == "recording"
    label = "● REC" if recording else "◉ " + s.get("state", "offline")
    tooltip = s.get("message", "")
    if s.get("replay", {}).get("state") == "on":
        label, tooltip = label + " ⟲", tooltip + "\nInstant replay on"
    return json.dumps({"text": label, "tooltip": tooltip,
                       "class": "recording" if recording else s.get("state", "offline")})


def find_app(apps: list[dict], query: str) -> dict:
    """Pick one installed app by desktop ID, exact name, or a unique partial name."""
    from .commands import normalize
    wanted = normalize(query)
    for test in (lambda a: a["id"] == query or a["id"] == query + ".desktop",
                 lambda a: normalize(a["name"]) == wanted,
                 lambda a: wanted in normalize(a["name"]) or wanted in a["id"].casefold()):
        found = [a for a in apps if test(a)]
        unique = {a["id"]: a for a in found}
        if len(unique) == 1:
            return found[0]
        if len(unique) > 1:
            raise ValueError(f"“{query}” matches several apps: " + ", ".join(sorted(unique)) + ". Use the desktop ID.")
    raise ValueError(f"No installed app matches “{query}”. Check the name in your app launcher.")


def run_alias(args, request) -> int:
    """jarvis alias                 -> list voice names
       jarvis alias NAME APP        -> add a voice name
       jarvis alias NAME --remove   -> remove it"""
    current = request("config")
    if not current.get("ok"):
        raise RuntimeError(current.get("error", "Jarvis service is not running"))
    config = current["config"]
    aliases = config.setdefault("aliases", {})
    if not args.name:
        for name, target in sorted(aliases.items()):
            print(f"{name}  ->  {target}")
        if not aliases:
            print("No voice names yet. Example: jarvis alias streamio stremio")
        return 0
    name = " ".join(args.name.lower().split())
    if args.remove:
        if aliases.pop(name, None) is None:
            raise ValueError(f"No voice name “{name}”")
        config.setdefault("app_prefixes", {}).pop(name, None)
        message = f"Removed “{name}”."
    else:
        if not args.app:
            raise ValueError("Say which app, e.g.: jarvis alias streamio stremio")
        apps = request("apps").get("apps", [])
        app = find_app(apps, args.app)
        aliases[name] = app["id"]
        wake = config.get("activation", {}).get("wake_phrase", "jarvis")
        message = f"Saved: say “{wake}, open {name}” to open {app['name']} ({app['id']})."
    result = request("save_config", config=config)
    if not result.get("ok"):
        raise ValueError(result.get("error", "Could not save"))
    print(message)
    return 0


def run_waybar(watch, stop: threading.Event, wait=None) -> int:
    """Stream status to Waybar and stay alive across service restarts.

    While the service is offline, show an 'off' state and retry with backoff
    (2 s up to 30 s) instead of exiting and being respawned every few seconds.
    """
    wait = wait or stop.wait
    delay = 2.0
    offline_shown = False
    while not stop.is_set():
        try:
            def output(event):
                nonlocal delay, offline_shown
                delay, offline_shown = 2.0, False
                print(waybar_line(event), flush=True)
            watch(output, stop)
        except (OSError, ValueError):
            pass
        if not offline_shown:
            print(waybar_line(None), flush=True)
            offline_shown = True
        wait(delay)
        delay = min(30.0, delay * 2)
    return 0


def main():
    if sys.argv[1:] == ["--background"]:
        from .windows.startup import start_service
        start_service()
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "--internal":
        from .entry import internal
        return internal(sys.argv[2], sys.argv[3:])
    parser = argparse.ArgumentParser(prog="jarvis", description="Offline desktop voice controls")
    from . import __version__
    parser.add_argument("--version", action="version", version="Jarvis " + __version__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("daemon", help="Run the background service")
    ui = sub.add_parser("ui", help="Open native settings")
    ui.add_argument("--page", default="dashboard")
    sub.add_parser("status")
    sub.add_parser("config")
    sub.add_parser("listen", help="Listen for one bounded command")
    sub.add_parser("cancel")
    sub.add_parser("mute")
    sub.add_parser("resume")
    from .host import is_windows
    if is_windows():
        sub.add_parser("shutdown", help="Stop Jarvis and its owned workers")
        startup = sub.add_parser("startup", help="Start Jarvis when you sign in to Windows")
        startup.add_argument("state", choices=["on", "off"])
    execute = sub.add_parser("execute", help="Execute an allowlisted phrase for integration testing")
    execute.add_argument("phrase")
    record = sub.add_parser("record")
    record.add_argument("action", choices=["start", "stop"])
    replay = sub.add_parser("replay", help="Instant replay: start, stop, or save the last seconds")
    replay.add_argument("action", choices=["start", "stop", "save"])
    sub.add_parser("doctor", help="Read-only dependency and session checks")
    alias = sub.add_parser("alias", help="Give an installed app a voice name, e.g.: jarvis alias streamio stremio")
    alias.add_argument("name", nargs="?", help="What you will say (\"open <name>\")")
    alias.add_argument("app", nargs="?", help="The app: its name as in your launcher, or its desktop ID")
    alias.add_argument("--remove", action="store_true", help="Remove the voice name instead")
    install = sub.add_parser("install-model", help="Download and verify the voice models (one time, about 790 MB)")
    install.add_argument("--archive", help="Use an already downloaded vosk-model-small-en-us-0.15.zip")
    install.add_argument("--basic", action="store_true", help="Only the small Vosk model (40 MB)")
    sub.add_parser("waybar", help="Event-driven JSON lines for a Waybar custom module")
    args = parser.parse_args()
    try:
        if args.command in (None, "ui"):
            from .host import is_windows
            if is_windows():
                from .windows.ui import main as ui_main
            else:
                from .ui import main as ui_main
            return ui_main(["--page", getattr(args, "page", "dashboard")])
        if args.command == "daemon":
            from .native_controller import selected_binary
            native = selected_binary()
            if native is not None:
                import os
                os.execv(str(native), [str(native)])
            from .daemon import main as daemon_main
            return daemon_main()
        if args.command == "install-model":
            from pathlib import Path
            from .models import (ACCURATE_MODELS, DEFAULT_ACCURATE_MODEL, MODEL_URL, install,
                                 install_accurate, install_denoiser)
            if not args.archive:
                print(f"Vosk model: {MODEL_URL} (about 40 MB)…", flush=True)
            print(f"  ready at {install(Path(args.archive).expanduser() if args.archive else None)}", flush=True)
            if not args.basic:
                # Both command models, so Settings can switch between them offline.
                for name in (DEFAULT_ACCURATE_MODEL, "whisper-tiny"):
                    spec = ACCURATE_MODELS[name]
                    print(f"Command model: {spec['archive']} (about {spec['megabytes']} MB, checked by SHA-256)…", flush=True)
                    shown = [-1]
                    def progress(fraction):
                        percent = int(fraction * 100)
                        if percent // 10 != shown[0]:
                            shown[0] = percent // 10
                            print(f"  {percent}%", flush=True)
                    print(f"  ready at {install_accurate(name, progress=progress)}", flush=True)
                print(f"Noise suppression model: ready at {install_denoiser()}", flush=True)
            print("Done. Restart Jarvis to use the new models.")
            return 0
        if args.command == "doctor":
            from .doctor import report
            result = report()
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "shutdown":
            from .windows.startup import stop_service
            print(json.dumps(stop_service(), indent=2))
            return 0
        if args.command == "startup":
            from .windows.startup import configure_startup
            print(json.dumps(configure_startup(args.state == "on"), indent=2))
            return 0
        from .client import request, watch
        if args.command == "alias":
            return run_alias(args, request)
        if args.command == "waybar":
            try:
                return run_waybar(watch, threading.Event())
            except (KeyboardInterrupt, BrokenPipeError):
                return 0
        op = {"resume": "mute", "mute": "mute", "record": "record_" + getattr(args, "action", ""),
              "replay": "replay_" + getattr(args, "action", "")}.get(args.command, args.command)
        kwargs = {"muted": args.command == "mute"} if args.command in ("resume", "mute") else {}
        if args.command == "execute":
            kwargs["phrase"] = args.phrase
        result = request(op, **kwargs)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"Jarvis: {exc}", file=sys.stderr)
        return 1
