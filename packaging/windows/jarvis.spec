"""Run with Windows Python/PyInstaller. Both entry points share one runtime."""

from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

root = Path(SPECPATH).parents[1]
datas = [(str(root / "src/murmur/onnx-cpu.conf"), "murmur")]
binaries = []
hiddenimports = []
for package in (
    "vosk",
    "pocketsphinx",
    "sherpa_onnx",
    "pyaudiowpatch",
    "pycaw",
    "comtypes",
):
    datas += collect_data_files(package)
    binaries += collect_dynamic_libs(package)
    hiddenimports += collect_submodules(
        package, filter=lambda name: ".test" not in name and ".example" not in name
    )
for package in ("pyvda",):
    # pyvda creates COM proxies on import. Discover its modules without executing
    # them in the packaging process (Wine has no real Windows virtual desktops).
    import importlib.util

    folder = Path(importlib.util.find_spec(package).submodule_search_locations[0])
    hiddenimports += [package] + [
        package + "." + str(p.relative_to(folder).with_suffix("")).replace("\\", ".")
        for p in folder.rglob("*.py")
        if p.name != "__init__.py"
    ]
hiddenimports += [
    "win32api",
    "win32con",
    "win32event",
    "win32file",
    "win32gui",
    "win32job",
    "win32pipe",
    "win32process",
    "win32security",
    "win32com.client",
    "win32com.propsys.propsys",
    "win32com.propsys.pscon",
    "pythoncom",
    "pywintypes",
    "wmi",
    "audioop",
    "tkinter",
    "tkinter.ttk",
]
a = Analysis(
    [
        str(root / "packaging/windows/console.py"),
        str(root / "packaging/windows/gui.py"),
    ],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        "gi",
        "murmur.ui",
        "matplotlib",
        "numpy",
        "pip",
        "setuptools",
        "PyInstaller",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
console = EXE(
    pyz,
    [item for item in a.scripts if item[0] != "gui"],
    a.binaries,
    a.datas,
    exclude_binaries=True,
    name="JarvisConsole",
    console=True,
    debug=False,
    strip=False,
    upx=False,
    manifest=str(root / "packaging/windows/Jarvis.manifest"),
)
gui = EXE(
    pyz,
    [item for item in a.scripts if item[0] != "console"],
    a.binaries,
    a.datas,
    exclude_binaries=True,
    name="Jarvis",
    console=False,
    debug=False,
    strip=False,
    upx=False,
    manifest=str(root / "packaging/windows/Jarvis.manifest"),
)
coll = COLLECT(console, gui, a.binaries, a.datas, strip=False, upx=False, name="Jarvis")
