"""Command line and startup sequence (spec #178): settings, DB, UI, VLC discovery.

Run ``vlc-bookmark-studio --help`` for the options."""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import QApplication

from bookmark_studio import __version__, platform_support
from bookmark_studio.app.application import Application
from bookmark_studio.app.vlc_launcher import resolve_startup_media, startup_playlist_source_uri
from bookmark_studio.logging.setup import configure_logging, get_logger
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.database import connect
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.playback.adapter import PlaybackAdapter
from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.dialogs.vlc_launch_dialog import VlcLaunchChoice
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


def _host_port(value: str) -> tuple[str | None, int]:
    host, sep, port = value.rpartition(":")
    try:
        number = int(port)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected [HOST:]PORT, got {value!r}") from None
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range: {number}")
    return (host or None) if sep else None, number


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vlc-bookmark-studio",
        description="VLC Bookmark Studio: visual bookmarks and loops for VLC playlists.",
        epilog=(
            "Examples: vlc-bookmark-studio song.mp3 | vlc-bookmark-studio --playlist mix.m3u --adapter libvlc | "
            "vlc-bookmark-studio --attach 43119 | vlc-bookmark-studio --sync-dir /mnt/c/Users/me/Bookmarks"
        ),
    )
    parser.add_argument("media", nargs="*", metavar="MEDIA",
                        help="media files, or one .m3u/.m3u8 playlist, to open right away")
    parser.add_argument("--playlist", metavar="FILE", help="open this .m3u/.m3u8 playlist")
    parser.add_argument("--adapter", choices=("auto", "http", "libvlc"), default="auto",
                        help="http: a separate VLC window driven over HTTP; libvlc: play inside the app; "
                             "auto (default): the last choice, else a VLC window if VLC is installed")
    parser.add_argument("--attach", metavar="[HOST:]PORT", type=_host_port,
                        help="attach to a VLC already running with the HTTP interface on this port")
    parser.add_argument("--port", type=int, metavar="PORT",
                        help="first HTTP port to try for a VLC this app launches (default: saved setting)")
    parser.add_argument("--vlc", metavar="PATH", help="VLC executable to use (this run only)")
    parser.add_argument("--libvlc-dir", metavar="DIR",
                        help="folder of a VLC whose libVLC the in-app player uses (must match Python's bitness)")
    parser.add_argument("--ffmpeg", metavar="PATH", help="ffmpeg executable for waveforms (this run only)")
    parser.add_argument("--data-dir", metavar="DIR",
                        help="keep the database, waveform cache, logs and settings in DIR (portable mode)")
    parser.add_argument("--sync-dir", metavar="DIR",
                        help="share bookmarks with other installations (Windows/WSL/other PCs) through this "
                             "folder; remembered for later runs")
    parser.add_argument("--no-sync", action="store_true", help="don't sync this run (keeps the saved folder)")
    parser.add_argument("--no-dialog", action="store_true",
                        help="don't show the open-media dialog at startup")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    parser.add_argument("--self-test", action="store_true",
                        help="check Qt, the database, sync, ffmpeg, VLC and libVLC, print a report and exit")
    parser.add_argument("--require", default="", metavar="LIST",
                        help="with --self-test: comma-separated tools that must be present (ffmpeg,vlc,libvlc)")
    parser.add_argument("--self-test-report", metavar="FILE", help="with --self-test: also write a JSON report")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _startup_choice(args: argparse.Namespace, settings: SettingsService, vlc_path: str | None,
                    libvlc_dir: str | None) -> VlcLaunchChoice | None:
    """What the command line asks to open, if anything."""
    from bookmark_studio.playback.libvlc_loader import libvlc_available

    if args.attach is not None:
        host, port = args.attach
        if host is None:
            host = platform_support.vlc_http_hosts(vlc_path)[1] if vlc_path else "127.0.0.1"
        return VlcLaunchChoice(mode="attach", port=port, host=host)
    selected = ([args.playlist] if args.playlist else []) + list(args.media)
    if not selected:
        return None
    media_paths = resolve_startup_media(selected)
    source_uri = startup_playlist_source_uri(selected)
    mode = {"http": "launch", "libvlc": "in_app"}.get(args.adapter)
    if mode is None:  # auto
        in_app_ok = libvlc_available(libvlc_dir)
        if in_app_ok and (settings.playback_backend() == "libvlc" or vlc_path is None):
            mode = "in_app"
        else:
            mode = "launch" if vlc_path is not None else "in_app"
    return VlcLaunchChoice(mode=mode, media_paths=media_paths, source_uri=source_uri)


