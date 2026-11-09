"""Small Win32 ownership/security helpers, loaded only on Windows."""

from __future__ import annotations
from contextlib import contextmanager

_job = None


def release_unused_memory(handle=None):
    """Hand Windows back the pages a process isn't using: this one, or a child by
    its handle. Pages it touches again return at once, with no change in behavior.
    FFmpeg alone goes from about 85 MiB to about 8 while recording."""
    import ctypes

    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.K32EmptyWorkingSet.argtypes = [ctypes.c_void_p]
    kernel.K32EmptyWorkingSet(kernel.GetCurrentProcess() if handle is None else handle)


@contextmanager
def com_context():
    import pythoncom

    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def user_sid():
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def security_attributes(*, inherit=False):
    import pywintypes
    import win32security

    sid = win32security.ConvertSidToStringSid(user_sid())
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:P(A;{'OICI' if inherit else ''};GA;;;{sid})", win32security.SDDL_REVISION_1
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def private_directory(path):
    """Only the current user may read the loopback IPC secret or captured audio."""
    import win32security

    path.mkdir(parents=True, exist_ok=True)
    descriptor = security_attributes(inherit=True).SECURITY_DESCRIPTOR
    win32security.SetFileSecurity(
        str(path),
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        descriptor,
    )


def own_service_children():
    """Inherited jobs close capture/voice children if the service exits or crashes.

    App/UI launches explicitly break away, so closing Jarvis never kills an app
    the user opened through a voice command. Nested jobs work on Windows 10/11.
    """
    global _job
    if _job is not None:
        return
    import win32api
    import win32job

    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation
    )
    info["BasicLimitInformation"]["LimitFlags"] = (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
    )
    win32job.SetInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation, info
    )
    win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    _job = job  # non-inheritable handle, deliberately retained until process exit


class ServiceLock:
    def __init__(self, path):
        import msvcrt

        self.file = path.open("a+b")
        try:
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            raise RuntimeError("Jarvis service is already running") from None

    def close(self):
        self.file.close()
