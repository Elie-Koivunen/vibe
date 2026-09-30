"""``--self-test``: checks that this installation can actually work, without a window.

Used by the packaged builds (is the bundled libVLC / ffmpeg really loadable?), by CI,
and by users reporting a problem. Each check reports ``ok``, ``missing`` (an optional
tool isn't installed) or ``FAIL``; the exit code is 1 if anything failed or if a tool
named in ``--require`` is missing.
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
import time
import traceback
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, TextIO

from bookmark_studio import __version__, platform_support


@dataclass
class Check:
    name: str
    status: str  # "ok" | "missing" | "FAIL"
    detail: str


def _tone_wav(path: Path, seconds: float = 1.5, rate: int = 22050) -> Path:
    frames = int(seconds * rate)
    data = b"".join(
        struct.pack("<h", int(0.2 * 32767 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(frames)
    )
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(data)
    return path


def _check_environment() -> str:
    from bookmark_studio.playback.libvlc_loader import python_bits

    return (
        f"VLC Bookmark Studio {__version__}, Python {sys.version.split()[0]} {python_bits()}-bit on {sys.platform}"
        f"{' (WSL)' if platform_support.is_wsl() else ''}{' (packaged)' if platform_support.is_frozen() else ''}; "
        f"data: {platform_support.user_data_dir()}"
    )


def _check_qt() -> str:
    # Headless Linux (CI, a WSL shell without WSLg) has no display for the default xcb
    # platform; the offscreen one still proves the Qt libraries and plugins load.
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import __version__ as pyside_version
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication, QWidget

    if QApplication.instance() is None:
        QApplication([sys.argv[0]])  # honours QT_QPA_PLATFORM
    widget = QWidget()
    widget.deleteLater()
    return f"PySide6 {pyside_version}, platform {QGuiApplication.platformName()}"


def _check_database(tmp: Path) -> str:
    from bookmark_studio.persistence.database import connect
    from bookmark_studio.persistence.migrations import migrate

    conn = connect(tmp / "selftest.db")
    try:
        migrate(conn)
        tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()
    return f"SQLite {__import__('sqlite3').sqlite_version}, schema {version}, {tables} tables"


def _check_sync(tmp: Path) -> str:
    """Two databases converge through a shared folder."""
    from uuid import uuid4

    from bookmark_studio.domain.bookmark import Bookmark
    from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
    from bookmark_studio.domain.media import Media
    from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
    from bookmark_studio.persistence.database import connect
    from bookmark_studio.persistence.media_repository import MediaRepository
    from bookmark_studio.persistence.migrations import migrate
    from bookmark_studio.project.sync_service import SyncService

    folder = tmp / "sync"
    conns = []
    for name in ("a", "b"):
        conn = connect(tmp / f"sync-{name}.db")
        migrate(conn)
        conns.append(conn)
    try:
        media = Media(
            id=uuid4(), canonical_uri="file:///selftest.wav", filename="selftest.wav", title=None,
            artist=None, album=None, duration_us=1_500_000, file_size=None, mtime_ns=None,
            fast_fingerprint="selftest-fingerprint",
        )
        MediaRepository(conns[0]).insert(media)
        BookmarkRepository(conns[0]).insert(Bookmark(
            id=uuid4(), playlist_id=None, media_id=media.id, scope=BookmarkScope.GLOBAL_MEDIA, lane_id=None,
            bookmark_type=BookmarkType.POINT, name="selftest", start_us=500_000, end_us=None,
            loop_enabled=False, repeat_count=None, loop_gap_ms=0, completion_action=CompletionAction.CONTINUE,
        ))
        a, b = SyncService(conns[0], folder), SyncService(conns[1], folder)
        a.sync_now()
        if not b.sync_now():
            raise RuntimeError("the second database did not receive the bookmark")
        count = len(BookmarkRepository(conns[1]).list_all())
    finally:
        for conn in conns:
            conn.close()
    return f"2 databases converged through a folder ({count} bookmark)"


def _check_ffmpeg(tmp: Path, ffmpeg: str | None) -> tuple[str, str]:
    from bookmark_studio.waveform.ffmpeg_decoder import stream_media_pcm

    ffmpeg = platform_support.find_ffmpeg(ffmpeg)
    if ffmpeg is None:
        return "missing", "ffmpeg not found (waveforms need it)"
    size = sum(len(chunk) for chunk in stream_media_pcm(ffmpeg, str(_tone_wav(tmp / "ffmpeg.wav"))))
    if size == 0:
        raise RuntimeError(f"{ffmpeg} decoded nothing")
    return "ok", f"{ffmpeg} decoded {size} bytes"


def _check_vlc(vlc: str | None) -> tuple[str, str]:
    vlc = platform_support.find_vlc(vlc)
    if vlc is None:
        return "missing", "VLC not found (the separate-window mode needs it)"
    return "ok", vlc


def _check_libvlc(tmp: Path, libvlc_dir: str | None) -> tuple[str, str]:
    from bookmark_studio.playback.libvlc_loader import (
        LibVlcUnavailable,
        instance_failure_hint,
        load_vlc_module,
        loaded_location,
    )

    try:
        vlc = load_vlc_module(libvlc_dir)
    except LibVlcUnavailable as exc:
        return "missing", str(exc)
    instance = vlc.Instance(["--quiet", "--aout=adummy", "--vout=dummy", "--no-video-title-show"])
    if instance is None:
        raise RuntimeError(f"libVLC loaded but refused to create an instance ({instance_failure_hint()})")
    try:
        player = instance.media_player_new()
        player.set_media(instance.media_new_path(str(_tone_wav(tmp / "libvlc.wav"))))
        player.play()
        deadline = time.monotonic() + 5
        while player.get_length() <= 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        length_ms = player.get_length()
        player.stop()
        player.release()
    finally:
        instance.release()
    if length_ms <= 0:
        raise RuntimeError("libVLC could not play a WAV file (audio plugins missing?)")
    location = loaded_location()
    version = vlc.libvlc_get_version()
    version = version.decode() if isinstance(version, bytes) else str(version)
    return "ok", f"libVLC {version} from {location.source if location else '?'} ({location.library if location else ''}); played {length_ms} ms"


def run_self_test(
    *, vlc: str | None = None, ffmpeg: str | None = None, libvlc_dir: str | None = None,
    require: tuple[str, ...] = (), report_path: str | None = None, out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    checks: list[Check] = []

    def run(name: str, fn: Callable[[], object]) -> None:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
            detail = f"{type(exc).__name__}: {exc}"
            if platform_support.env_setting("SELFTEST_TRACEBACK"):
                detail += "\n" + traceback.format_exc()
            checks.append(Check(name, "FAIL", detail))
            return
        status, detail = result if isinstance(result, tuple) else ("ok", str(result))
        if status == "missing" and name in require:
            status = "FAIL"
            detail = f"required but {detail}"
        checks.append(Check(name, status, detail))

    with tempfile.TemporaryDirectory(prefix="vlc-bookmark-studio-selftest-") as tmp_dir:
        tmp = Path(tmp_dir)
        run("environment", _check_environment)
        run("qt", _check_qt)
        run("database", lambda: _check_database(tmp))
        run("sync", lambda: _check_sync(tmp))
        run("ffmpeg", lambda: _check_ffmpeg(tmp, ffmpeg))
        run("vlc", lambda: _check_vlc(vlc))
        run("libvlc", lambda: _check_libvlc(tmp, libvlc_dir))

    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"{check.name:<{width}}  {check.status:<7}  {check.detail}", file=out)
    failed = [c.name for c in checks if c.status == "FAIL"]
    print("self-test " + ("FAILED: " + ", ".join(failed) if failed else "passed"), file=out)
    if report_path:
        Path(report_path).write_text(
            json.dumps({"version": __version__, "passed": not failed, "checks": [asdict(c) for c in checks]},
                       indent=2),
            encoding="utf-8",
        )
    return 1 if failed else 0


__all__ = ["Check", "run_self_test"]
