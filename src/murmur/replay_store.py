"""Small adapter to the Rust disk store; no capture starts on import."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import threading
import time

from .host import app_dir, child_options, data_dir, is_windows
from .recorder import RecorderError


def binary():
    name = "jarvis-replay-store" + (".exe" if is_windows() else "")
    candidates = [app_dir() / "tools" / name, Path(__file__).parent / "bin" / name]
    for path in candidates:
        if path.is_file():
            return str(path)
    raise RecorderError("Rust replay storage is missing. Build scripts/build_replay_store.py or reinstall the complete Jarvis package.")


class DiskReplayBuffer:
    REQUEST_TIMEOUT = 10

    def __init__(self, seconds, max_bytes, directory=""):
        root = Path(directory).expanduser() if directory else data_dir() / "replay-buffer"
        root = root.absolute()
        root.parent.mkdir(parents=True, exist_ok=True)
        if is_windows():
            from .windows.system import private_directory
            if root.is_symlink() or root.exists() and getattr(root.lstat(), "st_file_attributes", 0) & 0x400:
                raise RecorderError("Replay storage cannot be a junction or reparse point")
            private_directory(root)
        self.changed = threading.Condition()
        self.last = 0.0
        self.closed = False
        self.saving = False
        self.failed = False
        self.pending, self.next = {}, 0
        self.process = subprocess.Popen(
            [binary()], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, **child_options(),
        )
        # A separate condition lets this watchdog stop a helper while the
        # capture thread holds changed inside a blocked pipe write/read.
        self._deadline = threading.Condition()
        self._deadline_at = None
        self._expired = False
        self._watchdog_closed = False
        self._watchdog = threading.Thread(target=self._watch_deadline,
                                          name="jarvis-replay-store-deadline", daemon=True)
        self._watchdog.start()
        try:
            self._request("open", root=str(root), seconds=seconds, bytes=max_bytes)
        except Exception:
            self.close()
            raise

    def _watch_deadline(self):
        with self._deadline:
            while not self._watchdog_closed:
                if self._deadline_at is None:
                    self._deadline.wait()
                    continue
                remaining = self._deadline_at - time.monotonic()
                if remaining > 0:
                    self._deadline.wait(remaining)
                    continue
                self._expired = True
                break
            else:
                return
        # Killing the owned helper unblocks both full input pipes and stalled
        # replies. Its marked crash session is removed on the next start.
        try:
            self.process.kill()
        except OSError:
            pass

    def _deadline_error(self):
        return RecorderError(f"Replay disk storage exceeded its {self.REQUEST_TIMEOUT:g}-second request deadline; replay stopped")

    def _request(self, op, data=None, **fields):
        # All calls are serialized by changed. The helper reads at most 64 KiB
        # at once, even when a pipe reader has delivered a large segment.
        with self._deadline:
            if self._expired:
                raise self._deadline_error()
            self._deadline_at = time.monotonic() + self.REQUEST_TIMEOUT
            self._deadline.notify_all()
        try:
            self.process.stdin.write(json.dumps(dict(op=op, **fields)).encode() + b"\n")
            if data is not None:
                self.process.stdin.write(data)
            self.process.stdin.flush()
            reply = json.loads(self.process.stdout.readline(8 * 1024**2) or b"{}")
            if reply.get("ok") is not True:
                raise RecorderError(reply.get("error", "Replay disk store stopped unexpectedly"))
            return reply
        except (OSError, ValueError) as exc:
            raise RecorderError(f"Replay disk storage failed: {exc}") from exc
        finally:
            with self._deadline:
                expired = self._expired
                self._deadline_at = None
                self._deadline.notify_all()
            if expired:
                raise self._deadline_error()

    def append(self, data, sequence):
        with self.changed:
            if self.closed or self.failed:
                raise RecorderError("Replay disk buffer is closed")
            try:
                self._request("append", data=data, sequence=sequence, bytes=len(data))
            except RecorderError:
                self.failed = True
                self.changed.notify_all()
                raise
            # Complete contiguous history is determined by Rust; a snapshot
            # after this point includes the current capture second.
            self.pending[sequence] = time.monotonic()
            while self.next in self.pending:
                self.last = max(self.last, self.pending.pop(self.next))
                self.next += 1
            self.changed.notify_all()

    @contextmanager
    def files(self, *, after=0, timeout=0):
        with self.changed:
            if after:
                self.changed.wait_for(lambda: self.last >= after or self.closed or self.failed, timeout)
            if self.closed or self.failed or after and self.last < after:
                raise RecorderError("Replay could not finalize the current second; check capture status")
            paths = tuple(Path(p) for p in self._request("snapshot")["paths"])
            self.saving = True
        try:
            yield paths
        finally:
            with self.changed:
                try:
                    self._request("release")
                finally:
                    self.saving = False
                    if self.closed:
                        self._finish()

    def _finish(self):
        with self._deadline:
            self._watchdog_closed = True
            self._deadline.notify_all()
        self._watchdog.join()
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.close()  # EOF drops the session and releases its lock.
            except OSError:
                pass  # a failed helper can close its end before buffered flush
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.process.stdout.close()

    def close(self):
        with self.changed:
            self.closed = True
            self.changed.notify_all()
            # A save can finish after capture stops. Keep its pinned files until
            # remux has finished; no new segments are accepted in that interval.
            if not self.saving:
                self._finish()
