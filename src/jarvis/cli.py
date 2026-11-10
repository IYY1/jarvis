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


def run_language(args, request) -> int:
    """jarvis language            -> show it
       jarvis language arabic     -> Arabic and English
       jarvis language english    -> English only"""
    current = request("config")
    if not current.get("ok"):
        raise RuntimeError(current.get("error", "Jarvis service is not running"))
    config = current["config"]
    names = {"ar_en": "Arabic and English", "en": "English only"}
    if not args.language:
        print(names.get(config["recognition"]["language"], config["recognition"]["language"]))
        return 0
    wanted = "ar_en" if args.language == "arabic" else "en"
    if wanted == "ar_en":
        from .models import arabic_model_ready
        if not arabic_model_ready():
            raise ValueError("The Arabic model is not installed. Run: jarvis install-model --arabic")
    config["recognition"]["language"] = wanted
    result = request("save_config", config=config)
    if not result.get("ok"):
        raise ValueError(result.get("error", "Could not save"))
    print(f"Language: {names[wanted]}." + (" Dictation types Arabic unless Voice → Dictation says English."
                                           if wanted == "ar_en" else ""))
    return 0


def run_settings(args) -> int:
    """jarvis settings export [FILE] | import FILE | backups"""
    import time
    from pathlib import Path
    from . import config, settings_file
    if args.action == "export":
        target = args.file or str(Path.home() / f"jarvis-settings-{time.strftime('%Y-%m-%d')}.json")
        print(f"Saved your settings to {settings_file.export_settings(target)}")
    elif args.action == "import":
        if not args.file:
            raise ValueError("Say which file, e.g.: jarvis settings import ~/jarvis-settings.json")
        print(settings_file.import_settings(args.file))
    else:
        copies = sorted(config.backups_dir().glob("config-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for copy in copies:
            print(copy)
        if not copies:
            print("No backups yet. Jarvis keeps one before an update changes settings and before an import.")
        else:
            print("Restore one with: jarvis settings import FILE")
    return 0


def run_add_app(args, request) -> int:
    """jarvis add-app                     -> list programs added in Jarvis
       jarvis add-app PATH [--name NAME]  -> add one (a portable program or AppImage)
       jarvis add-app NAME --remove       -> remove one Jarvis added"""
    from pathlib import Path
    from . import app_catalog
    from .host import is_windows
    if not args.target:
        for app in app_catalog.added():
            print(f"{app['name']}  ->  {app['entry']}")
        if not app_catalog.added():
            print("No programs added yet. Example: jarvis add-app ~/Applications/Tool.AppImage --name tool")
        return 0
    if args.remove:
        wanted = args.target.casefold()
        match = next((a for a in app_catalog.added() if a["name"].casefold() == wanted or a["id"] == args.target), None)
        if match is None:
            raise ValueError(f"No added program named “{args.target}”")
        app_catalog.remove(match["id"])
        message = f"Removed {match['name']}."
    else:
        name = args.name or Path(args.target).stem
        if is_windows():
            from .windows.system import com_context
            with com_context():
                app_catalog.add(args.target, name)
        else:
            app_catalog.add(args.target, name)
        message = f"Added {name}. Give it a voice name with: jarvis alias NAME \"{name}\""
    try:
        request("apps")  # the service rescans so the change is usable at once
    except (OSError, ConnectionError):
        message += " Jarvis is not running; the change applies when it starts."
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
    import os
    if not os.environ.get("JARVIS_CONFIG") and not os.environ.get("JARVIS_NO_MIGRATION"):
        from .migrate import from_old_name
        for moved in from_old_name():   # once, after the rename from Murmur
            print(f"Jarvis: moved {moved}", file=sys.stderr)
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
    dictate = sub.add_parser("dictate", help="Type speech into the focused text field (toggles by default)")
    dictate.add_argument("action", nargs="?", default="toggle", choices=["start", "stop", "cancel", "toggle"])
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
    sub.add_parser("screenshot", help="Save a screenshot of the recording display")
    sub.add_parser("doctor", help="Read-only dependency and session checks")
    alias = sub.add_parser("alias", help="Give an installed app a voice name, e.g.: jarvis alias streamio stremio")
    alias.add_argument("name", nargs="?", help="What you will say (\"open <name>\")")
    alias.add_argument("app", nargs="?", help="The app: its name as in your launcher, or its desktop ID")
    alias.add_argument("--remove", action="store_true", help="Remove the voice name instead")
    settings = sub.add_parser("settings", help="Export your settings to a file, import them, or list backups")
    settings.add_argument("action", choices=["export", "import", "backups"])
    settings.add_argument("file", nargs="?", help="The settings file (export: default ~/jarvis-settings-DATE.json)")
    language = sub.add_parser("language", help="Switch Arabic on or off: jarvis language arabic | english")
    language.add_argument("language", nargs="?", choices=["arabic", "english"])
    add_app = sub.add_parser("add-app", help="Add a program your launcher doesn't list, e.g.: jarvis add-app ~/Tool.AppImage")
    add_app.add_argument("target", nargs="?", help="The program file, or with --remove the added program's name")
    add_app.add_argument("--name", help="Name to show and say (default: the file name)")
    add_app.add_argument("--remove", action="store_true", help="Remove a program Jarvis added")
    install = sub.add_parser("install-model", help="Download and verify the voice models (one time, about 790 MB)")
    install.add_argument("--archive", help="Use an already downloaded vosk-model-small-en-us-0.15.zip")
    install.add_argument("--basic", action="store_true", help="Only the small Vosk model (40 MB)")
    install.add_argument("--arabic", action="store_true",
                         help="Install the optional Arabic model (with --archive moonshine-streaming-tiny-ar.tar.bz2)")
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
        if args.command == "install-model" and args.arabic:
            from pathlib import Path
            from .models import install_arabic
            print(f"Arabic model: ready at {install_arabic(Path(args.archive).expanduser() if args.archive else None)}")
            print("Choose Arabic and English in Voice settings, then restart Jarvis.")
            return 0
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
        if args.command == "settings":
            return run_settings(args)
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
        if args.command == "add-app":
            return run_add_app(args, request)
        if args.command == "language":
            return run_language(args, request)
        if args.command == "waybar":
            try:
                return run_waybar(watch, threading.Event())
            except (KeyboardInterrupt, BrokenPipeError):
                return 0
        op = {"resume": "mute", "mute": "mute", "record": "record_" + getattr(args, "action", ""),
              "replay": "replay_" + getattr(args, "action", "")}.get(args.command, args.command)
        kwargs = {"muted": args.command == "mute"} if args.command in ("resume", "mute") else {}
        if args.command == "dictate":
            op, kwargs = "dictation", {"action": args.action}
        if args.command == "execute":
            kwargs["phrase"] = args.phrase
        result = request(op, **kwargs)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"Jarvis: {exc}", file=sys.stderr)
        return 1
