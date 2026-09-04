from __future__ import annotations

import re
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
from bookmark_studio.ui.transport import format_timecode
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


def test_bookmark_now_button_creates_point_bookmark_without_a_selection(qtbot) -> None:
    """Direct user request: "a button to explicitly bookmark" -- must work with no
    selection made first (drag-selection was itself broken by a separate bug, see
    scene.py's handle_empty_drag), so this is the always-available fallback."""
    window, repo, playlist, media = _build_window(qtbot)
    assert window._waveform_scene.selection() is None

    window._bookmark_now_button.click()

    bookmarks = repo.list_for_playlist_media(playlist.id, media.id)
    assert len(bookmarks) == 1
    assert bookmarks[0].end_us is None  # a point bookmark, not a segment


def test_bookmark_now_button_bookmarks_the_selection_when_one_exists(qtbot) -> None:
    """Regression, reported live as "the bookmarking seems to have changed": with a
    region highlighted, clicking "Bookmark Now" created a point bookmark at the
    playhead and silently ignored the selection -- confusing, not a real regression,
    since that was always its only behavior. It must now prefer the selection.
    """
    from bookmark_studio.domain.selection import Selection

    window, repo, playlist, media = _build_window(qtbot)
    window._waveform_scene.set_selection(Selection(start_us=5_000_000, end_us=9_000_000))

    window._bookmark_now_button.click()

    bookmarks = repo.list_for_playlist_media(playlist.id, media.id)
    assert len(bookmarks) == 1
    assert (bookmarks[0].start_us, bookmarks[0].end_us) == (5_000_000, 9_000_000)  # a segment, not a point
    assert window._waveform_scene.selection() is None  # cleared after committing, like Bookmark Selection


def test_bookmark_panel_play_loop_buttons_track_selection(qtbot) -> None:
    """Direct user request: dedicated controls "that would play explicitly from the
    bookmark listing itself", separate from the waveform's own Play button above."""
    from bookmark_studio.domain.bookmark import Bookmark
    from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction

    window, repo, playlist, media = _build_window(qtbot)
    segment = Bookmark(
        id=uuid4(), playlist_id=playlist.id, media_id=media.id, scope=BookmarkScope.PLAYLIST_MEDIA,
        lane_id=None, bookmark_type=BookmarkType.SEGMENT, name="Chorus", start_us=1_000_000,
        end_us=2_000_000, loop_enabled=False, repeat_count=None, loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    point = Bookmark(
        id=uuid4(), playlist_id=playlist.id, media_id=media.id, scope=BookmarkScope.PLAYLIST_MEDIA,
        lane_id=None, bookmark_type=BookmarkType.POINT, name="Intro", start_us=500_000,
        end_us=None, loop_enabled=False, repeat_count=None, loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    window.load_all_bookmarks([segment, point], {media.id: media.title})
    panel = window._bookmark_panel
    assert panel._play_bookmark_button.isEnabled() is False  # nothing selected yet

    panel.select_bookmark(segment.id)
    assert panel._play_bookmark_button.isEnabled() is True
    assert panel._loop_bookmark_button.isEnabled() is True  # has an end_us

    play_requests = []
    loop_requests = []
    window.play_bookmark_requested.connect(play_requests.append)
    window.loop_bookmark_requested.connect(loop_requests.append)
    panel._play_bookmark_button.click()
    panel._loop_bookmark_button.click()
    assert play_requests == [segment.id]
    assert loop_requests == [segment.id]

    panel.select_bookmark(point.id)
    assert panel._play_bookmark_button.isEnabled() is True
    assert panel._loop_bookmark_button.isEnabled() is False  # a point has nothing to loop


def test_selection_label_tracks_the_selection(qtbot) -> None:
    """The editable Start/End fields that used to sit next to this label were
    removed per direct follow-up request -- "these do not serve a purpose" -- so
    this just confirms the label itself (and the selection-dependent buttons) still
    track the waveform selection correctly."""
    from bookmark_studio.domain.selection import Selection

    window, _repo, _playlist, _media = _build_window(qtbot)
    assert window._selection_label.text() == "No selection"
    assert window._bookmark_selection_button.isEnabled() is False

    window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=2_000_000))
    assert "Selection" in window._selection_label.text()
    assert window._bookmark_selection_button.isEnabled() is True

    window._waveform_scene.clear_selection()
    assert window._selection_label.text() == "No selection"
    assert window._bookmark_selection_button.isEnabled() is False


