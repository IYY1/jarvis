"""Intent executor, independent of recognition and UI."""
from .commands import CommandError
from .host import is_windows
if is_windows():
    from .windows.desktop import volume
else:
    from .hyprland import volume


class Dispatcher:
    def __init__(self, apps, desktop, recorder, pause, open_ui, replay=None, config=None):
        self.apps, self.desktop, self.recorder, self.replay = apps, desktop, recorder, replay
        self.pause, self.open_ui = pause, open_ui
        self.config = config

    def execute(self, intent):
        action = intent.action
        if action == "close_app":
            app = self.apps.resolve(intent.target)
            running = self.desktop.running(app)
            if not running:
                raise CommandError(f"{app.name} is not running.")
            for address in dict.fromkeys(window["address"] for window in running):
                self.desktop.dispatch("close_address", address)
            return f"Close requested for {app.name}"
        if action in ("open_app", "focus_app"):
            app = self.apps.resolve(intent.target)
            try:
                running = self.desktop.running(app)
            except CommandError:
                if action == "focus_app":
                    raise
                running = []
            if running:
                self.desktop.dispatch("focus_address", running[0]["address"])
                return f"Focused {app.name}"
            if action == "focus_app":
                raise CommandError(f"{app.name} is not running. Say open {app.name}.")
            self.apps.launch(app)
            others = self.apps.duplicates(app) if hasattr(self.apps, "duplicates") else []
            if others:
                return (f"Opened {app.name} ({app.id}). Also installed as "
                        + ", ".join(o.id for o in others) + "; add an alias to choose another.")
            return f"Opened {app.name}"
        if action in ("workspace", "move_workspace", "fullscreen", "unfullscreen"):
            self.desktop.dispatch(action, intent.target)
            return "Desktop updated"
        if action.startswith("volume_"):
            volume(action)
            return "Volume updated"
        if action in ("record_start", "record_stop"):
            result = self.recorder.start() if action == "record_start" else self.recorder.stop()
            return result["message"]
        if action == "replay_save" and intent.target is not None:
            return self.replay.save(intent.target)["message"]   # only the last N seconds
        if action in ("replay_start", "replay_stop", "replay_save"):
            return getattr(self.replay, action.removeprefix("replay_"))()["message"]
        if action == "screenshot":
            from .windows.screenshot import take
            recording = (self.config or {}).get("recording", {})
            path = take(self.config, intent.target if isinstance(intent.target, int) else None)
            if recording.get("screenshot_sound", True):
                from .feedback import play
                play("clip")
            return f"Saved screenshot: {path.parent.name} / {path.name}"
        if action == "open_recorder":
            self.open_ui("recording")
            return "Opened recording settings"
        if action == "pause":
            self.pause()
            return "Microphone muted. Resume in Jarvis or from your shortcut."
        if action == "cancel":
            return "Cancelled"
        raise CommandError("Unsupported action")