def main(argv: list[str] | None = None) -> int:
    """Settings -> DB -> UI -> VLC discovery -> the player the command line or the
    open-media dialog picks. Never crashes just because VLC or ffmpeg is missing: the
    app starts without a player (offline mode) instead."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:  # the windowed Windows build has no console
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 - process lifetime
    platform_support.restore_child_library_path()
    argv = list(sys.argv if argv is None else argv)
    args = build_arg_parser().parse_args(argv[1:])

    if args.data_dir:
        # Before anything computes a path: the database, logs, waveforms, VLC config.
        os.environ[platform_support.DATA_DIR_ENV] = str(Path(args.data_dir).expanduser().resolve())

    if args.self_test:
        from bookmark_studio.selftest import run_self_test

        return run_self_test(
            vlc=args.vlc, ffmpeg=args.ffmpeg, libvlc_dir=args.libvlc_dir,
            require=tuple(filter(None, (t.strip() for t in args.require.split(",")))),
            report_path=args.self_test_report,
        )

    configure_logging(level=getattr(logging, args.log_level))
    log = get_logger("APP")
    log.info("VLC Bookmark Studio %s on %s (WSL: %s, packaged: %s)", __version__, sys.platform,
             platform_support.is_wsl(), platform_support.is_frozen())

    from bookmark_studio.ui.branding import APP_NAME, app_icon, set_windows_app_id

    set_windows_app_id()  # before the first window: our icon in the Windows taskbar
    qt_app = QApplication([argv[0]])  # our options are not Qt's
    qt_app.setApplicationName(APP_NAME)
    qt_app.setApplicationVersion(__version__)
    qt_app.setWindowIcon(app_icon())  # every window and dialog, the taskbar, Alt+Tab
    if args.data_dir:  # portable: settings next to the data instead of the registry / ~/.config
        settings = SettingsService(QSettings(str(platform_support.user_data_dir() / "settings.ini"),
                                             QSettings.Format.IniFormat))
    else:
        settings = SettingsService()

    conn = open_database()
    vlc_path = platform_support.find_vlc(args.vlc) if args.vlc else find_vlc_path(settings)
    if args.vlc and vlc_path != args.vlc:
        log.warning("--vlc %s does not exist; using %s", args.vlc, vlc_path)
    log.info("VLC path: %s", vlc_path)
    ffmpeg_path = platform_support.find_ffmpeg(args.ffmpeg) if args.ffmpeg else find_ffmpeg_path(settings)
    if ffmpeg_path is None:
        log.warning("ffmpeg not found; waveforms will be unavailable until it is installed")
    log.info("ffmpeg path: %s", ffmpeg_path)
    libvlc_dir = args.libvlc_dir or settings.libvlc_dir()

    if args.sync_dir:
        settings.set_sync_dir(str(Path(args.sync_dir).expanduser().resolve()))
    sync_service = None
    sync_dir = None if args.no_sync else settings.sync_dir()
    if sync_dir:
        from bookmark_studio.project.sync_service import SyncService

        sync_service = SyncService(conn, sync_dir)
        log.info("Syncing through %s as %s", sync_dir, sync_service.machine_id)

    application = Application(
        conn=conn,
        adapter=MockPlaybackAdapter([]),
        ffmpeg_path=ffmpeg_path or "ffmpeg",
        waveform_cache_dir=platform_support.user_data_dir() / "waveforms",
        settings=settings,
        vlc_path=vlc_path,
        libvlc_dir=libvlc_dir,
        sync_service=sync_service,
        http_port=args.port,
    )
    qt_app.aboutToQuit.connect(application.stop)
    application.start()
    choice = _startup_choice(args, settings, vlc_path, libvlc_dir)
    if choice is not None:
        application.apply_launch_choice(choice)
    elif not args.no_dialog:
        application.prompt_vlc_launch_dialog()

    try:
        return qt_app.exec()
    finally:
        application.stop()
        settings.sync()
        try:  # fold the write-ahead log into the database file: one self-contained file
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            log.exception("WAL checkpoint failed")
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
