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

    Returns (adapter, vlc_path). Whenever VLC is found at all, this returns the
    EnhancedLuaPlaybackAdapter -- it does NOT gate that choice on a one-shot
    probe_bridge() success/failure the way an earlier version did.

    That earlier version was a real, serious bug, not just a missed optimization:
    VLC's Lua bridge takes several seconds to finish loading after the VLC process
    starts (confirmed live -- a health probe attempted ~3-4s after launch failed, the
    same probe ~7s after launch succeeded). A single failed probe at app startup
    permanently locked the whole session onto an inert MockPlaybackAdapter([]) with an
    empty playlist that can never populate, no waveform, and nothing ever updating --
    which looks indistinguishable from "the app is broken," and there is no reconnect
    path back to a real adapter once Mock is chosen no matter how long VLC keeps
    running afterward. Application's own per-tick polling already tolerates a
    not-yet-reachable (or never-reachable) bridge gracefully (spec #104) -- each failed
    poll is logged and skipped, not fatal -- so it will self-heal automatically the
    moment the bridge actually comes up, which Mock could never do. Only fall back to
    Mock when VLC itself isn't installed/found at all, since there is then nothing to
    ever reconnect to.

    probe_bridge() is kept and still used by bootstrap.main() purely for an informative
    startup log line, not for this decision.
    """
    vlc_path = find_vlc_path(settings)
    if vlc_path is not None:
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
    if vlc_path is not None:
        reachable = probe_bridge("127.0.0.1", settings.bridge_port(), settings.bridge_token())
        log.info(
            "Bridge reachable at startup: %s (a 'no' here is not fatal -- polling "
            "keeps retrying and will connect once VLC's bridge finishes loading)",
            reachable,
        )

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