def test_delete_bookmark_button_removes_the_selected_bookmark(qtbot) -> None:
    """Direct user request: "there is no button to select and delete a bookmark"."""
    from bookmark_studio.domain.bookmark import Bookmark
    from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction

    window, repo, playlist, media = _build_window(qtbot)
    bookmark = Bookmark(
        id=uuid4(), playlist_id=playlist.id, media_id=media.id, scope=BookmarkScope.PLAYLIST_MEDIA,
        lane_id=None, bookmark_type=BookmarkType.POINT, name="Intro", start_us=1_000_000,
        end_us=None, loop_enabled=False, repeat_count=None, loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    repo.insert(bookmark)
    window.load_all_bookmarks([bookmark], {media.id: media.title})
    panel = window._bookmark_panel
    assert panel._delete_bookmark_button.isEnabled() is False

    panel.select_bookmark(bookmark.id)
    assert panel._delete_bookmark_button.isEnabled() is True

    panel._delete_bookmark_button.click()

    assert repo.get(bookmark.id) is None
    assert window._undo_stack.canUndo()


def test_bookmark_selection_populates_inspector(qtbot) -> None:
    window, repo, playlist, media = _build_window(qtbot)
    view = window._waveform_view

    pos = view.mapFromScene(time_us_to_scene_x(2_000_000), 40)
    QTest.mouseDClick(view.viewport(), Qt.LeftButton, pos=pos)
    bookmark = repo.list_for_playlist_media(playlist.id, media.id)[0]

    requests = []
    window.bookmark_song_display_requested.connect(requests.append)

    window._on_bookmark_activated(bookmark.id)
    assert window._inspector._name_edit.text() == bookmark.name
    # Direct user request: "when i select a bookmarking, i want it to automatically
    # select the song from the playlist above and display its waveform".
    assert requests == [bookmark.id]


def test_selection_bar_disabled_until_a_selection_exists(qtbot) -> None:
    window, _repo, _playlist, _media = _build_window(qtbot)
    assert not window._bookmark_selection_button.isEnabled()

    from bookmark_studio.domain.selection import Selection
    window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=2_000_000))
    assert window._bookmark_selection_button.isEnabled()
    assert "Selection" in window._selection_label.text()

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
    assert window._inspector._name_edit.selectedText() == bookmarks[0].name
    # Direct user request: default name is "bookmark-<date>-<6 alphanumeric chars>",
    # not a flat "New bookmark" that's identical for every bookmark in a session.
    assert re.fullmatch(r"bookmark-\d{8}-[a-z0-9]{6}", bookmarks[0].name)

    # Selection is cleared after committing it as a bookmark.
    assert window._waveform_scene.selection() is None
    # Direct user request: "per default, bookmark loop should be enabled (infinite)".
    assert bookmarks[0].loop_enabled is True
    assert bookmarks[0].repeat_count is None  # None means "forever" throughout this codebase


def test_editing_bookmark_panel_loop_and_fade_columns_persists(qtbot) -> None:
    """Direct follow-up request: "the columns for loop, fade in/out etc, they
    should also be directly editable" -- BookmarkPanel.loop_edited/fade_in_edited/
    fade_out_edited must actually persist to the repository, undo-stack included."""
    from bookmark_studio.domain.selection import Selection
    from bookmark_studio.ui.bookmark_panel import FADE_IN_COLUMN, FADE_OUT_COLUMN, LOOP_COLUMN

    window, repo, playlist, media = _build_window(qtbot)
    window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=3_000_000))
    window._bookmark_selection_button.click()
    bookmark = repo.list_for_playlist_media(playlist.id, media.id)[0]
    # The bookmark LIST panel (distinct from the waveform's own markers) is normally
    # populated by Application in response to bookmarks_changed -- this test builds
    # MainWindow standalone, so populate it directly the same way existing tests in
    # this file do (see load_all_bookmarks usage elsewhere).
    window.load_all_bookmarks([bookmark], {media.id: media.title})

    # Persisting an edit calls MainWindow._refresh_bookmarks(), which only rebuilds
    # the waveform's own markers (bookmarks_changed is what would tell Application
    # to repopulate this list panel, and nothing's listening for it in this
    # standalone-MainWindow test) -- so the same row object stays valid throughout.
    row = window._bookmark_panel._tree.topLevelItem(0)
    row.setText(LOOP_COLUMN, "×5")
    updated = repo.get(bookmark.id)
    assert updated.loop_enabled is True
    assert updated.repeat_count == 5

    row.setText(FADE_IN_COLUMN, "1000 ms")
    assert repo.get(bookmark.id).fade_in_ms == 1000

    row.setText(FADE_OUT_COLUMN, "500 ms")
    updated = repo.get(bookmark.id)
    assert updated.fade_out_ms == 500
    # Earlier edits weren't clobbered by later ones targeting a different field.
    assert updated.loop_enabled is True
    assert updated.repeat_count == 5
    assert updated.fade_in_ms == 1000

    window._undo_stack.undo()
    assert repo.get(bookmark.id).fade_out_ms == 0


def test_inspector_start_end_fields_are_editable_and_persist(qtbot) -> None:
    """Direct user request: "the begin/end time fields should be manually editable
    for refined adjustment" -- the fields existed and emitted commit signals, but
    nothing was connected to them."""
    window, repo, playlist, media = _build_window(qtbot)
    from bookmark_studio.domain.selection import Selection

    window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=3_000_000))
    window._bookmark_selection_button.click()
    bookmark = repo.list_for_playlist_media(playlist.id, media.id)[0]

    window._inspector._start_edit.setText("00:00:00.500")
    window._inspector._on_start_committed()
    assert repo.get(bookmark.id).start_us == 500_000

    window._inspector._end_edit.setText("00:00:04.000")
    window._inspector._on_end_committed()
    assert repo.get(bookmark.id).end_us == 4_000_000

    # An edit that would make start >= end is rejected and the field reverts.
    window._inspector._start_edit.setText("00:00:10.000")
    window._inspector._on_start_committed()
    assert repo.get(bookmark.id).start_us == 500_000


