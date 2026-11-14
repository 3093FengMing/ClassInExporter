# -*- mode: python ; coding: utf-8 -*-
"""Build the Windows portable directory; include package data explicitly only."""
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)

build_source_directory = Path(SPECPATH).resolve()
project_root = build_source_directory.parent
# Restrict DLL discovery to this Python environment and Windows. An unrelated
# Poppler/Conda directory on PATH can otherwise supply an incompatible ICU DLL
# under the same filename as Windows' ICU, preventing QtCore from loading.
system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
if not system_root:
    raise RuntimeError("The Windows system directory is not configured.")
windows_directory = Path(system_root)
dll_search_directories = [
    Path(sys.prefix) / "Scripts", Path(sys.base_prefix),
    Path(sys.base_prefix) / "DLLs", windows_directory / "System32",
    windows_directory,
]
os.environ["PATH"] = os.pathsep.join(
    str(directory) for directory in dll_search_directories if directory.is_dir()
)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=(0, 4, 1, 0), prodvers=(0, 4, 1, 0),
        mask=0x3F, flags=0, OS=0x40004, fileType=0x1, subtype=0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("FileDescription", "ClassInExporter"),
                StringStruct("FileVersion", "0.4.1"),
                StringStruct("InternalName", "ClassInExporter"),
                StringStruct("OriginalFilename", "ClassInExporter.exe"),
                StringStruct("ProductName", "ClassInExporter"),
                StringStruct("ProductVersion", "0.4.1"),
            ]),
        ]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

# PySide6, cryptography and requests use their maintained PyInstaller hooks.
# capstone opens its native DLL through ctypes; imageio_ffmpeg finds its
# executable relative to its installed package, including in a frozen bundle.
binaries = collect_dynamic_libs("capstone")
datas = collect_data_files("certifi") + collect_data_files(
    "imageio_ffmpeg", includes=["binaries/*"],
)
hiddenimports = [
    "classin_exporter.discovery",
    "classin_exporter.session",
    "classin_exporter.lms_material",
    "classin_exporter.lms_homework",
    "classin_exporter.lms_record",
    "imageio_ffmpeg",
    "capstone",
    "pefile",
    "cryptography.hazmat.primitives.ciphers",
    "cryptography.hazmat.primitives.padding",
]

a = Analysis(
    [str(build_source_directory / "launcher.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "unittest", "tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ClassInExporter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    version=version_info,
    contents_directory="_internal",
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False,
    upx=False,
    name="ClassInExporter",
)
