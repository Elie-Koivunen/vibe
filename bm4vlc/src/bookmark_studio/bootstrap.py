"""Startup sequence per spec #178: settings, DB, UI, VLC discovery."""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import QApplication

from bookmark_studio.app.application import Application
from bookmark_studio.logging.setup import configure_logging, get_logger
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.database import connect
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.playback.adapter import PlaybackAdapter
from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.main_window import MainWindow

STARTUP_MEDIA_FILTER = (
    "Playlists (*.m3u *.m3u8);;"
    "Media files (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.wma *.mp4 *.mkv *.avi *.mov *.webm);;"
    "All files (*.*)"
)


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "VLCBookmarkStudio" / "data"


def default_database_path() -> Path:
    return default_data_dir() / "bookmarkstudio.db"


def find_vlc_path(settings: SettingsService) -> str | None:
    """spec #178 'discover VLC': a saved path first, then well-known install locations."""
    saved = settings.vlc_path()
    if saved and Path(saved).exists():
        return saved

    on_path = shutil.which("vlc")
    if on_path:
        return on_path

    for candidate in (
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def probe_bridge(host: str, port: int, password: str, *, timeout_s: float = 1.0) -> bool:
    """spec #178 'probe enhanced bridge': True if VLC's built-in HTTP interface answers.

    Despite the name (kept for continuity with spec #178's terminology), this probes
    VLC's built-in HTTP interface, not the custom Lua bridge -- see vlc_launcher.py's
    module docstring for why that's the reliable default now.
    """
    adapter = StandardHttpPlaybackAdapter(host, port, password)
    try:
        adapter.connect()
        return True
    except Exception:  # noqa: BLE001 - any failure here just means "not available"
        return False
    finally:
        adapter.disconnect()


def open_database(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or default_database_path()
    conn = connect(db_path)
    migrate(conn)
    return conn


def build_main_window(bookmark_repository: BookmarkRepository) -> MainWindow:
    return MainWindow(bookmark_repository, undo_stack=QUndoStack())


def select_playback_adapter(settings: SettingsService) -> tuple[PlaybackAdapter, str | None]:
    """spec #178 'probe enhanced bridge -> fallback probe -> connected? / offline mode'.

    Returns (adapter, vlc_path). Whenever VLC is found at all, this returns a
    StandardHttpPlaybackAdapter (VLC's built-in HTTP interface, spec #28) -- not the
    custom Lua bridge spec #196 named as primary. See vlc_launcher.py's module
    docstring for why: the Lua bridge leaks a socket on every request and degrades a
    real session to fully unresponsive within minutes, confirmed live; the built-in
    interface, also confirmed live (100 requests over ~45s of realistic polling),
    leaks nothing.

    This does NOT gate the choice on a one-shot probe_bridge() success/failure. VLC's
    HTTP interface takes a little while to finish loading after the VLC process
    starts, and Application's own per-tick polling already tolerates a not-yet-
    reachable adapter gracefully (spec #104) -- each failed poll is logged and
    skipped, not fatal -- so it self-heals automatically once the interface comes up.
    Only fall back to Mock when VLC itself isn't installed/found at all, since there
    is then nothing to ever reconnect to.

    probe_bridge() is kept and still used by bootstrap.main() purely for an
    informative startup log line, not for this decision.
    """
    vlc_path = find_vlc_path(settings)
    if vlc_path is not None:
        return StandardHttpPlaybackAdapter("127.0.0.1", settings.bridge_port(), settings.bridge_token()), vlc_path
    return MockPlaybackAdapter([]), vlc_path


def main(argv: list[str] | None = None) -> int:
    """Follows spec #178's sequence: settings -> DB -> UI -> VLC discovery -> connect
    or fall back to offline mode (spec #104) -- never crash just because VLC isn't
    running or the bridge isn't installed.

    Starts the UI with a placeholder Mock adapter, then immediately runs the same
    launch/attach picker the "Launch VLC..." button uses later (Application.
    prompt_vlc_launch_dialog) -- one code path for both the first-run flow and any
    later re-launch, per the user's explicit ask for a dropdown of already-open VLC
    instances as an alternative to browsing for a playlist and launching a fresh one.
    """
    configure_logging()
    log = get_logger("APP")

    qt_app = QApplication(argv if argv is not None else sys.argv)
    settings = SettingsService()

    conn = open_database()
    vlc_path = find_vlc_path(settings)
    log.info("VLC path: %s", vlc_path)

    ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
    application = Application(
        conn=conn,
        adapter=MockPlaybackAdapter([]),
        ffmpeg_path=ffmpeg_path,
        waveform_cache_dir=default_data_dir().parent / "waveforms",
        settings=settings,
        vlc_path=vlc_path,
    )
    application.start()
    if vlc_path is not None:
        application.prompt_vlc_launch_dialog()
    else:
        log.info("VLC not found; starting in offline mode")

    return qt_app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
