from __future__ import annotations

import sqlite3
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from bookmark_studio.domain.media import Media
from bookmark_studio.domain.playlist import Playlist
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.ui.main_window import MainWindow
from bookmark_studio.ui.waveform.waveform_item import time_us_to_scene_x


def _build_window(qtbot):
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    media = Media(
        id=uuid4(), canonical_uri="file:///song.mp3", filename="song.mp3", title="Song",
        artist=None, album=None, duration_us=200_000_000, file_size=1, mtime_ns=1, fast_fingerprint="fp",
    )
    MediaRepository(conn).insert(media)
    playlist = Playlist(id=uuid4(), name="Guitar Practice", source_uri=None, is_ad_hoc=True)
    PlaylistRepository(conn).insert(playlist)

    bookmark_repo = BookmarkRepository(conn)
    window = MainWindow(bookmark_repo)
    window.resize(1000, 700)
    qtbot.addWidget(window)
    window.set_context(
        playlist_name=playlist.name, track_name=media.title, playlist_id=playlist.id,
        media_id=media.id, bookmark_count=0, duration_us=media.duration_us,
    )
    window.show()
    return window, bookmark_repo, playlist, media


def test_main_window_constructs_with_correct_breadcrumb(qtbot) -> None:
    window, _repo, playlist, media = _build_window(qtbot)
    assert window._breadcrumb.text() == f"{playlist.name} › {media.title} › 0 bookmarks"


def test_double_click_waveform_creates_bookmark_via_undo_stack(qtbot) -> None:
    window, repo, playlist, media = _build_window(qtbot)
    view = window._waveform_view

    pos = view.mapFromScene(time_us_to_scene_x(5_000_000), 40)
    QTest.mouseDClick(view.viewport(), Qt.LeftButton, pos=pos)

    bookmarks = repo.list_for_playlist_media(playlist.id, media.id)
    assert len(bookmarks) == 1
    assert bookmarks[0].scope.value == "playlist_media"
    assert abs(bookmarks[0].start_us - 5_000_000) < 5_000

    assert window._undo_stack.canUndo()
    window._undo_stack.undo()
    assert repo.list_for_playlist_media(playlist.id, media.id) == []

    window._undo_stack.redo()
    assert len(repo.list_for_playlist_media(playlist.id, media.id)) == 1


def test_bookmark_selection_populates_inspector(qtbot) -> None:
    window, repo, playlist, media = _build_window(qtbot)
    view = window._waveform_view

    pos = view.mapFromScene(time_us_to_scene_x(2_000_000), 40)
    QTest.mouseDClick(view.viewport(), Qt.LeftButton, pos=pos)
    bookmark = repo.list_for_playlist_media(playlist.id, media.id)[0]

    window._on_bookmark_activated(bookmark.id)
    assert window._inspector._name_edit.text() == "New bookmark"


def test_rename_via_inspector_pushes_undo_command(qtbot) -> None:
    window, repo, playlist, media = _build_window(qtbot)
    view = window._waveform_view
    pos = view.mapFromScene(time_us_to_scene_x(1_000_000), 40)
    QTest.mouseDClick(view.viewport(), Qt.LeftButton, pos=pos)
    bookmark = repo.list_for_playlist_media(playlist.id, media.id)[0]
    window._on_bookmark_activated(bookmark.id)

    window._inspector._name_edit.setText("Renamed")
    window._inspector._on_name_committed()

    updated = repo.get(bookmark.id)
    assert updated.name == "Renamed"

    window._undo_stack.undo()
    assert repo.get(bookmark.id).name == "New bookmark"
