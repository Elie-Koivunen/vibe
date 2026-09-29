"""Startup sequence per spec #178: settings, DB, UI, VLC discovery."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import QApplication

from bookmark_studio import __version__, platform_support
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

_PLAYLIST_PATTERNS = ["*.m3u", "*.m3u8"]
_MEDIA_PATTERNS = [
    "*.mp3", "*.wav", "*.flac", "*.m4a", "*.aac", "*.ogg", "*.opus", "*.wma",
    "*.mp4", "*.mkv", "*.avi", "*.mov", "*.webm",
]


def _both_cases(patterns: list[str]) -> str:
    # Qt's own (non-native) file dialog on Linux matches name filters case-sensitively,
    # so "*.mp3" alone hid SONG.MP3.
    return " ".join(patterns + [p.upper() for p in patterns])


STARTUP_MEDIA_FILTER = (
    f"Playlists ({_both_cases(_PLAYLIST_PATTERNS)});;"
    f"Media files ({_both_cases(_MEDIA_PATTERNS)});;"
    "All files (*)"
)


def default_data_dir() -> Path:
    """Windows: %LOCALAPPDATA%\\VLCBookmarkStudio\\data; Linux/WSL:
    ~/.local/share/VLCBookmarkStudio/data (honouring $XDG_DATA_HOME)."""
    return platform_support.user_data_dir() / "data"


def default_database_path() -> Path:
    return default_data_dir() / "bookmarkstudio.db"


def find_vlc_path(settings: SettingsService) -> str | None:
    """spec #178 'discover VLC': a saved path first, then the platform's usual install
    locations (see platform_support.vlc_candidates)."""
    return platform_support.find_vlc(settings.vlc_path())


def find_ffmpeg_path(settings: SettingsService) -> str | None:
    return platform_support.find_ffmpeg(settings.ffmpeg_path())


def probe_bridge(host: str, port: int, password: str, *, timeout_s: float = 1.0) -> bool:
    """spec #178 'probe enhanced bridge': True if VLC's built-in HTTP interface answers.

    Despite the name (kept for continuity with spec #178's terminology), this probes
    VLC's built-in HTTP interface, not the custom Lua bridge -- see vlc_launcher.py's
    module docstring for why that's the reliable default now.
    """
    adapter = StandardHttpPlaybackAdapter(host, port, password, timeout_scale=max(0.1, timeout_s / 0.5))
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
    BookmarkRepository(conn).rename_legacy_default_names()
    return conn


def build_main_window(bookmark_repository: BookmarkRepository) -> MainWindow:
    return MainWindow(bookmark_repository, undo_stack=QUndoStack())


def select_playback_adapter(settings: SettingsService) -> tuple[PlaybackAdapter, str | None]:
    """spec #178 'probe enhanced bridge -> fallback probe -> connected? / offline mode'.

    Returns (adapter, vlc_path): a StandardHttpPlaybackAdapter (VLC's built-in HTTP
    interface, spec #28) whenever VLC is installed -- not the custom Lua bridge, which
    leaks a socket per request (see vlc_launcher.py). Not gated on a one-shot probe:
    VLC's HTTP interface takes a moment to come up, and the polling loop self-heals.
    Mock only when VLC isn't installed at all. (main() currently starts with a Mock and
    lets the launch/attach dialog pick the real instance; this remains for callers that
    want a direct adapter.)
    """
    vlc_path = find_vlc_path(settings)
    if vlc_path is not None:
        _bind_host, connect_host = platform_support.vlc_http_hosts(vlc_path)
        return StandardHttpPlaybackAdapter(connect_host, settings.bridge_port(), settings.bridge_token()), vlc_path
    return MockPlaybackAdapter([]), vlc_path


def main(argv: list[str] | None = None) -> int:
    """Follows spec #178's sequence: settings -> DB -> UI -> VLC discovery -> connect
    or fall back to offline mode (spec #104) -- never crash just because VLC isn't
    running or ffmpeg isn't installed.

    Starts the UI with a placeholder Mock adapter, then immediately runs the same
    launch/attach picker the "Launch VLC..." button uses later.
    """
    configure_logging()
    log = get_logger("APP")
    log.info("VLC Bookmark Studio %s on %s (WSL: %s)", __version__, sys.platform, platform_support.is_wsl())

    qt_app = QApplication(argv if argv is not None else sys.argv)
    qt_app.setApplicationName("VLC Bookmark Studio")
    qt_app.setApplicationVersion(__version__)
    settings = SettingsService()

    conn = open_database()
    vlc_path = find_vlc_path(settings)
    log.info("VLC path: %s", vlc_path)
    ffmpeg_path = find_ffmpeg_path(settings)
    if ffmpeg_path is None:
        log.warning("ffmpeg not found; waveforms will be unavailable until it is installed")
    log.info("ffmpeg path: %s", ffmpeg_path)

    application = Application(
        conn=conn,
        adapter=MockPlaybackAdapter([]),
        ffmpeg_path=ffmpeg_path or "ffmpeg",
        waveform_cache_dir=platform_support.user_data_dir() / "waveforms",
        settings=settings,
        vlc_path=vlc_path,
    )
    # Application.stop() existed but was never called: a VLC this app launched kept
    # running after the app closed, and worker threads were abandoned mid-request.
    qt_app.aboutToQuit.connect(application.stop)
    application.start()
    if vlc_path is not None:
        application.prompt_vlc_launch_dialog()
    else:
        log.info("VLC not found; starting in offline mode")

    try:
        return qt_app.exec()
    finally:
        application.stop()
        settings.sync()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
