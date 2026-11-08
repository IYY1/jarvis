"""DXGI monitor identity, adapter/output selection and active HDR color space.

Display enumeration does not capture pixels. Using DXGI's own DeviceName avoids
assuming that DISPLAY1 or EnumDisplayMonitors order is FFmpeg's output index.
"""

from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import uuid


class GUID(C.Structure):
    _fields_ = [("data", C.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__((C.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


class OutputDesc(C.Structure):
    _fields_ = [
        ("name", W.WCHAR * 32),
        ("rect", W.RECT),
        ("attached", W.BOOL),
        ("rotation", C.c_uint),
        ("monitor", W.HMONITOR),
    ]


class OutputDesc1(C.Structure):
    _fields_ = OutputDesc._fields_ + [
        ("bits", C.c_uint),
        ("color_space", C.c_uint),
        ("red", C.c_float * 2),
        ("green", C.c_float * 2),
        ("blue", C.c_float * 2),
        ("white", C.c_float * 2),
        ("min_luminance", C.c_float),
        ("max_luminance", C.c_float),
        ("max_full_luminance", C.c_float),
    ]


class AdapterDesc1(C.Structure):
    _fields_ = [
        ("description", W.WCHAR * 128),
        ("vendor", C.c_uint),
        ("device", C.c_uint),
        ("subsystem", C.c_uint),
        ("revision", C.c_uint),
        ("video_memory", C.c_size_t),
        ("system_memory", C.c_size_t),
        ("shared_memory", C.c_size_t),
        ("luid_low", C.c_ulong),
        ("luid_high", C.c_long),
        ("flags", C.c_uint),
    ]


def method(obj, slot, restype, *types):
    table = C.cast(obj, C.POINTER(C.POINTER(C.c_void_p))).contents
    return C.WINFUNCTYPE(restype, C.c_void_p, *types)(table[slot])


def release(obj):
    if obj:
        method(obj, 2, C.c_ulong)(obj)


def refresh_rate(name):
    """The display's current refresh rate in Hz, or 0 when Windows won't say."""
    try:
        import win32api

        return int(win32api.EnumDisplaySettings(name, -1).DisplayFrequency)
    except Exception:
        return 0


class _Header(C.Structure):
    _fields_ = [("type", C.c_uint32), ("size", C.c_uint32), ("adapter_low", C.c_uint32),
                ("adapter_high", C.c_int32), ("id", C.c_uint32)]


class _SourceName(C.Structure):
    _fields_ = [("header", _Header), ("gdi_name", W.WCHAR * 32)]


class _TargetName(C.Structure):
    _fields_ = [("header", _Header), ("flags", C.c_uint32), ("technology", C.c_uint32),
                ("manufacturer", C.c_uint16), ("product", C.c_uint16), ("connector", C.c_uint32),
                ("friendly", W.WCHAR * 64), ("path", W.WCHAR * 128)]


class _PathSource(C.Structure):
    _fields_ = [("adapter_low", C.c_uint32), ("adapter_high", C.c_int32), ("id", C.c_uint32),
                ("mode", C.c_uint32), ("flags", C.c_uint32)]


class _PathTarget(C.Structure):
    _fields_ = [("adapter_low", C.c_uint32), ("adapter_high", C.c_int32), ("id", C.c_uint32),
                ("mode", C.c_uint32), ("technology", C.c_uint32), ("rotation", C.c_uint32),
                ("scaling", C.c_uint32), ("refresh_num", C.c_uint32), ("refresh_den", C.c_uint32),
                ("scanline", C.c_uint32), ("available", W.BOOL), ("flags", C.c_uint32)]


class _Path(C.Structure):
    _fields_ = [("source", _PathSource), ("target", _PathTarget), ("flags", C.c_uint32)]


MAKERS = {"AUS": "ASUS", "SAM": "Samsung", "GSM": "LG", "DEL": "Dell", "ACR": "Acer", "BNQ": "BenQ", "AOC": "AOC",
          "MSI": "MSI", "GBT": "Gigabyte", "LEN": "Lenovo", "HPN": "HP", "HWP": "HP", "PHL": "Philips",
          "VSC": "ViewSonic", "SNY": "Sony", "XMI": "Xiaomi", "AAC": "AOC", "IVM": "iiyama", "NEC": "NEC",
          "ALI": "Alienware", "CMN": "Innolux", "BOE": "BOE", "SHP": "Sharp", "HSD": "HannStar", "KOS": "KOORUI"}


def maker(code):
    """The maker's name from the display's three-letter EDID id, or "" when unknown."""
    for value in (code, ((code & 0xFF) << 8) | (code >> 8)):   # Windows may hand it over byte-swapped
        letters = "".join(chr(64 + (value >> shift & 31)) for shift in (10, 5, 0))
        if letters in MAKERS:
            return MAKERS[letters]
    return ""


def monitor_names():
    """Each active display's own name ("Odyssey G70B"), by its Windows device
    name (\\\\.\\DISPLAY1). Read once when displays are listed; {} when Windows won't say."""
    names = {}
    try:
        user32 = C.WinDLL("user32")
        paths, modes = C.c_uint32(), C.c_uint32()
        if user32.GetDisplayConfigBufferSizes(2, C.byref(paths), C.byref(modes)):   # QDC_ONLY_ACTIVE_PATHS
            return names
        path_array = (_Path * paths.value)()
        mode_array = (C.c_byte * (64 * max(1, modes.value)))()   # DISPLAYCONFIG_MODE_INFO is 64 bytes
        if user32.QueryDisplayConfig(2, C.byref(paths), path_array, C.byref(modes), mode_array, None):
            return names
        for path in path_array[: paths.value]:
            source = _SourceName()
            source.header = _Header(1, C.sizeof(_SourceName), path.source.adapter_low, path.source.adapter_high,
                                    path.source.id)   # DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
            target = _TargetName()
            target.header = _Header(2, C.sizeof(_TargetName), path.target.adapter_low, path.target.adapter_high,
                                    path.target.id)   # DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
            if user32.DisplayConfigGetDeviceInfo(C.byref(source)) or user32.DisplayConfigGetDeviceInfo(C.byref(target)):
                continue
            model = target.friendly.strip()
            if model:
                brand = maker(target.manufacturer)
                names[source.gdi_name] = f"{brand} {model}" if brand and brand.lower() not in model.lower() else model
    except (OSError, AttributeError, ValueError):
        pass
    return names


def display_label(display, names=None):
    """How a display is shown in a list: its own name, size and refresh rate."""
    own = (names or {}).get(display["name"]) or display.get("description") or display["name"]
    if own == display["name"]:
        own = own.replace("\\\\.\\", "").replace("DISPLAY", "Display ")
    detail = f"{display['width']}×{display['height']}" + (f", {display['refresh']} Hz" if display.get("refresh") else "")
    return f"{own} ({detail})"


def sources():
    # Set before any desktop coordinates are queried, including on scaled and
    # mixed-DPI monitors. This only affects the current Jarvis process.
    user32 = C.WinDLL("user32", use_last_error=True)
    user32.SetProcessDpiAwarenessContext.argtypes = [C.c_void_p]
    user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
    dxgi = C.WinDLL("dxgi", use_last_error=True)
    create = dxgi.CreateDXGIFactory1
    create.argtypes, create.restype = [C.POINTER(GUID), C.POINTER(C.c_void_p)], C.c_long
    factory = C.c_void_p()
    if (
        create(C.byref(GUID("770aae78-f26f-4dba-a829-253c83d1b387")), C.byref(factory))
        < 0
    ):
        raise RuntimeError("Windows DXGI display discovery failed")
    result = []
    try:
        adapter_index = 0
        while True:
            adapter = C.c_void_p()
            if (
                method(factory, 12, C.c_long, C.c_uint, C.POINTER(C.c_void_p))(
                    factory, adapter_index, C.byref(adapter)
                )
                < 0
            ):
                break
            try:
                adapter_desc = AdapterDesc1()
                vendor = 0
                if (
                    method(adapter, 10, C.c_long, C.POINTER(AdapterDesc1))(
                        adapter, C.byref(adapter_desc)
                    )
                    >= 0
                ):
                    vendor = adapter_desc.vendor
                output_index = 0
                while True:
                    output = C.c_void_p()
                    if (
                        method(adapter, 7, C.c_long, C.c_uint, C.POINTER(C.c_void_p))(
                            adapter, output_index, C.byref(output)
                        )
                        < 0
                    ):
                        break
                    try:
                        desc = OutputDesc()
                        hr = method(output, 7, C.c_long, C.POINTER(OutputDesc))(
                            output, C.byref(desc)
                        )
                        if hr >= 0 and desc.attached:
                            newer = C.c_void_p()
                            hdr, hdr_known = False, False
                            if (
                                method(
                                    output,
                                    0,
                                    C.c_long,
                                    C.POINTER(GUID),
                                    C.POINTER(C.c_void_p),
                                )(
                                    output,
                                    C.byref(
                                        GUID("068346e8-aaec-4b84-add7-137f513f77a1")
                                    ),
                                    C.byref(newer),
                                )
                                >= 0
                            ):
                                try:
                                    full = OutputDesc1()
                                    if (
                                        method(
                                            newer, 27, C.c_long, C.POINTER(OutputDesc1)
                                        )(newer, C.byref(full))
                                        >= 0
                                    ):
                                        hdr_known = True
                                        hdr = (
                                            full.color_space == 12
                                        )  # DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020
                                finally:
                                    release(newer)
                            result.append(
                                {
                                    "name": desc.name,
                                    "description": desc.name,
                                    "width": desc.rect.right - desc.rect.left,
                                    "height": desc.rect.bottom - desc.rect.top,
                                    "left": desc.rect.left,
                                    "top": desc.rect.top,
                                    "adapter": adapter_index,
                                    "vendor": vendor,
                                    "output": output_index,
                                    "hdr_known": hdr_known,
                                    "refresh": refresh_rate(desc.name),
                                    "colorManagementPreset": "hdr" if hdr else "srgb",
                                    "focused": len(result) == 0,
                                }
                            )
                    finally:
                        release(output)
                    output_index += 1
            finally:
                release(adapter)
            adapter_index += 1
    finally:
        release(factory)
    names = monitor_names()
    for display in result:
        display["description"] = names.get(display["name"], display["name"])
        display["label"] = display_label(display, names)
    return result
