"""Run with Windows Python/PyInstaller. Both entry points share one runtime."""

from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

root = Path(SPECPATH).parents[1]
# Speech runs in the bundled Rust worker (tools/jarvis-voice.exe); the Python
# service and settings never import an inference library.
# The shared Arabic parser reads this table at runtime, including in the
# frozen Windows service; Python module discovery cannot collect JSON.
datas = [(str(root / "src/jarvis/arabic_commands.json"), "jarvis"),
         (str(root / "src/jarvis/windows/jarvis.ico"), "jarvis/windows")]
binaries = []
hiddenimports = []
for package in (
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
        "jarvis.ui",
        "jarvis.speech",
        "jarvis.transcriber",
        "jarvis.vosk_engine",
        "vosk",
        "pocketsphinx",
        "sherpa_onnx",
        "srt",
        "tqdm",
        "websockets",
        "requests",
        "cffi",
        "matplotlib",
        "numpy",
        "pip",
        "setuptools",
        "PyInstaller",
    ],
    noarchive=False,
)
# build_windows.py records licenses and versions only for what is bundled.
import json

(Path(workpath) / "bundled-modules.json").write_text(json.dumps(sorted(
    {name.split(".")[0] for name, *_ in a.pure} | {dest.replace("\\", "/").split("/")[0] for dest, *_ in a.binaries}
)))
pyz = PYZ(a.pure)


def version_file(description, filename):
    """Windows shows FileDescription, not the file name, in Task Manager and in
    file properties: "Jarvis" reads better than "JarvisConsole"."""
    import re
    version = re.search(r'__version__ = "([^"]+)"', (root / "src/jarvis/__init__.py").read_text(encoding="utf-8"))[1]
    parts = (tuple(int(p) for p in version.split(".")) + (0, 0, 0, 0))[:4]
    path = Path(workpath) / f"version-{filename}.txt"
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={parts}, prodvers={parts}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Jarvis'),
      StringStruct('FileDescription', '{description}'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{filename}'),
      StringStruct('OriginalFilename', '{filename}.exe'),
      StringStruct('ProductName', 'Jarvis'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)""", encoding="utf-8")
    return str(path)


console = EXE(
    pyz,
    [item for item in a.scripts if item[0] != "gui"],
    a.binaries,
    a.datas,
    exclude_binaries=True,
    name="JarvisConsole",
    console=True,
    icon=str(root / "src/jarvis/windows/jarvis.ico"),
    version=version_file("Jarvis", "JarvisConsole"),
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
    # The same file the window loads (look.ICON), from the user's picture.
    icon=str(root / "src/jarvis/windows/jarvis.ico"),
    version=version_file("Jarvis", "Jarvis"),
    debug=False,
    strip=False,
    upx=False,
    manifest=str(root / "packaging/windows/Jarvis.manifest"),
)
coll = COLLECT(console, gui, a.binaries, a.datas, strip=False, upx=False, name="Jarvis")
