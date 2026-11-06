"""Win32 window control, Core Audio, and isolated virtual-desktop integration."""

from pathlib import PureWindowsPath
from ..commands import CommandError
from .system import com_context


def process_image(process):
    import ctypes
    from ctypes import wintypes

    query = ctypes.WinDLL("kernel32", use_last_error=True).QueryFullProcessImageNameW
    query.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = wintypes.BOOL
    length = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(length.value)
    if not query(int(process), 0, buffer, ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def window_identity(hwnd):
    import pywintypes
    import win32api
    import win32con
    import win32process
    from win32com.propsys import propsys, pscon

    identities = set()
    with com_context():
        try:
            value = (
                propsys.SHGetPropertyStoreForWindow(hwnd)
                .GetValue(pscon.PKEY_AppUserModel_ID)
                .GetValue()
            )
            if value:
                identities.add(str(value).casefold())
        except Exception:
            pass  # many classic windows have no explicit AUMID property
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        try:
            path = process_image(process)
            identities.add(str(PureWindowsPath(path)).casefold())
        finally:
            process.Close()
    except (OSError, pywintypes.error):
        pass  # an elevated/protected process may deny inspection
    return identities


class Desktop:
    def __init__(self):
        self._fullscreen = {}

    def running(self, app):
        import win32gui

        wanted = app.classes()
        windows = []

        def visit(hwnd, _):
            if (
                win32gui.IsWindowVisible(hwnd)
                and win32gui.GetWindowText(hwnd)
                and window_identity(hwnd) & wanted
            ):
                windows.append({"address": hwnd})
            return True

        win32gui.EnumWindows(visit, None)
        return windows

    def dispatch(self, action, target=None):
        import win32con
        import win32gui

        if action == "close_address":
            if type(target) is not int or target <= 0 or not win32gui.IsWindow(target):
                raise CommandError("The application window is no longer available")
            try:
                win32gui.PostMessage(target, win32con.WM_CLOSE, 0, 0)
            except OSError as exc:
                raise CommandError("Windows refused to close this window") from exc
            return
        if action == "focus_address":
            hwnd = target
            if not win32gui.IsWindow(hwnd):
                raise CommandError(
                    "The application window closed before it could be focused"
                )
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except OSError as exc:
                raise CommandError(
                    "Windows refused to focus this window; elevated apps may need the same privilege level"
                ) from exc
            return
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            raise CommandError("There is no foreground window")
        if action in ("workspace", "move_workspace"):
            if type(target) is not int or not 1 <= target <= 10:
                raise CommandError("Choose workspace one through ten")
            with com_context():
                try:
                    from pyvda import AppView, VirtualDesktop, get_virtual_desktops

                    desktops = get_virtual_desktops()
                    if target > len(desktops):
                        raise CommandError(
                            "Create that desktop in Windows Task View first"
                        )
                    if action == "workspace":
                        VirtualDesktop(target).go()
                    else:
                        AppView(hwnd=hwnd).move(VirtualDesktop(target))
                except CommandError:
                    raise
                except Exception as exc:
                    raise CommandError(
                        "Virtual desktops are unavailable on this Windows build; check Jarvis diagnostics"
                    ) from exc
            return
        if action == "fullscreen":
            if hwnd in self._fullscreen:
                return
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            placement = win32gui.GetWindowPlacement(hwnd)
            from win32api import GetMonitorInfo, MonitorFromWindow

            left, top, right, bottom = GetMonitorInfo(
                MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
            )["Monitor"]
            self._fullscreen[hwnd] = (style, placement)
            try:
                win32gui.SetWindowLong(
                    hwnd, win32con.GWL_STYLE, style & ~win32con.WS_OVERLAPPEDWINDOW
                )
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_TOP,
                    left,
                    top,
                    right - left,
                    bottom - top,
                    win32con.SWP_FRAMECHANGED | win32con.SWP_NOOWNERZORDER,
                )
            except OSError:
                self._fullscreen.pop(hwnd, None)
                raise
            return
        if action == "unfullscreen":
            saved = self._fullscreen.pop(hwnd, None)
            if saved is None:
                raise CommandError(
                    "Jarvis can restore fullscreen windows that it made fullscreen"
                )
            style, placement = saved
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
            win32gui.SetWindowPlacement(hwnd, placement)
            win32gui.SetWindowPos(
                hwnd,
                0,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE
                | win32con.SWP_NOSIZE
                | win32con.SWP_NOZORDER
                | win32con.SWP_FRAMECHANGED,
            )
            return
        raise CommandError("Unsupported Windows desktop action")


def volume(action):
    with com_context():
        from pycaw.pycaw import AudioUtilities

        endpoint = AudioUtilities.GetSpeakers().EndpointVolume
        if action in ("volume_mute", "volume_unmute"):
            endpoint.SetMute(action == "volume_mute", None)
        elif action in ("volume_up", "volume_down"):
            delta = 0.05 if action == "volume_up" else -0.05
            endpoint.SetMasterVolumeLevelScalar(
                max(0, min(1, endpoint.GetMasterVolumeLevelScalar() + delta)), None
            )
        else:
            raise CommandError("Unsupported volume action")
