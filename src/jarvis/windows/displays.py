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
    return result
