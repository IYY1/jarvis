"""User-only named pipes for FFmpeg audio and bounded replay segments."""

from __future__ import annotations
import threading
from .system import security_attributes


class Pipe:
    def __init__(self, name, *, outbound):
        import win32pipe

        if not name.startswith(r"\\.\pipe\Jarvis-") or len(name) > 240:
            raise ValueError("Invalid private capture pipe")
        self.handle = win32pipe.CreateNamedPipe(
            name,
            (
                win32pipe.PIPE_ACCESS_OUTBOUND
                if outbound
                else win32pipe.PIPE_ACCESS_INBOUND
            )
            | 0x00080000,
            win32pipe.PIPE_TYPE_BYTE
            | win32pipe.PIPE_READMODE_BYTE
            | win32pipe.PIPE_WAIT
            | 0x8,
            1,
            65536,
            65536,
            0,
            security_attributes(),
        )  # first instance; reject remote clients
        self.connected = False

    def connect(self):
        import win32pipe

        try:
            win32pipe.ConnectNamedPipe(self.handle, None)
        except Exception as exc:
            if getattr(exc, "winerror", exc.args[0] if exc.args else None) != 535:
                raise
        self.connected = True

    def close(self):
        if self.handle is not None:
            import win32api

            # Cancel blocked ConnectNamedPipe/ReadFile in this instance only.
            try:
                import ctypes
                from ctypes import wintypes

                cancel = ctypes.WinDLL("kernel32", use_last_error=True).CancelIoEx
                cancel.argtypes, cancel.restype = (
                    [wintypes.HANDLE, wintypes.LPVOID],
                    wintypes.BOOL,
                )
                cancel(int(self.handle), None)
            except OSError:
                pass
            win32api.CloseHandle(self.handle)
            self.handle = None


class PipeWriter(Pipe):
    def __init__(self, name):
        super().__init__(name, outbound=True)

    def __enter__(self):
        self.connect()
        return self

    def write(self, data):
        import win32file

        win32file.WriteFile(self.handle, data)

    def __exit__(self, *_):
        self.close()


class SegmentReader:
    """Three reusable pipes, never one thread per retained replay second."""

    MAX_SEGMENT = 64 * 1024 * 1024

    def __init__(self, prefix, on_segment, on_error):
        self.closed = threading.Event()
        self.pipes = [
            Pipe(f"{prefix}{index:03d}", outbound=False) for index in range(3)
        ]
        self.threads = []
        for index, pipe in enumerate(self.pipes):
            thread = threading.Thread(
                target=self._read,
                args=(index, pipe, on_segment, on_error),
                daemon=True,
                name="jarvis-replay-pipe",
            )
            self.threads.append(thread)
            thread.start()

    def _read(self, index, pipe, on_segment, on_error):
        import win32file
        import win32pipe

        cycle = 0
        try:
            while not self.closed.is_set():
                pipe.connect()
                chunks, size = [], 0
                while not self.closed.is_set():
                    try:
                        _, chunk = win32file.ReadFile(pipe.handle, 65536)
                    except Exception as exc:
                        if getattr(
                            exc, "winerror", exc.args[0] if exc.args else None
                        ) in (109, 232):
                            break
                        raise
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.MAX_SEGMENT:
                        raise RuntimeError(
                            "Replay segment exceeded its memory limit; use a lower bitrate"
                        )
                    chunks.append(chunk)
                if chunks and not self.closed.is_set():
                    on_segment(b"".join(chunks), index + 3 * cycle)
                    chunks.clear()
                    cycle += 1
                win32pipe.DisconnectNamedPipe(pipe.handle)
                pipe.connected = False
        except Exception as exc:
            if not self.closed.is_set():
                on_error(str(exc))

    def close(self):
        self.closed.set()
        for pipe in self.pipes:
            pipe.close()
        for thread in self.threads:
            thread.join(timeout=1)
