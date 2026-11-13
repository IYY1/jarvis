"""The listening sign: the Jarvis mark in a corner of the screen while Jarvis waits
for a command.

It turns slowly and breathes while listening, turns faster while a command runs,
flashes green when the command was understood or red when it wasn't, then fades
away. The window never takes focus, lets clicks through, stays out of Alt+Tab and
out of screen capture, so replays, recordings and screenshots never show it. It is
made the first time it is needed (never while switched off) and draws nothing
while hidden: its timer runs only while it shows.
"""

from __future__ import annotations
import ctypes
import math
import queue
import threading
import time

CORNERS = ("top_right", "top_left", "bottom_right", "bottom_left")
SIZE = 54            # the mark's outer ring, in pixels at 100% scaling
MARGIN = 22          # from the screen's edge
GLOW = (0x3E, 0xE0, 0xD0)   # teal
FRAME = (0xC8, 0xD4, 0xD8)  # the light grey outer rings
OK_COLOR, FAIL_COLOR = (0x3D, 0xDC, 0x84), (0xFF, 0x5A, 0x5F)
FRAME_MS = 40        # 25 frames a second, only while it shows


class Look:
    """What the sign shows at a moment. No window here, so it is tested anywhere.

    Phases: "hidden", "listening", "busy" (a command runs), "done" (a green or red
    flash, then fading) and "fading" (no command: it just fades)."""

    FADE_IN, FLASH, FADE_OUT = 0.15, 0.35, 0.4
    TURN, TURN_BUSY = 60.0, 240.0   # degrees a second: a turn in six seconds while listening
    BREATH = 1.6                    # seconds per glow breath

    def __init__(self):
        self.phase, self.changed, self.ok = "hidden", 0.0, None
        self.shown = 0.0          # when it last appeared
        self.angle, self.last = 0.0, 0.0
        self.alpha = self.fade_from = 0.0

    def set(self, phase, now, ok=None):
        if phase in ("done", "fading") and self.phase == "hidden":
            return                # nothing to end
        if phase in ("listening", "busy") and self.phase == "hidden":
            self.shown, self.last = now, now
        elif phase in ("listening", "busy") and self.phase in ("done", "fading"):
            # Back before it vanished: fade in again from how visible it still is.
            self.shown, self.last = now - self.alpha * self.FADE_IN, now
        if phase in ("done", "fading") and self.phase in ("done", "fading"):
            return                # already ending
        self.fade_from = self.alpha or 1.0   # fading starts from how visible it is now
        self.phase, self.changed, self.ok = phase, now, ok

    def frame(self, now):
        """(visible, alpha 0-1, angle in degrees, glow 0-1, ring: None, "ok" or "fail")."""
        if self.phase == "hidden":
            self.alpha = 0.0
            return False, 0.0, self.angle, 0.0, None
        speed = self.TURN_BUSY if self.phase == "busy" else self.TURN
        self.angle = (self.angle + speed * max(0.0, now - self.last)) % 360
        self.last = now
        since = now - self.changed
        ring = None
        if self.phase in ("listening", "busy"):
            alpha = min(1.0, (now - self.shown) / self.FADE_IN)
            glow = 1.0 if self.phase == "busy" else 0.5 + 0.5 * math.sin(2 * math.pi * (now - self.shown) / self.BREATH)
        elif self.phase == "done":
            ring = "ok" if self.ok else "fail"
            alpha = 1.0 if since < self.FLASH else 1.0 - (since - self.FLASH) / self.FADE_OUT
            glow = 1.0
        else:   # fading
            alpha = self.fade_from - since / self.FADE_OUT
            glow = 0.5
        if alpha <= 0 and self.phase in ("done", "fading"):   # faded out; appearing starts at 0 too
            self.phase, self.alpha = "hidden", 0.0
            return False, 0.0, self.angle, 0.0, None
        self.alpha = max(0.0, min(1.0, alpha))
        return True, self.alpha, self.angle, glow, ring


def corner_position(corner, area, size, scale):
    """Top-left of a window of `size` pixels in `corner` of `area` (left, top, right, bottom)."""
    left, top, right, bottom = area
    margin = round(MARGIN * scale)
    x = left + margin if corner.endswith("left") else right - margin - size
    y = top + margin if corner.startswith("top") else bottom - margin - size
    return x, y


