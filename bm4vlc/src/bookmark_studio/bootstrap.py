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
from bookmark_studio.playback.bridge_client import BridgeClient
from bookmark_studio.playback.enhanced_adapter import EnhancedLuaPlaybackAdapter
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.main_window import MainWindow


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


def probe_bridge(host: str, port: int, token: str, *, timeout_s: float = 1.0) -> bool:
    """spec #178 'probe enhanced bridge': True if bookmarkstudio.lua answers health."""
    client = BridgeClient(host, port, token)
    try:
        health = client.health()
        return health.ok
    except Exception:  # noqa: BLE001 - any failure here just means "not available"
        return False
    finally:
        client.close()


def open_database(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or default_database_path()
    conn = connect(db_path)
    migrate(conn)
    return conn


def build_main_window(bookmark_repository: BookmarkRepository) -> MainWindow:
    return MainWindow(bookmark_repository, undo_stack=QUndoStack())


def select_playback_adapter(settings: SettingsService) -> tuple[PlaybackAdapter, str | None]:
    """spec #178 'probe enhanced bridge -> fallback probe -> connected? / offline mode'.

    Returns (adapter, vlc_path). Falls back to an inert MockPlaybackAdapter([]) when
    nothing is reachable, rather than the standard-HTTP fallback (spec #28) -- that
    adapter needs a known host/port/password the settings UI doesn't collect yet, so
    wiring it in is left as a documented next step rather than guessed at here.
    """
    vlc_path = find_vlc_path(settings)
    if vlc_path is not None:
        reachable = probe_bridge("127.0.0.1", settings.bridge_port(), settings.bridge_token())
        if reachable:
            client = BridgeClient("127.0.0.1", settings.bridge_port(), settings.bridge_token())
            return EnhancedLuaPlaybackAdapter(client), vlc_path
    return MockPlaybackAdapter([]), vlc_path


def main(argv: list[str] | None = None) -> int:
    """Follows spec #178's sequence: settings -> DB -> UI -> VLC discovery -> connect
    or fall back to offline mode (spec #104) -- never crash just because VLC isn't
    running or the bridge isn't installed.
    """
    configure_logging()
    log = get_logger("APP")

    qt_app = QApplication(argv if argv is not None else sys.argv)
    settings = SettingsService()

    conn = open_database()
    adapter, vlc_path = select_playback_adapter(settings)
    log.info("VLC path: %s; adapter: %s", vlc_path, type(adapter).__name__)

    ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
    application = Application(
        conn=conn,
        adapter=adapter,
        ffmpeg_path=ffmpeg_path,
        waveform_cache_dir=default_data_dir().parent / "waveforms",
    )
    application.start()

    return qt_app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
