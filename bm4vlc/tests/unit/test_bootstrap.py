from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

from unittest.mock import patch

from bookmark_studio.bootstrap import (
    build_main_window, find_vlc_path, open_database, probe_bridge, select_playback_adapter,
)
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.migrations import current_version
from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.settings.settings_service import SettingsService


def test_open_database_creates_file_and_runs_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "bookmarkstudio.db"
    conn = open_database(db_path)
    assert db_path.exists()
    assert current_version(conn) >= 1


def test_find_vlc_path_locates_a_real_install_or_returns_none(tmp_path: Path) -> None:
    """Not asserting VLC IS installed (CI machines won't have it) -- just that this
    never raises and, if it does find something, the path actually exists."""
    settings = SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat))
    result = find_vlc_path(settings)
    if result is not None:
        assert Path(result).exists()


def test_probe_bridge_returns_false_when_nothing_listening() -> None:
    # Port 1 is a reserved/unlikely-bound port; connecting should fail cleanly, not raise.
    assert probe_bridge("127.0.0.1", 1, "irrelevant-token", timeout_s=0.2) is False


def test_select_playback_adapter_falls_back_to_mock_only_when_vlc_not_found(tmp_path: Path) -> None:
    settings = SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat))
    with patch("bookmark_studio.bootstrap.find_vlc_path", return_value=None):
        adapter, vlc_path = select_playback_adapter(settings)
    assert vlc_path is None
    assert isinstance(adapter, MockPlaybackAdapter)


def test_select_playback_adapter_chooses_standard_http_whenever_vlc_is_found_even_if_unreachable(
    tmp_path: Path,
) -> None:
    """Regression: an earlier version gated this on a one-shot probe_bridge() call and
    permanently fell back to Mock (which can never reconnect) if VLC's HTTP interface
    simply hadn't finished loading yet -- confirmed live, this took several real
    seconds after VLC's own process started. Application's polling loop already
    tolerates a not-yet-reachable adapter and self-heals once it comes up; Mock cannot."""
    settings = SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat))
    settings.set_bridge_port(1)  # deliberately unreachable
    with patch("bookmark_studio.bootstrap.find_vlc_path", return_value=r"C:\fake\vlc.exe"):
        adapter, vlc_path = select_playback_adapter(settings)
    assert vlc_path == r"C:\fake\vlc.exe"
    assert isinstance(adapter, StandardHttpPlaybackAdapter)


def test_build_main_window_constructs_without_error(qtbot, tmp_path: Path) -> None:
    conn = open_database(tmp_path / "bookmarkstudio.db")
    repo = BookmarkRepository(conn)
    window = build_main_window(repo)
    qtbot.addWidget(window)
    assert window.windowTitle() == "VLC Bookmark Studio"
