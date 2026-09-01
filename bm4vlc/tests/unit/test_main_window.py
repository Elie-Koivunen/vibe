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
    qtbot.waitActive(window)
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


def test_selection_bar_disabled_until_a_selection_exists(qtbot) -> None:
    window, _repo, _playlist, _media = _build_window(qtbot)
    assert not window._bookmark_selection_button.isEnabled()

    from bookmark_studio.domain.selection import Selection
    window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=2_000_000))
    assert window._bookmark_selection_button.isEnabled()
    assert "Selection:" in window._selection_label.text()

    window._waveform_scene.clear_selection()
    assert not window._bookmark_selection_button.isEnabled()


def test_bookmark_selection_button_creates_segment_bookmark_and_populates_inspector(qtbot) -> None:
    window, repo, playlist, media = _build_window(qtbot)
    from bookmark_studio.domain.selection import Selection

    window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=3_000_000))
    window._bookmark_selection_button.click()

    bookmarks = repo.list_for_playlist_media(playlist.id, media.id)
    assert len(bookmarks) == 1
    assert bookmarks[0].bookmark_type.value == "segment"
    assert (bookmarks[0].start_us, bookmarks[0].end_us) == (1_000_000, 3_000_000)

    # spec #46: inline name editor is loaded and its text pre-selected for immediate
    # rename, no modal dialog. setFocus()/hasFocus() are not asserted here -- real
    # keyboard focus depends on window-manager-level "active window" state that the
    # offscreen Qt platform used for headless testing does not fully provide, so
    # hasFocus() is unreliable in this environment regardless of correct app behavior.
    assert window._inspector.current_bookmark().id == bookmarks[0].id
    assert window._inspector._name_edit.selectedText() == "New bookmark"

    # Selection is cleared after committing it as a bookmark.
    assert window._waveform_scene.selection() is None


def test_ctrl_b_menu_action_creates_bookmark_from_selection(qtbot) -> None:
    """Exercises the same handler a real Ctrl+B keypress triggers. Not simulated via a
    raw QTest key sequence: Qt's WindowShortcut context depends on OS-level "active
    window" focus that the offscreen platform used for headless testing doesn't fully
    provide, making raw keystroke simulation unreliable here independent of whether the
    app itself is correct. Triggering the QAction directly verifies the same wiring."""
    window, repo, playlist, media = _build_window(qtbot)
    from bookmark_studio.domain.selection import Selection

    window._waveform_scene.set_selection(Selection(start_us=500_000, end_us=1_500_000))
    menu_bar = window.menuBar()
    bookmark_menu = next(a.menu() for a in menu_bar.actions() if a.text() == "Bookmark")
    bookmark_selection_action = next(a for a in bookmark_menu.actions() if a.text() == "Bookmark Selection")
    assert bookmark_selection_action.shortcut().toString() == "Ctrl+B"
    bookmark_selection_action.trigger()

    bookmarks = repo.list_for_playlist_media(playlist.id, media.id)
    assert len(bookmarks) == 1
    assert (bookmarks[0].start_us, bookmarks[0].end_us) == (500_000, 1_500_000)


def test_delete_shortcut_removes_selected_bookmark(qtbot) -> None:
    window, repo, playlist, media = _build_window(qtbot)
    view = window._waveform_view
    pos = view.mapFromScene(time_us_to_scene_x(1_000_000), 40)
    QTest.mouseDClick(view.viewport(), Qt.LeftButton, pos=pos)
    bookmark = repo.list_for_playlist_media(playlist.id, media.id)[0]

    window._on_delete_shortcut()
    assert repo.get(bookmark.id) is None

    window._undo_stack.undo()
    assert repo.get(bookmark.id) is not None


def test_menu_bar_has_real_actions_not_empty_stubs(qtbot) -> None:
    """Regression: an earlier version added empty QMenus with zero actions except
    Edit's undo/redo -- clicking File/View/Bookmark/Playback/etc. did nothing."""
    window, _repo, _playlist, _media = _build_window(qtbot)
    menu_bar = window.menuBar()
    menus_by_title = {action.text(): action.menu() for action in menu_bar.actions()}

    assert len(menus_by_title["File"].actions()) >= 3
    assert len(menus_by_title["View"].actions()) >= 3
    assert len(menus_by_title["Bookmark"].actions()) >= 4
    assert len(menus_by_title["Playback"].actions()) >= 4
    assert len(menus_by_title["Tools"].actions()) >= 1
    assert len(menus_by_title["Help"].actions()) >= 1


def test_playback_menu_play_pause_action_reaches_transport_signal(qtbot) -> None:
    window, _repo, _playlist, _media = _build_window(qtbot)
    received = []
    window._transport.play_pause_clicked.connect(lambda: received.append(True))

    menu_bar = window.menuBar()
    playback_menu = next(a.menu() for a in menu_bar.actions() if a.text() == "Playback")
    play_pause_action = next(a for a in playback_menu.actions() if "Play" in a.text())
    play_pause_action.trigger()

    assert received == [True]


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
