"""Windows native counters. Imported only on Windows, only for diagnostics.

PrivateUsage is commit, not private working set. Prefer EX2 on newer Windows;
older builds count non-shareable resident pages with QueryWorkingSet. Failed
or inaccessible counters stay null instead of substituting another metric.
See docs/memory-measurement.md for the Microsoft API references.
"""
import ctypes as c
from ctypes import wintypes as w
import mmap


class Entry(c.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("pid", w.DWORD),
                ("heap", c.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                ("ppid", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                ("name", w.WCHAR * 260)]


class Counters(c.Structure):
    _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [
        (name, c.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
        "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]


class Counters2(c.Structure):
    _fields_ = Counters._fields_ + [("PrivateWorkingSetSize", c.c_size_t),
                                   ("SharedCommitUsage", c.c_ulonglong)]


def kernel():
    dll = c.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE),
        ("Process32FirstW", [w.HANDLE, c.POINTER(Entry)], w.BOOL),
        ("Process32NextW", [w.HANDLE, c.POINTER(Entry)], w.BOOL),
        ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ("CloseHandle", [w.HANDLE], w.BOOL),
        ("K32GetProcessMemoryInfo", [w.HANDLE, c.c_void_p, w.DWORD], w.BOOL),
        ("K32QueryWorkingSet", [w.HANDLE, c.c_void_p, w.DWORD], w.BOOL),
    ):
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result
    return dll


def private_working_set(pid, dll):
    """Count resident, non-shareable pages when EX2 is unavailable.

    QueryWorkingSet needs stronger access than GetProcessMemoryInfo. Denial,
    repeated growth, or a working-set list beyond 16 MiB leaves the metric
    unavailable. The bounded retry loop never changes the target's residency.
    """
    handle = dll.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION | VM_READ
    if not handle:
        return None
    try:
        slots = 4096
        max_slots = 16 * 1024**2 // c.sizeof(c.c_size_t) - 1
        for _ in range(4):
            buffer = (c.c_size_t * (slots + 1))()
            if dll.K32QueryWorkingSet(handle, buffer, c.sizeof(buffer)):
                count = buffer[0]
                if count > slots:
                    return None
                # PSAPI_WORKING_SET_BLOCK: 5 protection bits, 3 share-count
                # bits, then Shared. A shareable page with one owner is still
                # shared memory, not private working set.
                return sum(not (buffer[i] & 0x100) for i in range(1, count + 1)) * mmap.PAGESIZE
            if c.get_last_error() != 24:  # ERROR_BAD_LENGTH
                return None
            slots = max(slots * 2, buffer[0] + buffer[0] // 4 + 256)
            if slots > max_slots:
                return None
        return None
    finally:
        dll.CloseHandle(handle)


def processes():
    dll = kernel()
    handle = dll.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        entry = Entry()
        entry.dwSize = c.sizeof(entry)
        found = dll.Process32FirstW(handle, c.byref(entry))
        rows = {}
        while found:
            rows[entry.pid] = {"ppid": entry.ppid, "name": entry.name}
            found = dll.Process32NextW(handle, c.byref(entry))
        if c.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise c.WinError(c.get_last_error())
        return rows
    finally:
        dll.CloseHandle(handle)


def memory(pid):
    dll = kernel()
    handle = dll.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise c.WinError(c.get_last_error())
    try:
        # A sentinel also catches systems that accept EX2 but fill only EX.
        counters = Counters2()
        counters.cb = c.sizeof(counters)
        counters.PrivateWorkingSetSize = c.c_size_t(-1).value
        if dll.K32GetProcessMemoryInfo(handle, c.byref(counters), counters.cb):
            private = counters.PrivateWorkingSetSize
            unavailable = (private == c.c_size_t(-1).value or private > counters.WorkingSetSize
                           or private == 0 < counters.WorkingSetSize)
            return {"commit_bytes": counters.PrivateUsage,
                    "private_working_set_bytes": private_working_set(pid, dll) if unavailable else private}
        counters = Counters()
        counters.cb = c.sizeof(counters)
        if not dll.K32GetProcessMemoryInfo(handle, c.byref(counters), counters.cb):
            raise c.WinError(c.get_last_error())
        return {"commit_bytes": counters.PrivateUsage,
                "private_working_set_bytes": private_working_set(pid, dll)}
    finally:
        dll.CloseHandle(handle)
