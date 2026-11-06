"""Event-driven notification-area controls; the service owns the tray thread."""

import threading


class Tray:
    def __init__(self, service):
        self.service = service
        self.hwnd = None
        self.ready = threading.Event()
        self.error = None
        self.tooltip = "Jarvis · microphone closed"

    def start(self):
        self.thread = threading.Thread(
            target=self._run, name="jarvis-tray", daemon=True
        )
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            raise RuntimeError(
                "Could not create the Jarvis tray indicator: "
                + str(self.error or "timeout")
            )

    def update(self, status):
        import win32con
        import win32gui

        mic = "MIC OPEN" if status["microphone_open"] else "mic closed"
        capture = " · RECORDING" if status["recording"]["state"] == "recording" else ""
        replay = " · replay on" if status["replay"]["state"] == "on" else ""
        tooltip = f"Jarvis · {mic}{capture}{replay}"
        if tooltip != self.tooltip:
            self.tooltip = tooltip
            if self.hwnd:
                win32gui.PostMessage(self.hwnd, win32con.WM_APP + 2, 0, 0)

    def _run(self):
        import win32api
        import win32con
        import win32gui

        callback = win32con.WM_APP + 1
        taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")

        def icon(add=False):
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_ADD if add else win32gui.NIM_MODIFY,
                (
                    self.hwnd,
                    0,
                    win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
                    callback,
                    win32gui.LoadIcon(0, win32con.IDI_APPLICATION),
                    self.tooltip,
                ),
            )

        def invoke(op, **payload):
            self.service.loop.call_soon_threadsafe(
                lambda: self.service.background(
                    self.service.handle({"op": op, **payload}), "tray action"
                )
            )

        def message(hwnd, msg, wparam, lparam):
            if msg == taskbar_created:
                icon(True)
            elif msg == win32con.WM_APP + 2:
                icon()
            elif msg == callback:
                if lparam == win32con.WM_LBUTTONDBLCLK:
                    invoke("open_ui")
                elif lparam == win32con.WM_RBUTTONUP:
                    menu = win32gui.CreatePopupMenu()
                    muted = self.service.muted
                    for ident, label in (
                        (1, "Open Jarvis"),
                        (2, "Resume microphone" if muted else "Mute microphone"),
                        (3, "Listen once"),
                        (4, "Stop recording"),
                        (5, "Save replay"),
                        (6, "Quit Jarvis"),
                    ):
                        win32gui.AppendMenu(menu, win32con.MF_STRING, ident, label)
                    try:
                        win32gui.SetForegroundWindow(hwnd)
                        choice = win32gui.TrackPopupMenu(
                            menu,
                            win32con.TPM_RETURNCMD | win32con.TPM_NONOTIFY,
                            *win32gui.GetCursorPos(),
                            0,
                            hwnd,
                            None,
                        )
                        if choice == 2:
                            invoke("mute", muted=not muted)
                        elif choice in (1, 3, 4, 5, 6):
                            invoke(
                                {
                                    1: "open_ui",
                                    3: "listen",
                                    4: "record_stop",
                                    5: "replay_save",
                                    6: "shutdown",
                                }[choice]
                            )
                    finally:
                        win32gui.DestroyMenu(menu)
            elif msg == win32con.WM_CLOSE:
                win32gui.DestroyWindow(hwnd)
            elif msg == win32con.WM_DESTROY:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
                win32gui.PostQuitMessage(0)
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        try:
            cls = win32gui.WNDCLASS()
            cls.hInstance = win32api.GetModuleHandle(None)
            cls.lpszClassName = "JarvisServiceTray"
            cls.lpfnWndProc = message
            win32gui.RegisterClass(cls)
            self.hwnd = win32gui.CreateWindow(
                cls.lpszClassName, "Jarvis", 0, 0, 0, 0, 0, 0, 0, cls.hInstance, None
            )
            icon(True)
            self.ready.set()
            win32gui.PumpMessages()
        except Exception as exc:
            self.error = exc
            self.ready.set()
        finally:
            self.hwnd = None

    def close(self):
        if self.hwnd:
            import win32con
            import win32gui

            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
        self.thread.join(timeout=3)