def test_inspector_start_field_commits_on_real_enter_keypress(qtbot) -> None:
    """Same as test_inspector_start_end_fields_are_editable_and_persist but drives
    the field the way a real user does -- select all, type, press Enter -- to prove
    the per-unit spin boxes' editingFinished actually reaches
    _on_inspector_start_committed, not just that the handler is correct when called
    directly. TimecodeEdit is a row of separate HH/MM/SS/mmm spin boxes (direct
    follow-up request: "each time unit has its own control arrows, just as in
    audacity"), so driving it means typing into the specific unit boxes that need
    to change (start is 00:00:01.000, target is 00:00:00.500 -- seconds and millis).
    """
    window, repo, playlist, media = _build_window(qtbot)
    from bookmark_studio.domain.selection import Selection

    window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=3_000_000))
    window._bookmark_selection_button.click()
    bookmark = repo.list_for_playlist_media(playlist.id, media.id)[0]

    edit = window._inspector._start_edit

    edit._seconds.setFocus()
    edit._seconds.selectAll()
    QTest.keyClicks(edit._seconds, "00")

    edit._millis.setFocus()
    edit._millis.selectAll()
    QTest.keyClicks(edit._millis, "500")
    QTest.keyClick(edit._millis, Qt.Key_Return)

    assert repo.get(bookmark.id).start_us == 500_000


def test_dragging_a_bookmark_on_the_waveform_refreshes_the_open_inspector(qtbot) -> None:
    """Direct user request: "if the bookmark is adjusted, it should automatically
    update the bookmark values" -- dragging/resizing a bookmark on the waveform
    already persisted to the database, but the Inspector's Start/End fields (if that
    same bookmark was loaded there) stayed stuck on the pre-drag values."""
    window, repo, playlist, media = _build_window(qtbot)
    from bookmark_studio.domain.selection import Selection

    window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=3_000_000))
    window._bookmark_selection_button.click()
    bookmark = repo.list_for_playlist_media(playlist.id, media.id)[0]
    assert window._inspector.current_bookmark().id == bookmark.id

    window._on_bookmark_resize_finished(bookmark.id, "end", 5_000_000)

    assert repo.get(bookmark.id).end_us == 5_000_000
    assert window._inspector._end_edit.text() == "00:00:05.000"


def test_inspector_start_end_update_after_a_real_drag_created_and_then_resized_bookmark(qtbot) -> None:
    """Direct user report: "these fields are not getting dynamically updated when
    creating a bookmark or adjusting a bookmark" -- drives the exact real gesture
    (mouse-drag a selection on the waveform, click Bookmark Selection, then
    mouse-drag the new bookmark's own left handle) rather than calling the
    handlers directly, in case the real drag path behaves differently from the
    synthetic one other tests already cover.
    """
    window, repo, playlist, media = _build_window(qtbot)
    view = window._waveform_view

    start_pos = view.mapFromScene(time_us_to_scene_x(4_000_000), 40)
    end_pos = view.mapFromScene(time_us_to_scene_x(9_000_000), 40)
    QTest.mousePress(view.viewport(), Qt.LeftButton, pos=start_pos)
    QTest.mouseMove(view.viewport(), pos=end_pos)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=end_pos)
    assert window._waveform_scene.selection() is not None

    window._bookmark_selection_button.click()
    bookmark = repo.list_for_playlist_media(playlist.id, media.id)[0]
    assert window._inspector.current_bookmark().id == bookmark.id
    assert window._inspector._start_edit.text() == "00:00:04.000"
    assert window._inspector._end_edit.text() == "00:00:09.000"

    # Now drag the newly created bookmark's own LEFT handle on the waveform --
    # the real BookmarkRegionItem drag path, not a direct handler call.
    region = window._waveform_scene.bookmark_item(bookmark.id)
    assert region is not None
    handle_scene_x = time_us_to_scene_x(bookmark.start_us) + 2  # a couple px into the left handle
    drag_start = view.mapFromScene(handle_scene_x, 40)
    drag_end = view.mapFromScene(time_us_to_scene_x(bookmark.start_us + 500_000), 40)
    QTest.mousePress(view.viewport(), Qt.LeftButton, pos=drag_start)
    QTest.mouseMove(view.viewport(), pos=drag_end)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=drag_end)

    updated = repo.get(bookmark.id)
    assert updated.start_us > bookmark.start_us  # actually persisted
    assert window._inspector.current_bookmark().id == bookmark.id
    assert window._inspector._start_edit.text() == format_timecode(updated.start_us)


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
    original_name = bookmark.name
    window._on_bookmark_activated(bookmark.id)

    window._inspector._name_edit.setText("Renamed")
    window._inspector._on_name_committed()

    updated = repo.get(bookmark.id)
    assert updated.name == "Renamed"

    window._undo_stack.undo()
    assert repo.get(bookmark.id).name == original_name
