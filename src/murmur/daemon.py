"""Event-driven service. GTK and speech libraries are never imported here."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading

from . import config as configuration
from .actions import Dispatcher
from .host import is_windows, child_options
if is_windows():
    from .windows.apps import AppRegistry
else:
    from .apps import AppRegistry
from .client import socket_path
from .commands import REFERENCE, CommandError, Parser, normalize
if is_windows():
    from .windows.desktop import Desktop as Hyprland
    from .windows.capture import Recorder, Replay, audio_sources
else:
    from .hyprland import Hyprland
    from .recorder import Recorder, audio_sources
    from .replay import Replay
from .state import Activation
from .desktop_process import desktop_argv
from . import dictation


class Service:
    def __init__(self, config=None):
        self.config = configuration.validate(config) if config is not None else configuration.load()
        self.apps = AppRegistry(self.config["aliases"])
        self.parser = Parser.from_config(self.config)
        a = self.config["activation"]
        self.activation = Activation(a["command_timeout"], a["confidence"])
        self.desktop = Hyprland()
        self.recorder = Recorder(self.config, self.recording_changed)
        self.replay = Replay(self.config, lambda status: self.loop and self.loop.call_soon_threadsafe(self.on_replay_changed, status))
        self.last_replay_file = None
        self.muted = not self.config["privacy"]["onboarded"]
        self.message = "Complete first-run microphone consent in Settings" if self.muted else "Starting voice control"
        self.worker = self.voice_task = None
        self.worker_mode = None
        self.unanswered = None   # listen/cancel lines the worker hasn't answered; None until it's ready
        self.watchers = set()
        self.loop = None
        self.level = 0
        self.unsupported = []
        self.last_recording_state = "idle"
        self.action_lock = asyncio.Lock()
        self.worker_lock = asyncio.Lock()
        self.stopping = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        from .native_worker import python_worker_available, rust_ready
        self.voice_available = python_worker_available() or rust_ready(self.config)
        self.desktop_executor = None
        if is_windows():
            from concurrent.futures import ThreadPoolExecutor
            import pythoncom
            self.desktop_executor = ThreadPoolExecutor(max_workers=1, initializer=pythoncom.CoInitialize, thread_name_prefix="jarvis-com")
        self.tray = None
        self.engine = None
        self.gate = None
        self.dictation = dictation.Session()
        self.dispatcher = Dispatcher(self.apps, self.desktop, self.recorder, self.pause_from_action, self.open_ui,
                                     replay=self.replay, config=self.config)

    def status(self):
        return {"state": self.activation.state, "message": self.message, "muted": self.muted,
                "recording": self.recorder.status(), "replay": self.replay.status(), "level": self.level,
                "voice_available": self.voice_available,
                "unsupported_phrases": self.unsupported, "engine": self.engine, "gate": self.gate,
                "dry_run": os.environ.get("MURMUR_DRY_RUN") == "1",
                "microphone_open": self.worker is not None,
                "mic_test": self.worker is not None and self.worker_mode == "test",
                "dictation": self.dictation.status()}

    async def desktop_call(self, func, *args):
        if self.desktop_executor is None:
            return await asyncio.to_thread(func, *args)
        try:
            return await asyncio.get_running_loop().run_in_executor(self.desktop_executor, func, *args)
        except (CommandError, ValueError, OSError, RuntimeError):
            raise
        except Exception as exc:
            raise CommandError(f"Windows could not complete this action: {exc}") from exc

    def publish(self):
        if self.tray:
            self.tray.update(self.status())
        message = {"ok": True, "status": self.status()}
        for queue in tuple(self.watchers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)

    def voice_grammar(self) -> dict:
        """Command grammar plus which desktop entry each spoken app name opens."""
        names = list(self.apps.index)
        names.extend(row["voice_name"] for row in self.apps.voice_commands(self.parser))
        grammar = self.parser.grammar(names)
        app_ids = {}
        for name in names:
            try:
                app_ids[name] = self.apps.resolve(name).id
            except CommandError:
                app_ids[name] = name
        grammar["app_ids"] = app_ids
        return grammar

    def background(self, coroutine, what: str) -> asyncio.Task:
        """Run a coroutine without awaiting it, but keep a reference (asyncio only
        holds weak references to tasks) and surface its failure in the status."""
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def finished(done: asyncio.Task) -> None:
            self.tasks.discard(done)
            if done.cancelled() or done.exception() is None:
                return
            exc = done.exception()
            print(f"murmur: {what} failed: {exc!r}", file=sys.stderr, flush=True)
            if what == "voice restart":
                self.activation.error()
            self.message = f"{what.capitalize()} failed: {exc}"
            self.publish()
        task.add_done_callback(finished)
        return task

    def recording_changed(self, _status):
        if self.loop:
            self.loop.call_soon_threadsafe(self.on_recording_changed, _status)

    def on_recording_changed(self, status):
        state = status["state"]
        if state != self.last_recording_state and state in ("recording", "idle", "error") and self.config["recording"].get("sound_feedback", False):
            from .feedback import play
            self.background(asyncio.to_thread(play, state), "recording sound")
        self.last_recording_state = state
        self.publish()

    def on_replay_changed(self, status):
        if status["file"] and status["file"] != self.last_replay_file and self.config["replay"]["sound"]:
            from .feedback import play
            self.background(asyncio.to_thread(play, "clip"), "replay sound")
        self.last_replay_file = status["file"]
        self.publish()

    def pause_from_action(self):
        self.loop.call_soon_threadsafe(lambda: self.background(self.set_mute(True), "mute"))

    def open_ui(self, page):
        if is_windows():
            from .host import command, child_options
            proc = subprocess.Popen(command("ui", "--page", page), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **child_options(independent=True))
            threading.Thread(target=proc.wait, daemon=True).start()
            return
        # Separate process and no UI import retained by the service.
        proc = subprocess.Popen(desktop_argv([sys.executable, "-m", "murmur", "ui", "--page", page]),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        threading.Thread(target=proc.wait, daemon=True, name="murmur-ui-reaper").start()

    async def stop_worker(self):
        async with self.worker_lock:
            await self._stop_worker_locked()

    async def _stop_worker_locked(self):
        proc = self.worker
        self.worker = self.unanswered = None
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        task = self.voice_task
        self.voice_task = None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.level = 0

    async def start_worker(self, mode):
        if self.muted or not self.config["privacy"]["onboarded"]:
            raise CommandError("Microphone is muted. Complete consent and unmute in Settings.")
        async with self.worker_lock:
            if self.muted or not self.config["privacy"]["onboarded"]:
                raise CommandError("Microphone is muted. Complete consent and unmute in Settings.")
            if mode == "wake_word" and self.config["activation"]["mode"] != "wake_word":
                return  # a queued resume must not outlive a switch to PTT
            await self._stop_worker_locked()
            self.message = "Opening microphone…"
            if mode == "once":
                self.activation.arm()
            payload = {"config": self.config, "mode": mode, "grammar": self.voice_grammar()}
            from .native_worker import prepare
            worker_argv, payload, worker_env = prepare(self.config, mode, payload["grammar"])
            encoded = (json.dumps(payload) + "\n").encode()
            if len(encoded) > 1048576:
                raise CommandError("Voice grammar exceeds the 1 MiB worker limit")
            self.worker = await asyncio.create_subprocess_exec(
                *worker_argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=65536,
                env=worker_env, **child_options())
            self.worker_mode = mode
            # Mute may arrive while subprocess creation yields. Never leave a stale worker.
            if self.muted or not self.config["privacy"]["onboarded"] or (mode == "wake_word" and self.config["activation"]["mode"] != "wake_word"):
                await self._stop_worker_locked()
                return
            self.worker.stdin.write(encoded)
            await self.worker.stdin.drain()
            if mode not in ("wake_word", "dictation"):   # only these take control lines
                self.worker.stdin.close()
            self.voice_task = asyncio.create_task(self.read_voice(self.worker, mode))
            self.publish()

    async def steer_worker(self, line: bytes) -> bool:
        """Start (b"listen") or drop (b"cancel") a command session in the ready
        wake worker, keeping its models loaded. False if there is none to steer."""
        async with self.worker_lock:
            proc = self.worker
            if (proc is None or self.worker_mode != "wake_word" or self.unanswered is None
                    or self.muted or self.activation.state == "error"):
                return False
            self.activation.arm()   # nothing heard before this line may run
            self.unanswered += 1
            try:
                proc.stdin.write(line + b"\n")
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                return False   # it just exited; the caller starts a new one
            return True

    async def read_voice(self, proc, mode):
        try:
            while line := await proc.stdout.readline():
                event = json.loads(line)
                if self.worker is not proc:
                    return
                kind = event["event"]
                if self.unanswered and kind in ("wake", "command", "heard", "timeout"):
                    continue   # from the session that a listen or cancel line replaced
                if kind == "loading":
                    self.message = event["message"]
                elif kind == "ready":
                    self.unanswered = 0 if self.unanswered is None else self.unanswered - 1
                    self.unsupported = event.get("unsupported", [])
                    self.engine = event.get("engine")
                    self.activation.arm()
                    if event.get("listening"):
                        self.activation.listen(deadline=event.get("deadline"))
                    self.message = "Say a command" if event.get("listening") else ("Microphone test · ends in 15 seconds" if mode == "test" else "Waiting for “" + self.config["activation"]["wake_phrase"] + "”")
                elif kind == "wake":
                    self.activation.listen(deadline=event.get("deadline"))
                    self.message = "Listening · say one command"
                elif kind == "command":
                    accepted = self.activation.accept(event["text"], event.get("confidence"),
                                                      captured_at=event.get("captured_at"))
                    if accepted:
                        self.message = "Processing command…"
                        self.publish()
                        await self.execute(event["text"])
                        if event.get("closest_match") and event.get("heard"):
                            # Say what was actually heard, so a wrong guess is visible.
                            self.message = f"Heard “{event['heard']}” → {event['text']}. {self.message}"
                    else:
                        self.message = "No confident match. Try again or adjust sensitivity."
                    if not self.muted:
                        self.activation.arm() if mode == "wake_word" else self.activation.pause()
                elif kind == "heard":
                    self.message = f"Heard “{event.get('text', '')}”, which isn't a command I know. Try again."
                elif kind == "timeout":
                    self.message = "No command heard. Try again."
                    self.activation.arm() if mode == "wake_word" else self.activation.pause()
                elif kind == "dictation":
                    if event.get("state") == "started":
                        self.dictation.active = True
                        self.message = self.dictation.message = "Dictation · speak, then say “stop dictation” or press Stop"
                    elif self.dictation.active:
                        reason = {"idle": "Dictation stopped after a quiet stretch", "limit": "Dictation stopped at its time limit",
                                  "cancel": "Dictation cancelled", "error": "Dictation could not start"}.get(event.get("reason"), "Dictation stopped")
                        if event.get("dropped"):
                            reason += f" · {event['dropped']} part(s) were skipped because transcription fell behind"
                        await self.end_dictation(reason)
                elif kind == "dictation_text":
                    await self.dictated(event.get("text", ""))
                elif kind == "dictation_error":
                    self.message = self.dictation.message = "Dictation: " + event.get("message", "transcription failed")
                elif kind == "level":
                    self.level = event["level"]
                    self.gate = event.get("gate")
                elif kind == "error":
                    self.activation.error()
                    self.message = event["message"]
                self.publish()
            await proc.wait()
            if self.worker is proc:
                self.worker = None
                self.level = 0
                if mode in ("test", "once") and not self.muted and self.activation.state != "error" and self.config["activation"]["mode"] == "wake_word":
                    self.background(self.start_worker("wake_word"), "voice restart")
                elif self.activation.state != "error":
                    if mode == "wake_word" and not self.muted:
                        self.activation.error()
                        self.message = "Voice worker stopped unexpectedly. Check the microphone and choose Resume."
                    else:
                        self.activation.pause()
                self.publish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.stop_worker()
            self.activation.error()
            self.message = "Voice worker failed: " + str(exc)
            self.publish()

    async def set_mute(self, muted):
        if type(muted) is not bool:
            raise CommandError("Mute must be true or false")
        if not muted and not self.config["privacy"]["onboarded"]:
            raise CommandError("Complete first-run microphone consent in Settings")
        self.muted = muted
        if muted:
            # Mark closed before awaiting termination so no queued result can dispatch.
            await self.end_dictation("Dictation stopped: microphone muted")
            self.activation.pause()
            await self.stop_worker()
            self.message = "Microphone muted · audio capture closed"
        elif self.config["activation"]["mode"] == "wake_word":
            await self.start_worker("wake_word")
        else:
            await self.stop_worker()
            self.activation.pause()
            self.message = "Push-to-talk ready · microphone closed"
        self.publish()

    async def start_dictation(self):
        """Dictation in the running hands-free worker, or a dictation-only worker."""
        if self.muted or not self.config["privacy"]["onboarded"]:
            raise CommandError("Microphone is muted. Complete consent and unmute in Settings.")
        if self.dictation.active:
            return
        if not self.config["recognition"]["accurate"]:
            raise CommandError("Dictation needs the speech model. Turn on Accurate recognition in Voice settings.")
        try:
            typer = await dictation.Typer.start()
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            raise CommandError(str(exc)) from exc
        session = dictation.Session()
        session.active, session.typer = True, typer
        session.window = (await asyncio.to_thread(dictation.focused) or (None,))[0]
        language = self.config["dictation"]["language"]
        session.language = language if language != "auto" else ("ar" if self.config["recognition"]["language"] == "ar_en" else "en")
        session.message = "Starting dictation…"
        session.indicator = dictation.indicator()
        self.dictation = session
        async with self.worker_lock:
            proc = self.worker
            steered = False
            if proc is not None and self.worker_mode == "wake_word" and self.unanswered is not None:
                try:
                    proc.stdin.write(b"dictate\n")
                    await proc.stdin.drain()
                    steered = True
                except (BrokenPipeError, ConnectionResetError):
                    pass
        if not steered:
            try:
                await self.start_worker("dictation")
            except (CommandError, ValueError, OSError) as exc:
                await self.end_dictation(str(exc))
                raise CommandError(str(exc)) from exc
        self.publish()

    async def stop_dictation(self, cancel=False):
        if not self.dictation.active:
            return
        proc = self.worker
        try:
            if proc is None or proc.stdin.is_closing():
                raise BrokenPipeError
            proc.stdin.write(b"cancel\n" if cancel else b"dictate_stop\n")
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, AttributeError):
            await self.end_dictation("Dictation stopped")
        self.publish()

    async def end_dictation(self, message):
        session = self.dictation
        if session.typer is not None:
            typer, session.typer = session.typer, None
            self.background(typer.close(), "dictation helper")
        if session.indicator is not None and session.indicator.poll() is None:
            indicator, session.indicator = session.indicator, None
            self.loop.call_later(2, lambda: indicator.poll() is None and indicator.terminate())
        if session.active or message:
            session.message = self.message = message
        session.active = False
        session.window = None

    async def dictated(self, text):
        session = self.dictation
        if not session.active or session.typer is None:
            return
        text, stop = dictation.split_stop(text.strip(), self.config["dictation"]["stop_phrases"])
        if text.strip():
            text = (" " if session.typed_any else "") + text.strip()   # never Enter or Tab
            session.typed_any = True
            window = await asyncio.to_thread(dictation.focused)
            if session.window is not None and (window is None or window[0] != session.window):
                session.held += text
                await self.stop_dictation(cancel=True)
                await self.end_dictation("Focus moved to another window, so dictation stopped without typing there.")
                self.publish()
                return
            keyboard = self.config["dictation"]["keyboard_fallback"] and not (window and window[1])
            try:
                result = await session.typer.insert(text, keyboard)
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                result = {"ok": False, "reason": "error", "message": f"Dictation could not type the text: {exc}"}
            if result.get("ok"):
                session.inserted += len(text)
                session.method = result.get("method", "")
            else:
                session.held += text
                session.message = self.message = result.get("message", "Dictation could not type the text")
                if result.get("reason") in ("focus", "sensitive", "blocked"):
                    await self.stop_dictation(cancel=True)
                    await self.end_dictation(session.message)
        if stop:
            await self.stop_dictation()

    async def execute(self, phrase):
        async with self.action_lock:
            intent = None
            try:
                intent = self.parser.parse(phrase)
                if intent.action in ("dictation_start", "dictation_stop"):
                    if intent.action == "dictation_start":
                        await self.start_dictation()
                        self.message = "Dictation starting · speak into the focused text field"
                    else:
                        await self.stop_dictation()
                        self.message = "Dictation stopped"
                    self.publish()
                    return {"ok": True, "message": self.message}
                if os.environ.get("MURMUR_DRY_RUN") == "1":
                    self.message = "Dry run: " + intent.action
                else:
                    self.message = await self.desktop_call(self.dispatcher.execute, intent)
            except (CommandError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                if intent and intent.action == "replay_save" and self.config["replay"]["sound"]:
                    from .feedback import play
                    self.background(asyncio.to_thread(play, "error"), "replay sound")
                self.message = str(exc)
                self.publish()
                return {"ok": False, "error": str(exc)}
            self.publish()
            return {"ok": True, "message": self.message}

    async def handle(self, payload):
        op = payload.get("op")
        if op == "shutdown" and is_windows():
            self.stopping.set()
            return {"ok": True, "pid": os.getpid()}
        if op == "status":
            from .process_memory import snapshot
            return {"ok": True, "status": self.status(), "pid": os.getpid(), "memory": snapshot()}
        if op == "config":
            return {"ok": True, "config": self.config}
        if op == "apps":
            # Pick up apps installed since the last scan (the UI asks on open/Reconnect).
            if await self.desktop_call(self.apps.rescan) and self.worker and not self.muted \
                    and self.config["activation"]["mode"] == "wake_word":
                self.background(self.start_worker("wake_word"), "voice restart")
            entries = self.apps.list()
            if not entries and getattr(self.apps, "scan_error", None):
                raise CommandError(self.apps.scan_error)
            if self.worker:
                excluded = set(self.unsupported)
                for app in entries:
                    app["speakable"] = any("open " + normalize(n) not in excluded for n in [app["name"], *app["aliases"]] if len(n) <= 256)
            return {"ok": True, "apps": entries}
        if op == "commands":
            return {"ok": True, "commands": REFERENCE, "applications": self.apps.voice_commands(self.parser)}
        if op == "devices":
            return {"ok": True, "devices": await asyncio.to_thread(devices)}
        if op == "recording_devices":
            return {"ok": True, "devices": [{"id": d["name"], "name": d["description"], "monitor": d["monitor"]} for d in await asyncio.to_thread(audio_sources)]}
        if op == "monitors":
            return {"ok": True, "monitors": await asyncio.to_thread(self.recorder.sources)}
        if op == "mute":
            await self.set_mute(payload["muted"])
        elif op == "listen":
            if not await self.steer_worker(b"listen"):
                await self.start_worker("once")
        elif op == "cancel":
            if not await self.steer_worker(b"cancel"):
                await self.stop_worker()
                self.activation.pause()
                if not self.muted and self.config["activation"]["mode"] == "wake_word":
                    await self.start_worker("wake_word")
                else:
                    self.message = "Cancelled · microphone closed"
        elif op == "dictation":
            action = payload.get("action")
            if action == "toggle":
                action = "stop" if self.dictation.active else "start"
            if action == "start":
                await self.start_dictation()
            elif action in ("stop", "cancel"):
                await self.stop_dictation(cancel=action == "cancel")
            elif action == "copied":
                self.dictation.held = ""
            else:
                raise CommandError("Dictation action must be start, stop, cancel or toggle")
        elif op == "mic_test":
            if payload.get("enabled"):
                await self.start_worker("test")
            elif self.worker is not None and self.worker_mode == "test":   # a finished test must not reload wake capture
                await self.stop_worker()
                if not self.muted and self.config["activation"]["mode"] == "wake_word":
                    await self.start_worker("wake_word")
                else:
                    self.activation.pause()
        elif op == "save_config":
            c = configuration.validate(payload["config"])
            if self.recorder.status()["state"] in ("recording", "stopping") and c["recording"] != self.config["recording"]:
                raise CommandError("Stop recording before changing capture settings")
            if self.replay.status()["state"] in ("on", "stopping") and (
                    c["recording"] != self.config["recording"] or any(
                        c["replay"][key] != self.config["replay"][key]
                        for key in ("seconds", "game_audio", "microphone", "storage", "max_mb", "buffer_dir",
                                    "audio_tracks", "separate_apps"))):
                raise CommandError("Stop instant replay before changing capture settings")
            was_onboarded = self.config["privacy"]["onboarded"]
            self.config = await asyncio.to_thread(configuration.save, c)
            self.parser = Parser.from_config(c)
            self.apps = await self.desktop_call(AppRegistry, c["aliases"])
            self.dispatcher.apps = self.apps
            self.dispatcher.config = self.config
            self.recorder.config = self.replay.config = self.config
            a = c["activation"]
            self.activation.timeout, self.activation.confidence = a["command_timeout"], a["confidence"]
            if not c["privacy"]["onboarded"]:
                await self.set_mute(True)
            else:
                await self.set_mute(self.muted if was_onboarded else False)
            return {"ok": True, "config": self.config}
        elif op in ("replay_start", "replay_stop", "replay_save"):
            return await self.execute(op.removeprefix("replay_") + " replay")
        elif op in ("record_start", "record_stop"):
            return await self.execute("start recording" if op == "record_start" else "stop recording")
        elif op == "execute":
            # Trusted local IPC/CLI can test deterministic parsing, never arbitrary shell.
            return await self.execute(payload["phrase"])
        elif op == "open_ui":
            self.open_ui("recording" if payload.get("page") == "recording" else "dashboard")
        elif op == "set_startup":
            enabled = payload.get("enabled")
            if type(enabled) is not bool:
                raise CommandError("Startup must be true or false")
            if is_windows():
                from .windows.startup import set_startup
                await asyncio.to_thread(set_startup, enabled)
            else:
                r = await asyncio.to_thread(subprocess.run, ["systemctl", "--user", "enable" if enabled else "disable", "murmur.service"], capture_output=True, text=True, timeout=5)
                if r.returncode:
                    raise CommandError("Install the optional user service first (see README). " + r.stderr.strip()[:300])
            self.config["startup"] = enabled
            await asyncio.to_thread(configuration.save, self.config)
        else:
            raise CommandError("Unsupported operation")
        self.publish()
        return {"ok": True, "status": self.status()}


def devices():
    if is_windows():
        from .windows.audio import devices as windows_devices
        return windows_devices()
    result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise CommandError("Cannot reach PipeWire; check the graphical session")
    nodes = json.loads(result.stdout)
    return [{"id": "", "name": "System default input"}] + [
        {"id": p["node.name"], "name": p.get("node.description", p["node.name"])}
        for n in nodes if (p := n.get("info", {}).get("props", {})).get("media.class") == "Audio/Source" and "node.name" in p]


async def serve():
    service = Service()
    service.loop = asyncio.get_running_loop()
    token = None
    if is_windows():
        from .windows import transport
        from .windows.system import ServiceLock, own_service_children, private_directory
        path = transport.endpoint_path()
        private_directory(path.parent)
        lock = ServiceLock(path.with_name("service.lock"))
        own_service_children()
        token = transport.new_token()
    else:
        import fcntl
        path = Path(socket_path())
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("IPC directory must be owned by you with mode 0700")
        lock = (path.parent / "service.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise RuntimeError("Murmur service is already running") from exc
    path.unlink(missing_ok=True)
    async def connection(reader, writer):
        queue = None
        try:
            if not is_windows():
                peer = writer.get_extra_info("socket")
                _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    return
            line = await asyncio.wait_for(reader.readline(), 5)
            if len(line) > 65536:
                raise ValueError("Request exceeds 64 KiB")
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("Request must be an object")
            if is_windows() and not transport.authenticate(payload, token):
                raise ValueError("Unauthorized Jarvis client")
            if payload.get("op") == "watch":
                queue = asyncio.Queue(maxsize=8)
                service.watchers.add(queue)
                queue.put_nowait({"ok": True, "status": service.status()})
                # Detect disconnect while status is unchanged, without timer polling.
                # Subscriptions also end at shutdown: Server.wait_closed() waits
                # for every connection, so an open Settings window or Waybar
                # module would otherwise keep the service from exiting.
                eof = asyncio.create_task(reader.read(1))
                stop = asyncio.create_task(service.stopping.wait())
                try:
                    while not eof.done() and not stop.done():
                        event = asyncio.create_task(queue.get())
                        done, _ = await asyncio.wait([eof, stop, event], return_when=asyncio.FIRST_COMPLETED)
                        if event not in done:
                            event.cancel()
                            await asyncio.gather(event, return_exceptions=True)
                            break
                        writer.write((json.dumps(event.result()) + "\n").encode())
                        await asyncio.wait_for(writer.drain(), 2)
                finally:
                    eof.cancel()
                    stop.cancel()
            else:
                result = await service.handle(payload)
                writer.write((json.dumps(result) + "\n").encode())
                await writer.drain()
        except (ValueError, KeyError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            try:
                writer.write((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())
                await writer.drain()
            except (OSError, ConnectionError):
                pass
        finally:
            if queue:
                service.watchers.discard(queue)
            writer.close()
            await writer.wait_closed()
    if is_windows():
        server = await asyncio.start_server(connection, "127.0.0.1", 0, limit=65536)
        transport.publish(server.sockets[0].getsockname()[1], token)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: service.loop.call_soon_threadsafe(service.stopping.set))
    else:
        server = await asyncio.start_unix_server(connection, str(path), limit=65536)
        path.chmod(0o600)
        for sig in (signal.SIGINT, signal.SIGTERM):
            service.loop.add_signal_handler(sig, service.stopping.set)
    try:
        if is_windows():
            from .windows.tray import Tray
            service.tray = Tray(service)
            service.tray.start()
        try:
            await asyncio.to_thread(service.recorder.inspect_previous)
        except OSError as exc:
            print(f"murmur: could not inspect earlier recordings: {exc}", file=sys.stderr)
        if service.config["replay"]["autostart"]:
            try:
                await asyncio.to_thread(service.replay.start)
            except RuntimeError as exc:   # shown in status; the service keeps running
                print(f"murmur: instant replay did not start: {exc}", file=sys.stderr)
        if not service.muted:
            # A voice failure must not take the whole service down (and into a
            # systemd restart loop); show it and wait for the user instead.
            try:
                await service.set_mute(False)
            except (CommandError, RuntimeError, OSError) as exc:
                service.activation.error()
                service.message = f"Voice control could not start: {exc}"
        async with server:
            await service.stopping.wait()
    finally:
        server.close()
        await server.wait_closed()
        try:
            await service.set_mute(True)
            results = await asyncio.gather(asyncio.to_thread(service.replay.close),
                                           asyncio.to_thread(service.recorder.close), return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    print(f"murmur: capture cleanup: {result}", file=sys.stderr)
        finally:
            path.unlink(missing_ok=True)
            lock.close()
            if service.tray:
                service.tray.close()
            if service.desktop_executor:
                service.desktop_executor.shutdown(wait=False, cancel_futures=True)


def main():
    asyncio.run(serve())
