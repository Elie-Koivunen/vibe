# PyInstaller spec for VLC Bookmark Studio. Run through packaging/build.py, which sets
# BM4VLC_ICON and adds libVLC/ffmpeg next to the executable afterwards.
#
# One folder ("onedir"), not one file: starts fast, and libVLC's plugin folder can sit
# next to the executable. Windows gets two launchers sharing the same files:
# bm4vlc.exe (no console window) and bm4vlc-cli.exe (console: --help, --self-test).
import os
import sys
from pathlib import Path

HERE = Path(SPECPATH)
ROOT = HERE.parent
ICON = os.environ.get("BM4VLC_ICON") or None

datas = [
    (str(ROOT / "migrations"), "bookmark_studio/migrations"),
    (str(ROOT / "vlc" / "bookmarkstudio.lua"), "bookmark_studio/vlc"),
]

a = Analysis(
    [str(HERE / "bm4vlc_entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=["vlc"],  # python-vlc is imported lazily (after choosing libVLC)
    excludes=["tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtSql",
              "PySide6.QtTest", "PySide6.QtDBus", "PySide6.QtPdf", "matplotlib", "pytest"],
    noarchive=False,
)
# Qt's software OpenGL fallback (20 MB): the UI is raster-painted widgets, no OpenGL.
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() != "opengl32sw.dll"]
pyz = PYZ(a.pure)

gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="bm4vlc",
    console=sys.platform != "win32",
    icon=ICON,
)
executables = [gui]
if sys.platform == "win32":
    executables.append(EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="bm4vlc-cli",
        console=True,
        icon=ICON,
    ))

COLLECT(*executables, a.binaries, a.datas, name="bm4vlc")
