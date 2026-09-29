"""Builds the packaged app: a portable Windows zip, or a Linux tar.gz plus AppImage.

    python packaging/build.py [--vlc-dir DIR] [--ffmpeg PATH] [--appimage] [--out DIR]

Windows: --vlc-dir is an unpacked 64-bit VLC (the vlc-X.Y.Z-win64.zip from videolan.org);
it is copied to ``vlc/`` next to bm4vlc.exe, so the portable build has both the in-app
player (libVLC) and a VLC window of its own. --ffmpeg is copied to ``ffmpeg/``.

Linux: VLC and ffmpeg come from the system (``sudo apt install vlc ffmpeg``); --appimage
also wraps the build into a single-file AppImage (needs appimagetool on PATH or
APPIMAGETOOL=/path/to/appimagetool).

Every build runs its own ``--self-test`` before it is archived.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from bookmark_studio import __version__  # noqa: E402

IS_WINDOWS = sys.platform == "win32"
APP_NAME = "VLC Bookmark Studio"


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def render_icons(work: Path) -> tuple[Path, Path | None]:
    """icon.svg -> 256 px PNG (Linux/AppImage) and a multi-size .ico (Windows)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])  # noqa: F841 - needed for painting
    renderer = QSvgRenderer(str(HERE / "icon.svg"))
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    png = work / "bm4vlc.png"
    if not image.save(str(png)):
        raise RuntimeError("could not write the PNG icon")
    ico = None
    if IS_WINDOWS:
        ico = work / "bm4vlc.ico"
        if not image.save(str(ico)):
            ico = None
            log("warning: Qt could not write .ico; the executable keeps the default icon")
    return png, ico


def run_pyinstaller(work: Path, icon: Path | None) -> Path:
    env = dict(os.environ)
    if icon is not None:
        env["BM4VLC_ICON"] = str(icon)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(work / "dist"), "--workpath", str(work / "pyi"), str(HERE / "bm4vlc.spec")],
        check=True, env=env, cwd=ROOT,
    )
    return work / "dist" / "bm4vlc"


def add_vlc(app_dir: Path, vlc_dir: Path) -> None:
    from bookmark_studio.playback.libvlc_loader import pe_bits

    library = vlc_dir / ("libvlc.dll" if IS_WINDOWS else "libvlc.so.5")
    if IS_WINDOWS:
        bits = pe_bits(library)
        if bits != 64:
            raise SystemExit(f"{library} is {bits}-bit; the packaged app needs the 64-bit VLC (win64 zip)")
    elif not library.exists():
        raise SystemExit(f"no {library.name} in {vlc_dir}")
    target = app_dir / "vlc"
    log(f"bundling VLC from {vlc_dir}")
    shutil.copytree(vlc_dir, target, dirs_exist_ok=True)
    cache_gen = target / ("vlc-cache-gen.exe" if IS_WINDOWS else "vlc-cache-gen")
    if cache_gen.exists():  # plugins.dat: libVLC starts without scanning ~350 plugins
        subprocess.run([str(cache_gen), str(target / "plugins")], check=False)


def add_ffmpeg(app_dir: Path, ffmpeg: Path) -> None:
    target = app_dir / "ffmpeg"
    target.mkdir(exist_ok=True)
    log(f"bundling ffmpeg from {ffmpeg}")
    shutil.copy2(ffmpeg, target / ("ffmpeg.exe" if IS_WINDOWS else "ffmpeg"))


def add_docs(app_dir: Path) -> None:
    shutil.copy2(ROOT / "README.md", app_dir / "README.md")
    shutil.copy2(ROOT / "CHANGELOG.md", app_dir / "CHANGELOG.md")
    shutil.copy2(HERE / "THIRD-PARTY-NOTICES.txt", app_dir / "THIRD-PARTY-NOTICES.txt")
    license_file = ROOT.parent / "LICENSE"
    if license_file.exists():
        shutil.copy2(license_file, app_dir / "LICENSE.txt")
    if IS_WINDOWS:
        # Portable mode: data, settings and logs stay inside the unpacked folder.
        (app_dir / "bm4vlc-portable.cmd").write_text(
            '@echo off\r\nstart "" "%~dp0bm4vlc.exe" --data-dir "%~dp0data" %*\r\n', encoding="ascii"
        )
    else:
        script = app_dir / "bm4vlc-portable.sh"
        script.write_text(
            '#!/bin/sh\nHERE="$(cd "$(dirname "$0")" && pwd)"\n'
            'exec "$HERE/bm4vlc" --data-dir "$HERE/data" "$@"\n', encoding="ascii"
        )
        script.chmod(0o755)