class ListeningSign:
    """Thread-safe front of the sign window. `corner()` names the corner, or None when switched off."""

    def __init__(self, corner):
        self.corner = corner
        self.commands = queue.SimpleQueue()
        self.thread = None
        self.window = None
        self.failed = False

    def listen(self):
        self._send("listening")

    def busy(self):
        self._send("busy")

    def done(self, ok):
        self._send("done", bool(ok))

    def hide(self):
        self._send("fading")

    def _send(self, phase, ok=None):
        if self.failed:
            return
        if phase in ("listening", "busy"):
            if not self.corner():
                phase = "fading"   # switched off while it showed: let it go
            elif self.thread is None:
                self._start()
        if self.window is None:
            return
        self.commands.put((phase, ok))
        self.window.wake()

    def _start(self):
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(ready,), name="jarvis-listening-sign", daemon=True)
        self.thread.start()
        ready.wait(3)

    def _run(self, ready):
        try:
            self.window = _Window(self)
        except Exception as exc:   # never let the sign take voice control down with it
            import sys
            print(f"jarvis: listening sign unavailable: {exc!r}", file=sys.stderr, flush=True)
            self.failed = True
            ready.set()
            return
        ready.set()
        self.window.run()

    def close(self):
        if self.window is not None:
            self.window.close()


