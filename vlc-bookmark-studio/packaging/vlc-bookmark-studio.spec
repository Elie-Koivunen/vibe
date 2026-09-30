# PyInstaller spec for VLC Bookmark Studio. Run through packaging/build.py, which sets
# VLC_BOOKMARK_STUDIO_ICON and adds libVLC/ffmpeg next to the executable afterwards.
#
# One folder ("onedir"), not one file: starts fast, and libVLC's plugin folder can sit
# next to the executable. Windows gets two launchers sharing the same files:
# VLCBookmarkStudio.exe (no console window) and VLCBookmarkStudio-cli.exe (console:
# --help, --self-test). On Linux the program is vlc-bookmark-studio.
import os
import sys
from pathlib import Path

HERE = Path(SPECPATH)
ROOT = HERE.parent
ICON = os.environ.get("VLC_BOOKMARK_STUDIO_ICON") or None
NAME = "VLCBookmarkStudio" if sys.platform == "win32" else "vlc-bookmark-studio"

datas = [
    (str(ROOT / "migrations"), "bookmark_studio/migrations"),
    (str(ROOT / "vlc" / "bookmarkstudio.lua"), "bookmark_studio/vlc"),
]

a = Analysis(
    [str(HERE / "vlc_bookmark_studio_entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=["vlc"],  # python-vlc is imported lazily (after choosing libVLC)
    excludes=["tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtSql",
              "PySide6.QtTest", "PySide6.QtDBus", "PySide6.QtPdf", "matplotlib", "pytest"],
    noarchive=False,
)
# Qt's software OpenGL fallback (20 MB): the UI is raster-painted widgets, no OpenGL.
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() != "opengl32sw.dll"]
# Never bundle a libVLC found on the build machine (PyInstaller picks it up from
# python-vlc's ctypes calls). VLC looks for its plugins next to wherever libvlccore was
# loaded from, so a lone copy in _internal/ finds none and refuses to start. Linux uses
# the system's VLC; the Windows build ships a complete VLC folder in vlc/ instead.
a.binaries = [
    entry for entry in a.binaries
    if not Path(entry[0]).name.lower().startswith(("libvlc.", "libvlccore."))
]
pyz = PYZ(a.pure)

gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=NAME,
    console=sys.platform != "win32",
    icon=ICON,
)
executables = [gui]
if sys.platform == "win32":
    executables.append(EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=f"{NAME}-cli",
        console=True,
        icon=ICON,
    ))

COLLECT(*executables, a.binaries, a.datas, name=NAME)