def self_test(app_dir: Path, require: list[str]) -> None:
    exe = app_dir / ("bm4vlc-cli.exe" if IS_WINDOWS else "bm4vlc")
    with tempfile.TemporaryDirectory(prefix="bm4vlc-build-selftest-") as data:
        args = [str(exe), "--self-test", "--data-dir", data]
        if require:
            args += ["--require", ",".join(require)]
        log("self-test: " + " ".join(args[1:]))
        env = dict(os.environ, BM4VLC_SELFTEST_TRACEBACK="1")
        env.pop("BM4VLC_LIBVLC_DIR", None)  # must find the bundled/system libVLC by itself
        env.pop("QT_QPA_PLATFORM", None)  # test the real platform plugin (headless Linux falls back itself)
        if subprocess.run(args, env=env).returncode != 0:
            raise SystemExit("the packaged app failed its self-test")


def archive_windows(app_dir: Path, out: Path) -> Path:
    path = out / f"bm4vlc-{__version__}-windows-x64.zip"
    log(f"writing {path}")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file in sorted(app_dir.rglob("*")):
            if file.is_file():
                archive.write(file, Path("bm4vlc") / file.relative_to(app_dir))
    return path


def archive_linux(app_dir: Path, out: Path) -> Path:
    path = out / f"bm4vlc-{__version__}-linux-{platform.machine()}.tar.gz"
    log(f"writing {path}")
    with tarfile.open(path, "w:gz") as archive:
        archive.add(app_dir, arcname="bm4vlc")
    return path


def build_appimage(app_dir: Path, icon_png: Path, work: Path, out: Path) -> Path:
    tool = os.environ.get("APPIMAGETOOL") or shutil.which("appimagetool")
    if not tool:
        raise SystemExit("appimagetool not found (set APPIMAGETOOL)")
    appdir = work / "AppDir"
    if appdir.exists():
        shutil.rmtree(appdir)
    shutil.copytree(app_dir, appdir / "usr" / "bin")
    shutil.copy2(icon_png, appdir / "bm4vlc.png")
    (appdir / "bm4vlc.desktop").write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={APP_NAME}\nComment=Visual bookmarks and loops for VLC playlists\n"
        "Exec=bm4vlc %F\nIcon=bm4vlc\nCategories=AudioVideo;Audio;Player;\nTerminal=false\n",
        encoding="utf-8",
    )
    apprun = appdir / "AppRun"
    apprun.write_text(
        '#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\nexec "$HERE/usr/bin/bm4vlc" "$@"\n', encoding="ascii"
    )
    apprun.chmod(0o755)
    path = out / f"bm4vlc-{__version__}-{platform.machine()}.AppImage"
    env = dict(os.environ, ARCH=platform.machine(), APPIMAGE_EXTRACT_AND_RUN="1")
    log(f"writing {path}")
    subprocess.run([tool, "--no-appstream", str(appdir), str(path)], check=True, env=env)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vlc-dir", type=Path, help="VLC folder to bundle (Windows: the 64-bit win64 zip, unpacked)")
    parser.add_argument("--ffmpeg", type=Path, help="ffmpeg executable to bundle")
    parser.add_argument("--appimage", action="store_true", help="Linux: also build an AppImage")
    parser.add_argument("--require", default="",
                        help="tools the self-test must find (default: vlc,libvlc,ffmpeg when bundled)")
    parser.add_argument("--work", type=Path, default=ROOT / "build")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    args.work.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    icon_png, icon_ico = render_icons(args.work)
    app_dir = run_pyinstaller(args.work, icon_ico)
    require = [t for t in args.require.split(",") if t]
    if args.vlc_dir:
        add_vlc(app_dir, args.vlc_dir)
        require = require or ["vlc", "libvlc"]
    if args.ffmpeg:
        add_ffmpeg(app_dir, args.ffmpeg)
        require = list(dict.fromkeys(require + ["ffmpeg"]))
    add_docs(app_dir)
    self_test(app_dir, require)

    outputs = [archive_windows(app_dir, args.out) if IS_WINDOWS else archive_linux(app_dir, args.out)]
    if args.appimage and not IS_WINDOWS:
        outputs.append(build_appimage(app_dir, icon_png, args.work, args.out))
    for path in outputs:
        log(f"done: {path} ({path.stat().st_size / 1_048_576:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