class _Window:
    """The layered Win32 window, drawn with GDI+ into a premultiplied 32-bit bitmap."""

    def __init__(self, owner):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes, self.w = ctypes, w
        self.owner, self.look = owner, Look()
        user32, gdi32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32")
        gdiplus, kernel32 = ctypes.WinDLL("gdiplus"), ctypes.WinDLL("kernel32")
        self.user32, self.gdi32, self.gdiplus = user32, gdi32, gdiplus
        H, P = w.HANDLE, ctypes.c_void_p
        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        user32.DefWindowProcW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        user32.DefWindowProcW.restype = LRESULT
        user32.CreateWindowExW.argtypes = (w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, P)
        user32.CreateWindowExW.restype = w.HWND
        user32.PostMessageW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        user32.SetTimer.argtypes = (w.HWND, ctypes.c_size_t, w.UINT, P)
        user32.SetTimer.restype = ctypes.c_size_t
        user32.KillTimer.argtypes = (w.HWND, ctypes.c_size_t)
        user32.SetWindowPos.argtypes = (w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT)
        user32.ShowWindow.argtypes = (w.HWND, ctypes.c_int)
        user32.GetForegroundWindow.restype = w.HWND
        user32.MonitorFromWindow.argtypes = (w.HWND, w.DWORD)
        user32.MonitorFromWindow.restype = H
        user32.GetMonitorInfoW.argtypes = (H, P)
        user32.UpdateLayeredWindow.argtypes = (w.HWND, w.HDC, P, P, w.HDC, P, w.COLORREF, P, w.DWORD)
        user32.GetDC.argtypes = (w.HWND,)
        user32.GetDC.restype = w.HDC
        user32.ReleaseDC.argtypes = (w.HWND, w.HDC)
        user32.SetWindowDisplayAffinity.argtypes = (w.HWND, w.DWORD)
        user32.GetMessageW.argtypes = (P, w.HWND, w.UINT, w.UINT)
        user32.DispatchMessageW.argtypes = (P,)
        user32.TranslateMessage.argtypes = (P,)
        user32.DestroyWindow.argtypes = (w.HWND,)
        gdi32.CreateCompatibleDC.argtypes = (w.HDC,)
        gdi32.CreateCompatibleDC.restype = w.HDC
        gdi32.CreateDIBSection.argtypes = (w.HDC, P, w.UINT, ctypes.POINTER(P), H, w.DWORD)
        gdi32.CreateDIBSection.restype = H
        gdi32.SelectObject.argtypes = (w.HDC, H)
        gdi32.SelectObject.restype = H
        gdi32.DeleteObject.argtypes = (H,)
        for name, types in {
            "GdipCreateBitmapFromStream": (P, P), "GdipCreateBitmapFromScan0": (ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, P, P),
            "GdipGetImageGraphicsContext": (P, P), "GdipSetSmoothingMode": (P, ctypes.c_int),
            "GdipSetInterpolationMode": (P, ctypes.c_int), "GdipSetPixelOffsetMode": (P, ctypes.c_int),
            "GdipGraphicsClear": (P, ctypes.c_uint32), "GdipResetWorldTransform": (P,),
            "GdipTranslateWorldTransform": (P, ctypes.c_float, ctypes.c_float, ctypes.c_int),
            "GdipRotateWorldTransform": (P, ctypes.c_float, ctypes.c_int),
            "GdipDrawImageRectI": (P, P, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int),
            "GdipCreateSolidFill": (ctypes.c_uint32, P), "GdipSetSolidFillColor": (P, ctypes.c_uint32),
            "GdipFillEllipse": (P, P, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float),
            "GdipCreatePen1": (ctypes.c_uint32, ctypes.c_float, ctypes.c_int, P), "GdipSetPenColor": (P, ctypes.c_uint32),
            "GdipDrawEllipse": (P, P, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float),
            "GdipDrawLine": (P, P, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float),
            "GdipSetPenDashArray": (P, P, ctypes.c_int), "GdipSetPenDashStyle": (P, ctypes.c_int),
            "GdipDrawArc": (P, P, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float),
            "GdipSetPenWidth": (P, ctypes.c_float), "GdipSetPenStartCap": (P, ctypes.c_int), "GdipSetPenEndCap": (P, ctypes.c_int),
            "GdipFlush": (P, ctypes.c_int), "GdipDeleteGraphics": (P,), "GdipDisposeImage": (P,),
            "GdipDeleteBrush": (P,), "GdipDeletePen": (P,),
        }.items():
            getattr(gdiplus, name).argtypes = types
        try:   # this thread's coordinates are real pixels on every monitor
            user32.SetThreadDpiAwarenessContext(P(-4))
        except AttributeError:
            pass

        class StartupInput(ctypes.Structure):
            _fields_ = [("version", ctypes.c_uint32), ("callback", P), ("no_thread", w.BOOL), ("no_codecs", w.BOOL)]
        self.token = ctypes.c_size_t()
        gdiplus.GdiplusStartup(ctypes.byref(self.token), ctypes.byref(StartupInput(1, None, False, False)), None)
        self.brush, self.pen = P(), P()
        gdiplus.GdipCreateSolidFill(0, ctypes.byref(self.brush))
        gdiplus.GdipCreatePen1(0, 2.0, 2, ctypes.byref(self.pen))   # 2: UnitPixel
        gdiplus.GdipSetPenStartCap(self.pen, 2)   # round ends on the arcs
        gdiplus.GdipSetPenEndCap(self.pen, 2)

        self.procedure = self.WNDPROC(self._message)   # kept alive as long as the window

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE),
                        ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON)]
        kernel32.GetModuleHandleW.restype = w.HINSTANCE
        instance = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, self.procedure, 0, 0, instance, None, None, None, None,
                          "JarvisListeningSign", None)
        user32.RegisterClassExW.argtypes = (P,)
        user32.RegisterClassExW(ctypes.byref(cls))
        # Layered, click-through, topmost, out of Alt+Tab, never activated.
        ex_style = 0x00080000 | 0x00000020 | 0x00000008 | 0x00000080 | 0x08000000
        self.hwnd = user32.CreateWindowExW(ex_style, "JarvisListeningSign", "Jarvis listening", 0x80000000,
                                           0, 0, 1, 1, None, None, instance, None)
        if not self.hwnd:
            raise OSError(f"could not create the window ({ctypes.get_last_error()})")
        user32.SetWindowDisplayAffinity(self.hwnd, 0x11)   # WDA_EXCLUDEFROMCAPTURE: never in a capture
        self.screen = user32.GetDC(None)
        self.memory = gdi32.CreateCompatibleDC(self.screen)
        self.side, self.scale, self.bitmap, self.graphics, self.canvas = 0, 0.0, None, P(), P()
        self.position = (0, 0)
        self.timer = False

    # Thread-safe -------------------------------------------------------------

    def wake(self):
        self.user32.PostMessageW(self.hwnd, 0x8000, 0, 0)   # WM_APP

    def close(self):
        self.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)   # WM_CLOSE

    # The window's own thread -------------------------------------------------

    def run(self):
        msg = self.w.MSG()
        while self.user32.GetMessageW(self.ctypes.byref(msg), None, 0, 0) > 0:
            self.user32.TranslateMessage(self.ctypes.byref(msg))
            self.user32.DispatchMessageW(self.ctypes.byref(msg))
        self._free()

    def _message(self, hwnd, message, wparam, lparam):
        try:
            if message == 0x8000:    # WM_APP: new phases
                self._take_commands()
                return 0
            if message == 0x0113:    # WM_TIMER
                self._draw()
                return 0
            if message == 0x0010:    # WM_CLOSE
                self.user32.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:    # WM_DESTROY
                self.user32.PostQuitMessage(0)
                return 0
        except Exception as exc:
            import sys
            print(f"jarvis: listening sign: {exc!r}", file=sys.stderr, flush=True)
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _take_commands(self):
        was_hidden = self.look.phase == "hidden"
        while True:
            try:
                phase, ok = self.owner.commands.get_nowait()
            except queue.Empty:
                break
            self.look.set(phase, time.monotonic(), ok)
        if self.look.phase == "hidden":
            return
        if was_hidden:
            self._place()
        if not self.timer:
            self.user32.SetTimer(self.hwnd, 1, FRAME_MS, None)
            self.timer = True
        self._draw()

    def _place(self):
        ctypes, w = self.ctypes, self.w

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]
        # The screen the user is looking at: the one with the focused window.
        monitor = self.user32.MonitorFromWindow(self.user32.GetForegroundWindow(), 1)   # nearest, else primary
        info = MONITORINFO(ctypes.sizeof(MONITORINFO))
        self.user32.GetMonitorInfoW(monitor, ctypes.byref(info))
        dpi_x, dpi_y = w.UINT(96), w.UINT(96)
        try:
            ctypes.WinDLL("shcore").GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        except (OSError, AttributeError):
            pass
        scale = max(1.0, dpi_x.value / 96)
        if scale != self.scale:
            self._canvas(scale)
        r = info.rcWork
        self.position = corner_position(self.owner.corner() or "top_right", (r.left, r.top, r.right, r.bottom),
                                        self.side, scale)
        # SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW, above everything, games included.
        self.user32.SetWindowPos(self.hwnd, self.w.HWND(-1), self.position[0], self.position[1], 0, 0,
                                 0x0001 | 0x0010 | 0x0040)

    def _canvas(self, scale):
        ctypes, w = self.ctypes, self.w
        self._free_canvas()
        self.scale = scale
        self.side = round(SIZE * scale * 1.75)   # room for the light around the mark

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD),
                        ("biBitCount", w.WORD), ("biCompression", w.DWORD), ("biSizeImage", w.DWORD),
                        ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD),
                        ("biClrImportant", w.DWORD)]
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), self.side, -self.side, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = self.bits = ctypes.c_void_p()
        self.bitmap = self.gdi32.CreateDIBSection(self.screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        self.previous = self.gdi32.SelectObject(self.memory, self.bitmap)
        # 0x000E200B: PixelFormat32bppPARGB, what UpdateLayeredWindow wants, drawn straight into the DIB.
        self.gdiplus.GdipCreateBitmapFromScan0(self.side, self.side, self.side * 4, 0x000E200B, bits,
                                               ctypes.byref(self.canvas))
        self.gdiplus.GdipGetImageGraphicsContext(self.canvas, ctypes.byref(self.graphics))
        self.gdiplus.GdipSetSmoothingMode(self.graphics, 4)      # anti-aliased
        self.gdiplus.GdipSetInterpolationMode(self.graphics, 7)  # high-quality bicubic
        self.gdiplus.GdipSetPixelOffsetMode(self.graphics, 4)

    def _draw(self):
        visible, alpha, angle, glow, ring = self.look.frame(time.monotonic())
        if not visible:
            self.user32.ShowWindow(self.hwnd, 0)   # SW_HIDE
            if self.timer:
                self.user32.KillTimer(self.hwnd, 1)
                self.timer = False
            return
        k, middle = self.scale, self.side / 2
        color = OK_COLOR if ring == "ok" else FAIL_COLOR if ring == "fail" else GLOW
        g, gp = self.graphics, self.gdiplus
        gp.GdipGraphicsClear(g, 0)   # see-through: only the lines and the light are drawn
        paint_mark(gp, g, self.brush, self.pen, middle, SIZE * k / 2, k, angle, glow, color, light=middle - SIZE * k / 2)
        gp.GdipFlush(g, 1)
        self._show(alpha)

    def _show(self, alpha):
        ctypes, w = self.ctypes, self.w

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("alpha", ctypes.c_ubyte), ("format", ctypes.c_ubyte)]
        blend = BLENDFUNCTION(0, 0, max(0, min(255, round(alpha * 255))), 1)   # AC_SRC_OVER, AC_SRC_ALPHA
        destination, size, source = w.POINT(*self.position), w.SIZE(self.side, self.side), w.POINT(0, 0)
        self.user32.UpdateLayeredWindow(self.hwnd, self.screen, ctypes.byref(destination), ctypes.byref(size),
                                        self.memory, ctypes.byref(source), 0, ctypes.byref(blend), 2)   # ULW_ALPHA
        self.user32.ShowWindow(self.hwnd, 4)   # SW_SHOWNOACTIVATE

    def _free_canvas(self):
        if self.graphics:
            self.gdiplus.GdipDeleteGraphics(self.graphics)
            self.graphics = self.ctypes.c_void_p()
        if self.canvas:
            self.gdiplus.GdipDisposeImage(self.canvas)
            self.canvas = self.ctypes.c_void_p()
        if self.bitmap:
            self.gdi32.SelectObject(self.memory, self.previous)
            self.gdi32.DeleteObject(self.bitmap)
            self.bitmap = None

    def _free(self):
        self._free_canvas()
        self.gdiplus.GdipDeleteBrush(self.brush)
        self.gdiplus.GdipDeletePen(self.pen)
        self.gdi32.DeleteDC(self.memory)
        self.user32.ReleaseDC(None, self.screen)



def paint_mark(gp, g, brush, pen_, middle, radius, k, angle, glow, color, light=0.0, detail=3):
    """The Jarvis mark, drawn as a HUD dial with GDI+ (`gp`) on graphics `g`: light grey
    frame rings, a ruler of ticks, a notched gear ring and a glowing core, each layer
    turned by `angle` its own way. `light` is how far a breathing glow reaches beyond
    the frame. `detail` 3 is everything; 2 drops the finest lines and 1 keeps the
    frame, the gear and the core, for small icons."""

    def pen(width, opacity, rgb=color):
        gp.GdipSetPenWidth(pen_, max(1.0, width * k))
        gp.GdipSetPenColor(pen_, _argb(round(opacity * 255), rgb))

    def fill(r, opacity, rgb=color):
        gp.GdipSetSolidFillColor(brush, _argb(round(opacity * 255), rgb))
        gp.GdipFillEllipse(g, brush, middle - r, middle - r, r * 2, r * 2)

    def circle(r):
        gp.GdipDrawEllipse(g, pen_, middle - r, middle - r, r * 2, r * 2)

    def arcs(r, count, sweep, turn):
        for n in range(count):
            gp.GdipDrawArc(g, pen_, middle - r, middle - r, r * 2, r * 2, turn + n * 360 / count, sweep)

    def dashed(r, width, opacity, count, share, turn, rgb=color):
        """`count` even marks around a ring, as one dashed stroke: a wide pen makes
        each dash a radial block. One call instead of one per mark."""
        width = max(1.0, width * k)
        period = 2 * math.pi * r / count
        dashes = (ctypes.c_float * 2)(period * share / width, period * (1 - share) / width)
        gp.GdipSetPenWidth(pen_, width)
        gp.GdipSetPenColor(pen_, _argb(round(opacity * 255), rgb))
        gp.GdipSetPenDashArray(pen_, dashes, 2)
        gp.GdipDrawArc(g, pen_, middle - r, middle - r, r * 2, r * 2, turn, 360.0)
        gp.GdipSetPenDashStyle(pen_, 0)   # solid again

    if light > 0:   # a soft light behind it, breathing
        reach = light * (0.45 + 0.55 * glow)
        for step in range(6, 0, -1):
            fill(radius * 0.55 + (radius * 0.45 + reach) * step / 6, (11 + 11 * glow) / 255)
    pen(1.1 if detail > 1 else 1.4, 0.85, FRAME)
    circle(radius)                                       # the outer frame
    if detail > 2:
        pen(0.8, 0.45, FRAME)
        circle(radius * 0.93)
        dashed(radius * 0.825, radius * 0.07 / k, 0.75, 48, 0.28, angle * 0.5)   # a ruler of fine ticks
    if detail > 1:
        dashed(radius * 0.66, 2.8, 0.9, 8, 30 / 45, -angle * 0.8)        # the notched gear ring
    else:
        dashed(radius * 0.66, 3.4, 0.9, 4, 55 / 90, -angle * 0.8)
    if detail > 2:
        pen(0.8, 0.5)
        circle(radius * 0.53)
    if detail > 1:
        pen(1.6, 0.95)
        arcs(radius * 0.42, 3, 70.0, angle * 1.6)        # three quick arcs
    core = radius * (0.27 + 0.02 * glow)
    fill(core, (40 + 50 * glow) / 255)
    if detail > 1:
        pen(1.8, 1.0, _lighter(color))
        circle(core)                                     # the bright double ring of the core
        pen(1.2, 0.9, _lighter(color))
        circle(core * 0.64)
    fill(core * (0.34 if detail > 1 else 0.6), 1.0, _lighter(color, 140))

def _lighter(color, amount=80):
    return tuple(min(255, c + amount) for c in color)


def _argb(alpha, color):
    return (alpha << 24) | (color[0] << 16) | (color[1] << 8) | color[2]
