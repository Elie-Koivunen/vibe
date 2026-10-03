"""0.9.0: the After Loop column, the 🔁 among the playback buttons, the volume fader
following fades; from testing the 0.9.0 builds: the orange selected tab, After loop
Next/Previous Bookmark following the bookmark list across songs (and selecting the
bookmark it moved to), new bookmarks at the top of the list, a playing selection in green
that carries on as the bookmark made from it, and Max at 125 %."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QTreeWidget

from bookmark_studio.app.application import MAX_BUTTON_VOLUME, Application
from bookmark_studio.domain.enums import BookmarkType, CompletionAction, LoopState
from bookmark_studio.domain.selection import Selection
from bookmark_studio.persistence.database import connect
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.playback.status import VlcPlaylistItem
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.bookmark_panel import AFTER_LOOP_COLUMN, COLUMNS, BookmarkPanel
from tests.unit.test_050_ui import _bookmark, _make_app
from tests.unit.test_regressions_020 import _segment, _wav


def _ini(tmp_path: Path) -> SettingsService:
    return SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))


# -- the After Loop column --


def test_after_loop_is_a_column_shown_after_loop(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    header = panel._tree.header()
    order = [COLUMNS[header.logicalIndex(v)] for v in range(header.count())]
    assert order.index("After Loop") == order.index("Loop") + 1
    media_id = uuid4()
    bookmark = _segment(media_id, None, completion_action=CompletionAction.STOP)
    panel.set_bookmarks([bookmark], {media_id: "Song"})
    assert panel._tree.topLevelItem(0).text(AFTER_LOOP_COLUMN) == "Stop"


def test_choosing_after_loop_in_the_list_saves_it_with_undo(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    bookmark = _bookmark(app, end_us=3_000_000)
    panel = app.window._bookmark_panel
    panel._tree.topLevelItem(0).setText(AFTER_LOOP_COLUMN, "Next Track")  # as the dropdown does
    assert app._bookmark_repository.get(bookmark.id).completion_action is CompletionAction.NEXT_TRACK
    assert panel.is_flashing(bookmark.id)  # saved: the orange flash
    app.window._undo_stack.undo()
    assert app._bookmark_repository.get(bookmark.id).completion_action is CompletionAction.CONTINUE


def test_a_column_layout_saved_before_after_loop_keeps_its_order(qtbot) -> None:
    """A 0.7.0-0.8.0 layout has 9 columns: Qt appends the new one at the end; it goes next
    to Loop, and the user's own order of the others stays."""
    old = QTreeWidget()
    qtbot.addWidget(old)
    old.setColumnCount(9)
    old.setHeaderLabels(COLUMNS[:9])
    header = old.header()
    header.setSectionsMovable(True)
    header.moveSection(header.visualIndex(COLUMNS.index("Start")), 0)  # the user's move
    state = header.saveState()

    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    assert panel.restore_header_state(state)  # no column count saved: 9
    restored = panel._tree.header()
    order = [COLUMNS[restored.logicalIndex(v)] for v in range(restored.count())]
    assert order[0] == "Start"
    assert order.index("After Loop") == order.index("Loop") + 1


def test_the_column_count_is_saved_with_the_layout(qtbot, tmp_path) -> None:
    settings = _ini(tmp_path)
    app = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: None)
    app.quit()
    assert settings.header_columns("bookmarks") == len(COLUMNS)


# -- 🔁 among the playback buttons --


def test_the_loop_button_loops_the_bookmark_selected_in_the_list(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    bookmark = _bookmark(app, start_us=1_000_000, end_us=3_000_000)
    point = _bookmark(app, start_us=4_000_000, end_us=None, bookmark_type=BookmarkType.POINT)
    loop_button = app.window._transport.loop_bookmark_button
    assert not loop_button.isEnabled()
    app.window._bookmark_panel.select_bookmark(point.id)
    assert not loop_button.isEnabled()  # a point has nothing to loop
    app.window._bookmark_panel.select_bookmark(bookmark.id)
    assert loop_button.isEnabled()
    loop_button.click()
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.PLAYING, timeout=3000)
    assert app._active_loop_bookmark_id == bookmark.id


# -- the volume fader follows fades (reported on 0.8.0) --


def test_the_fader_follows_a_fade_in_and_back_to_the_users_level(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    adapter.set_volume(200)
    qtbot.waitUntil(lambda: app._loop_controller.target_volume == 200, timeout=3000)
    bookmark = _bookmark(app, start_us=1_000_000, end_us=4_000_000, loop_enabled=True, fade_in_ms=600)
    stepped: list[int] = []
    app._loop_controller.volume_stepped.connect(stepped.append)
    shown: list[int] = []
    original = app.window.show_volume

    def record(level: int) -> None:
        shown.append(level)
        original(level)

    app.window.show_volume = record
    app._on_loop_bookmark_requested(bookmark.id)
    # (The user's level stays 200 -- since 0.9.0 a bookmark doesn't raise it; the fade
    # starts from 0.)
    qtbot.waitUntil(lambda: len(stepped) > 2 and stepped[-1] == 200, timeout=4000)
    rise = shown[shown.index(0):] if 0 in shown else []
    assert any(0 < level < 200 for level in rise), shown  # the fader rose step by step from 0
    assert shown[-1] == 200 and app.window.volume_level() == 200  # the user's level after the fade
    assert app._loop_controller.target_volume == 200  # the user's level is the fade's goal


def test_the_fader_follows_a_fade_out(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    bookmark = _bookmark(app, start_us=1_000_000, end_us=1_600_000, loop_enabled=True, repeat_count=1,
                         fade_out_ms=400, completion_action=CompletionAction.PAUSE)
    shown: list[int] = []
    original = app.window.show_volume

    def record(level: int) -> None:
        shown.append(level)
        original(level)

    app.window.show_volume = record
    app._on_loop_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.COMPLETED, timeout=4000)
    target = app._loop_controller.target_volume
    assert any(0 <= level < target for level in shown)  # it went down during the fade-out
    assert shown[-1] == target  # and back to the user's level when the loop ended


# -- the selected tab is orange --


def _tab_color(tabs, index: int):
    bar = tabs.tabBar()
    image = bar.grab().toImage()
    ratio = image.devicePixelRatio()
    rect = bar.tabRect(index)
    # just inside the left edge, clear of the text
    return image.pixelColor(int((rect.left() + 4) * ratio), int(rect.center().y() * ratio))


def _is_orange(color) -> bool:
    return abs(color.red() - 255) <= 8 and abs(color.green() - 159) <= 10 and color.blue() <= 50


def test_the_selected_tab_is_orange(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    tabs = window._side_tabs
    tabs.setCurrentIndex(0)
    assert _is_orange(_tab_color(tabs, 0)) and not _is_orange(_tab_color(tabs, 1))
    tabs.setCurrentIndex(1)
    assert _is_orange(_tab_color(tabs, 1)) and not _is_orange(_tab_color(tabs, 0))
    assert _is_orange(_tab_color(window._bookmark_panel._tabs, 0))  # the list's tab too


# -- After loop: Next / Previous Bookmark follow the list (reported on the 0.9.0 build) --


def _two_song_app(qtbot, tmp_path):
    """Two different songs (_make_app's two files sound the same: to the library, one song)."""
    a = _wav(tmp_path / "a.wav")
    b = _wav(tmp_path / "b.wav", seconds=4.0)
    adapter = MockPlaybackAdapter([VlcPlaylistItem(1, a.resolve().as_uri(), "A", 3.0),
                                   VlcPlaylistItem(2, b.resolve().as_uri(), "B", 4.0)])
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    app = Application(conn=conn, adapter=adapter, ffmpeg_path="/nonexistent", waveform_cache_dir=tmp_path / "wf")
    qtbot.addWidget(app.window)
    app.start()
    qtbot._request.addfinalizer(app.stop)
    qtbot.waitUntil(lambda: len(app.playlists.resolved) == 2
                    and app.playlists.synchronizer.active_playlist_id is not None, timeout=5000)
    (item_a, media_a), (item_b, media_b) = app.playlists.resolved
    assert media_a.id != media_b.id
    return app, item_a, media_a, item_b, media_b


def _playing_bookmark(app):
    playback = app._bookmark_playback
    return playback.bookmark_id if playback is not None else None


def test_next_bookmark_after_the_loop_is_the_next_row_even_in_another_song(qtbot, tmp_path) -> None:
    """The only bookmark of its song, Repeat 2, After loop Next Bookmark: playback went on
    in the same song -- Next Bookmark looked only at that song's bookmarks. It's the next
    row of the list now, whichever song it is in (the list's order, not start times)."""
    app, item_a, media_a, item_b, media_b = _two_song_app(qtbot, tmp_path)
    adapter = app.session.adapter
    looped = _bookmark(app, media_id=media_a.id, name="looped", start_us=1_000_000, end_us=1_200_000,
                       loop_enabled=True, repeat_count=2, completion_action=CompletionAction.NEXT_BOOKMARK)
    after = _bookmark(app, media_id=media_b.id, name="after", start_us=500_000, end_us=900_000)
    app._on_bookmark_reorder_requested([looped.id, after.id])  # the user's order in the list
    app._on_play_bookmark_requested(looped.id)
    qtbot.waitUntil(lambda: _playing_bookmark(app) == after.id, timeout=5000)
    qtbot.waitUntil(lambda: adapter.get_status().current_playlist_item_id == item_b.vlc_id, timeout=3000)
    status = adapter.get_status()
    assert status.state == "playing" and status.time_us >= 500_000


def test_next_bookmark_follows_the_lists_order_within_a_song(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    looped = _bookmark(app, name="looped", start_us=1_000_000, end_us=1_200_000, loop_enabled=True,
                       repeat_count=1, completion_action=CompletionAction.NEXT_BOOKMARK)
    later = _bookmark(app, name="later", start_us=4_000_000, end_us=4_500_000)
    sooner = _bookmark(app, name="sooner", start_us=3_000_000, end_us=3_500_000)
    app._on_bookmark_reorder_requested([looped.id, later.id, sooner.id])
    app._on_play_bookmark_requested(looped.id)
    qtbot.waitUntil(lambda: _playing_bookmark(app) == later.id, timeout=5000)  # not "sooner"


def test_previous_bookmark_after_the_loop_is_the_row_above(qtbot, tmp_path) -> None:
    app, item_a, media_a, item_b, media_b = _two_song_app(qtbot, tmp_path)
    above = _bookmark(app, media_id=media_b.id, name="above", start_us=2_000_000, end_us=2_400_000)
    looped = _bookmark(app, media_id=media_a.id, name="looped", start_us=1_000_000, end_us=1_200_000,
                       loop_enabled=True, repeat_count=1, completion_action=CompletionAction.PREVIOUS_BOOKMARK)
    app._on_bookmark_reorder_requested([above.id, looped.id])
    app._on_play_bookmark_requested(looped.id)
    qtbot.waitUntil(lambda: _playing_bookmark(app) == above.id, timeout=5000)
    qtbot.waitUntil(lambda: app.session.adapter.get_status().current_playlist_item_id == item_b.vlc_id,
                    timeout=3000)


def test_next_bookmark_after_the_last_row_lets_playback_go_on(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    first = _bookmark(app, name="first", start_us=3_000_000, end_us=3_500_000)
    last = _bookmark(app, name="last", start_us=1_000_000, end_us=1_200_000, loop_enabled=True,
                     repeat_count=1, completion_action=CompletionAction.NEXT_BOOKMARK)
    app._on_bookmark_reorder_requested([first.id, last.id])
    app._on_play_bookmark_requested(last.id)
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.COMPLETED, timeout=5000)
    qtbot.wait(300)
    assert _playing_bookmark(app) == last.id  # nothing else was started
    assert app.session.adapter.get_status().state == "playing"


def test_the_tab_shows_the_bookmark_next_bookmark_moved_to(qtbot, tmp_path) -> None:
    """Reported on the 0.9.0 build: after the loop went on to the next bookmark, the
    Bookmark Studio tab still showed the previous one's settings."""
    app = _make_app(qtbot, tmp_path)
    looped = _bookmark(app, name="looped", start_us=1_000_000, end_us=1_200_000, loop_enabled=True,
                       repeat_count=1, completion_action=CompletionAction.NEXT_BOOKMARK)
    after = _bookmark(app, name="after", start_us=4_000_000, end_us=4_400_000, loop_enabled=True,
                      repeat_count=3, completion_action=CompletionAction.STOP)
    app._on_bookmark_reorder_requested([looped.id, after.id])
    panel, inspector = app.window._bookmark_panel, app.window._inspector
    panel.select_bookmark(looped.id)
    assert inspector.current_bookmark().id == looped.id
    app._on_play_bookmark_requested(looped.id)
    qtbot.waitUntil(lambda: _playing_bookmark(app) == after.id, timeout=5000)
    qtbot.waitUntil(lambda: inspector.current_bookmark() is not None
                    and inspector.current_bookmark().id == after.id, timeout=2000)
    assert panel._selected_bookmark_ids() == [after.id]
    assert inspector._repeat_spin.value() == 3  # its own settings
    assert inspector._completion_combo.currentData() == CompletionAction.STOP.value


def test_moving_on_leaves_a_new_bookmark_being_set_up_alone(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    looped = _bookmark(app, name="looped", start_us=1_000_000, end_us=1_200_000, loop_enabled=True,
                       repeat_count=1, completion_action=CompletionAction.NEXT_BOOKMARK)
    after = _bookmark(app, name="after", start_us=4_000_000, end_us=4_400_000)
    app._on_bookmark_reorder_requested([looped.id, after.id])
    app._on_play_bookmark_requested(looped.id)
    app.window._waveform_scene.set_selection(Selection(start_us=2_000_000, end_us=3_000_000))
    inspector = app.window._inspector
    assert inspector.is_drafting()
    inspector._name_edit.setText("half typed")
    qtbot.waitUntil(lambda: _playing_bookmark(app) == after.id, timeout=5000)
    qtbot.wait(100)
    assert inspector.is_drafting() and inspector._name_edit.text() == "half typed"  # not swept away


# -- new bookmarks go to the top of the list (reported on the 0.9.0 build) --


def _listed_names(app) -> list[str]:
    tree = app.window._bookmark_panel._tree
    column = COLUMNS.index("Name")
    return [tree.topLevelItem(i).text(column) for i in range(tree.topLevelItemCount())]


def test_a_new_bookmark_goes_to_the_top_of_the_list(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    _bookmark(app, name="early", start_us=500_000, end_us=800_000)
    _bookmark(app, name="late", start_us=4_000_000, end_us=4_500_000)
    window = app.window
    window._waveform_scene.set_selection(Selection(start_us=2_000_000, end_us=2_600_000))
    window._inspector._name_edit.setText("newest")
    window._bookmark_selection_button.click()
    assert _listed_names(app) == ["newest", "early", "late"]  # not by start time
    window._waveform_scene.set_selection(Selection(start_us=3_000_000, end_us=3_500_000))
    window._inspector._name_edit.setText("newer still")
    window._bookmark_selection_button.click()
    assert _listed_names(app)[:2] == ["newer still", "newest"]
    window._on_point_bookmark_requested(5_000_000)  # Bookmark menu: point at the playhead
    assert _listed_names(app)[1:] == ["newer still", "newest", "early", "late"]  # the point is on top


# -- a playing selection is green, and carries on as the bookmark made from it --


def test_a_playing_selection_and_its_song_are_green(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    scene = window._waveform_scene
    scene.set_selection(Selection(start_us=2_000_000, end_us=2_600_000))
    assert scene._selection_item.playback_state() is None
    window._loop_selection_button.click()
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.PLAYING, timeout=3000)
    assert scene._selection_item.playback_state() == "playing"
    assert window._playlist_panel._bookmark_song == (app._current_vlc_item_id, "playing")
    window._transport.stop_button.click()
    assert scene._selection_item.playback_state() == "done"  # yellow once played
    assert window._playlist_panel._bookmark_song[1] == "done"
    scene.set_selection(Selection(start_us=3_000_000, end_us=3_400_000))  # another one
    assert scene._selection_item.playback_state() is None
    assert app._bookmark_playback is None


def test_bookmarking_a_playing_selection_keeps_it_playing_as_the_bookmark(qtbot, tmp_path) -> None:
    """Reported on the 0.9.0 build: Bookmark selection while the selection played stopped
    the playback, and nothing showed the new bookmark playing."""
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window._waveform_scene.set_selection(Selection(start_us=2_000_000, end_us=2_600_000))
    window._loop_selection_button.click()
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.PLAYING, timeout=3000)
    window._inspector._repeat_spin.setValue(2)  # set up in the form before bookmarking
    window._bookmark_selection_button.click()
    bookmark = window._inspector.current_bookmark()
    assert bookmark is not None and (bookmark.start_us, bookmark.end_us) == (2_000_000, 2_600_000)
    assert window._waveform_scene.selection() is None
    assert app._loop_controller.state is not LoopState.IDLE  # still playing
    assert app._active_loop_bookmark_id == bookmark.id
    # green in all three places
    assert app._bookmark_playback.bookmark_id == bookmark.id and app._bookmark_playback.state == "playing"
    assert window._waveform_scene.bookmark_item(bookmark.id).playback_state() == "playing"
    assert window._bookmark_panel._playback == (bookmark.id, "playing")
    assert window._playlist_panel._bookmark_song == (app._current_vlc_item_id, "playing")
    assert app._loop_controller.spec.repeat_count == 2  # the bookmark's own settings now


def test_bookmarking_a_selection_that_is_not_playing_starts_nothing(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window._waveform_scene.set_selection(Selection(start_us=2_000_000, end_us=2_600_000))
    window._bookmark_selection_button.click()
    assert app._loop_controller.state is LoopState.IDLE
    assert app._bookmark_playback is None


# -- Max goes to the fader's top (reported on the 0.9.0 build) --


def test_max_raises_the_volume_to_125_percent(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    assert MAX_BUTTON_VOLUME == 320  # 125 % on VLC's 0-512 scale
    app.window._volume_eq._max_button.click()  # nothing plays: at once
    qtbot.waitUntil(lambda: app.session.adapter.get_status().volume == 320, timeout=3000)
    qtbot.waitUntil(lambda: app.window.volume_level() == 320, timeout=3000)  # the fader at its top


# -- a bookmark made while the song plays through it (reported on the 0.9.0 build) --


def _play_from(app, qtbot, time_us: int) -> None:
    adapter = app.session.adapter
    adapter.seek_absolute_us(time_us)
    app._on_play_pause_clicked()
    qtbot.waitUntil(lambda: app._last_playback_state == "playing"
                    and app._actually_playing_vlc_item_id == app._current_vlc_item_id
                    and abs(app._clock.estimated_position_us() - time_us) < 400_000, timeout=3000)


def test_a_bookmark_made_while_the_song_plays_through_it_is_green(qtbot, tmp_path) -> None:
    """The main Play, not ▶ Selection: the song plays through the highlighted region and
    Bookmark selection makes it a bookmark -- the one playing now, green everywhere."""
    app = _make_app(qtbot, tmp_path)
    window = app.window
    _play_from(app, qtbot, 2_200_000)
    window._waveform_scene.set_selection(Selection(start_us=2_000_000, end_us=3_000_000))
    window._bookmark_selection_button.click()
    bookmark = window._inspector.current_bookmark()
    assert app._bookmark_playback is not None and app._bookmark_playback.bookmark_id == bookmark.id
    assert app._bookmark_playback.state == "playing" and app._bookmark_playback.looping
    assert window._waveform_scene.bookmark_item(bookmark.id).playback_state() == "playing"
    assert window._bookmark_panel._playback == (bookmark.id, "playing")
    assert window._playlist_panel._bookmark_song == (app._current_vlc_item_id, "playing")
    # ... and it loops from there, as a bookmark (Loop is on for a new one): no seek now
    assert app._loop_controller.state is LoopState.PLAYING and app._active_loop_bookmark_id == bookmark.id


def test_a_bookmark_made_away_from_the_playhead_is_not_green(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    _play_from(app, qtbot, 4_500_000)
    window._waveform_scene.set_selection(Selection(start_us=2_000_000, end_us=3_000_000))
    window._bookmark_selection_button.click()
    assert app._bookmark_playback is None


# -- ⏮ / ⏭ step through the bookmark list (reported on the 0.9.0 build) --


def test_next_and_previous_bookmark_play_the_rows_below_and_above(qtbot, tmp_path) -> None:
    app, item_a, media_a, item_b, media_b = _two_song_app(qtbot, tmp_path)
    first = _bookmark(app, media_id=media_a.id, name="first", start_us=2_000_000, end_us=2_400_000)
    second = _bookmark(app, media_id=media_b.id, name="second", start_us=1_000_000, end_us=1_400_000,
                       loop_enabled=True, repeat_count=4)
    third = _bookmark(app, media_id=media_a.id, name="third", start_us=500_000, end_us=900_000)
    app._on_bookmark_reorder_requested([first.id, second.id, third.id])
    window, panel = app.window, app.window._bookmark_panel
    panel.select_bookmark(first.id)
    window._transport.next_bookmark_button.click()
    assert _playing_bookmark(app) == second.id  # the row below, in the other song
    assert panel._selected_bookmark_ids() == [second.id]
    assert window._inspector.current_bookmark().id == second.id  # Bookmark Studio shows it
    assert window._inspector._repeat_spin.value() == 4
    assert app._active_loop_bookmark_id == second.id  # played with its own settings (a loop)
    qtbot.waitUntil(lambda: app.session.adapter.get_status().current_playlist_item_id == item_b.vlc_id,
                    timeout=3000)
    window._transport.next_bookmark_button.click()
    assert _playing_bookmark(app) == third.id
    window._transport.next_bookmark_button.click()  # the last row: nothing below
    assert _playing_bookmark(app) == third.id
    window._transport.previous_bookmark_button.click()
    assert _playing_bookmark(app) == second.id
    assert window._bookmark_panel._playback == (second.id, "playing")  # green


def test_next_bookmark_counts_from_the_bookmark_playing_not_the_selected_row(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    rows = [_bookmark(app, name=name, start_us=start, end_us=start + 300_000)
            for name, start in (("a", 500_000), ("b", 1_500_000), ("c", 2_500_000), ("d", 3_500_000))]
    app._on_bookmark_reorder_requested([b.id for b in rows])
    app._on_play_bookmark_requested(rows[0].id)
    app.window._bookmark_panel.select_bookmark(rows[2].id)  # looking at another one
    app.window._transport.next_bookmark_button.click()
    assert _playing_bookmark(app) == rows[1].id


def test_with_nothing_played_or_selected_next_starts_at_the_top_previous_at_the_bottom(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    top = _bookmark(app, name="top", start_us=500_000, end_us=800_000)
    bottom = _bookmark(app, name="bottom", start_us=2_500_000, end_us=2_800_000)
    app._on_bookmark_reorder_requested([top.id, bottom.id])
    app.window._transport.previous_bookmark_button.click()
    assert _playing_bookmark(app) == bottom.id
    app._clear_bookmark_playback()
    app.window._bookmark_panel.select_bookmarks(set())
    app.window._transport.next_bookmark_button.click()
    assert _playing_bookmark(app) == top.id


# -- the end of a session (found while testing the 0.9.0 build) --


def test_the_undo_stack_is_unhooked_when_the_session_ends(qtbot, tmp_path) -> None:
    """At every quit the undo stack, clearing itself as the window was destroyed, redrew
    the window -- whose waveform was already gone (RuntimeError)."""
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=2_000_000))
    window._bookmark_selection_button.click()  # one undo step
    refreshed: list[int] = []
    window._refresh_bookmarks = lambda: refreshed.append(1)
    app.stop()
    window._undo_stack.clear()  # as its destructor does
    assert refreshed == []


# -- a bookmark saved during playback loops from there (reported on the 0.9.0 build) --


def test_a_bookmark_saved_during_playback_counts_its_loops_and_fades_from_there(qtbot, tmp_path) -> None:
    """The pass playing when it is saved is its first; then its Repeat count, its fade-out
    before each end, and After loop -- as when it is played."""
    app = _make_app(qtbot, tmp_path)
    window, controller = app.window, app._loop_controller
    _play_from(app, qtbot, 2_100_000)
    window._waveform_scene.set_selection(Selection(start_us=2_000_000, end_us=2_600_000))
    window._inspector._repeat_spin.setValue(2)
    window._inspector._fade_out_spin.setValue(200)
    passes: list = []
    controller.iteration_changed.connect(passes.append)
    completions: list = []
    controller.loop_completed.connect(completions.append)
    levels: list[int] = []
    controller.volume_stepped.connect(levels.append)
    target = controller.target_volume
    window._bookmark_selection_button.click()
    bookmark = window._inspector.current_bookmark()
    assert controller.spec is not None and controller.spec.repeat_count == 2 and controller.spec.fade_out_ms == 200
    assert controller._passes_done == 0  # the pass playing is the first
    qtbot.waitUntil(lambda: completions == [CompletionAction.CONTINUE], timeout=5000)
    assert passes == [1]  # one more pass after the first, then done
    assert any(level < target for level in levels)  # it faded out before an end
    assert app._bookmark_playback.bookmark_id == bookmark.id and app._bookmark_playback.state == "done"


def test_a_playing_selection_made_a_bookmark_counts_its_loops_from_the_save(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window, controller = app.window, app._loop_controller
    window._waveform_scene.set_selection(Selection(start_us=2_000_000, end_us=2_300_000))
    passes: list = []
    controller.iteration_changed.connect(passes.append)
    window._loop_selection_button.click()
    qtbot.waitUntil(lambda: len(passes) >= 2, timeout=5000)  # the selection looped twice already
    window._inspector._repeat_spin.setValue(3)
    window._bookmark_selection_button.click()
    assert controller._passes_done == 0 and controller._remaining == 3  # its own count starts now


# -- Play plays the song on screen (reported on the 0.9.0 build) --


def test_play_plays_the_song_picked_in_the_playlist(qtbot, tmp_path) -> None:
    """A single click in the playlist shows a song without playing it; Play then resumed
    the player's own song, and the red line didn't move on the one on screen."""
    app, item_a, media_a, item_b, media_b = _two_song_app(qtbot, tmp_path)
    adapter = app.session.adapter
    qtbot.waitUntil(lambda: app._actually_playing_vlc_item_id == item_a.vlc_id, timeout=3000)
    app._on_playlist_item_selected(item_b.vlc_id)  # the single click
    assert app._current_vlc_item_id == item_b.vlc_id and not app.window.follow_player_enabled()
    app.window._transport.play_pause_button.click()
    qtbot.waitUntil(lambda: adapter.get_status().current_playlist_item_id == item_b.vlc_id
                    and adapter.get_status().state == "playing", timeout=3000)
    assert app.window.follow_player_enabled()  # the playhead follows the player again
    adapter.seek_absolute_us(1_500_000)  # playback moves on...
    qtbot.waitUntil(lambda: app.window.playhead_time_us() >= 1_500_000, timeout=3000)  # ...and so does the line


def test_play_after_pause_resumes_in_place(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    _play_from(app, qtbot, 3_000_000)
    app.window._transport.play_pause_button.click()  # pause
    qtbot.waitUntil(lambda: adapter.get_status().state == "paused", timeout=3000)
    app.window._transport.play_pause_button.click()  # play: the same song, where it was
    qtbot.waitUntil(lambda: adapter.get_status().state == "playing", timeout=3000)
    assert adapter.get_status().time_us >= 3_000_000


# -- typing into Fade in / Fade out replaces "Off" (reported on the 0.9.0 build) --


def test_clicking_into_a_fade_field_selects_off_so_typing_replaces_it(qtbot, tmp_path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    bookmark = _bookmark(app, start_us=1_000_000, end_us=2_000_000, loop_enabled=True)
    window._bookmark_panel.select_bookmark(bookmark.id)
    inspector = window._inspector
    # (the whole of Off / Forever; of "0 ms" the number -- typing replaces it, the unit stays)
    for spin, text, selected in ((inspector._fade_in_spin, "Off", "Off"), (inspector._fade_out_spin, "Off", "Off"),
                                 (inspector._gap_spin, "0 ms", "0"), (inspector._repeat_spin, "Forever", "Forever")):
        assert spin.text() == text
        QTest.mouseClick(spin.lineEdit(), Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda spin=spin, selected=selected: spin.lineEdit().selectedText() == selected,
                        timeout=2000)
    QTest.mouseClick(inspector._fade_in_spin.lineEdit(), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: inspector._fade_in_spin.lineEdit().selectedText() == "Off", timeout=2000)
    QTest.keyClicks(inspector._fade_in_spin, "450")
    assert inspector._fade_in_spin.value() == 450


# -- Duplicate (requested on the 0.9.0 build) --


def test_duplicate_sits_under_move_down_just_over_save(qtbot, tmp_path) -> None:
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QAction

    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    panel = window._bookmark_panel
    order = [panel._move_up_button, panel._move_down_button, panel._duplicate_button, panel._export_button]
    ys = [b.mapTo(window, QPoint(0, 0)).y() for b in order]
    assert ys == sorted(ys) and panel._duplicate_button.text() == "Duplicate"
    assert not panel._duplicate_button.isEnabled()
    action = next(a for a in window.menuBar().findChildren(QAction) if a.text() == "Duplicate Selected Bookmarks")
    assert action.shortcut().toString() == "Ctrl+D"


def test_duplicates_get_names_of_their_own_at_the_top_and_undo_together(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    panel = window._bookmark_panel
    one = _bookmark(app, name="drop", start_us=1_000_000, end_us=2_000_000, loop_enabled=True, repeat_count=3,
                    fade_in_ms=100, fade_out_ms=200, tags=("intro",), notes="first")
    _bookmark(app, name="middle", start_us=2_500_000, end_us=2_700_000)
    two = _bookmark(app, name="peak", start_us=3_000_000, end_us=3_500_000)
    panel.select_bookmarks({one.id, two.id})
    assert panel._duplicate_button.isEnabled()
    panel._duplicate_button.click()
    rows = app._bookmark_repository.list_for_playlist(app.playlists.synchronizer.active_playlist_id)
    assert len(rows) == 5
    copy_one, copy_two = rows[0], rows[1]  # at the top, in the order they were in
    names = [b.name for b in rows]
    assert len(set(names)) == 5  # every name its own
    assert copy_one.name not in ("drop", "peak") and len(copy_one.name) == 6
    for copy, original in ((copy_one, one), (copy_two, two)):
        assert copy.id != original.id
        assert (copy.start_us, copy.end_us, copy.loop_enabled, copy.repeat_count, copy.fade_in_ms,
                copy.fade_out_ms, copy.tags, copy.notes) == (
            original.start_us, original.end_us, original.loop_enabled, original.repeat_count,
            original.fade_in_ms, original.fade_out_ms, original.tags, original.notes)
    assert set(panel._selected_bookmark_ids()) == {copy_one.id, copy_two.id}
    assert panel.is_flashing(copy_one.id)
    window._undo_stack.undo()  # one step takes both away
    assert len(app._bookmark_repository.list_for_playlist(app.playlists.synchronizer.active_playlist_id)) == 3
    window._undo_stack.redo()
    assert {b.name for b in app._bookmark_repository.list_for_playlist(
        app.playlists.synchronizer.active_playlist_id)} == set(names)


def test_a_single_duplicate_is_shown_in_the_bookmark_studio_tab(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    original = _bookmark(app, name="solo", start_us=1_000_000, end_us=2_000_000)
    app.window._bookmark_panel.select_bookmark(original.id)
    app.window._bookmark_panel.request_duplicate()
    shown = app.window._inspector.current_bookmark()
    assert shown is not None and shown.id != original.id and shown.start_us == original.start_us


# -- a song played from the playlist clears the last bookmark's highlight (0.9.0) --


def _played_bookmark(app, qtbot):  # noqa: ANN001, ANN202
    bookmark = _bookmark(app, name="played", start_us=1_000_000, end_us=1_200_000, loop_enabled=True,
                         repeat_count=1, completion_action=CompletionAction.PAUSE)
    app._on_play_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: app._bookmark_playback is not None and app._bookmark_playback.state == "done",
                    timeout=5000)
    return bookmark


def _nothing_highlighted(app) -> bool:  # noqa: ANN001
    window = app.window
    return (app._bookmark_playback is None and window._bookmark_panel._playback is None
            and window._waveform_scene._playback is None and window._playlist_panel._bookmark_song is None)


def test_a_song_double_clicked_in_the_playlist_clears_the_last_bookmarks_highlight(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    _played_bookmark(app, qtbot)
    assert app.window._bookmark_panel._playback is not None  # yellow: played
    app._on_playlist_item_double_clicked(2)
    assert _nothing_highlighted(app)


def test_a_song_picked_in_the_playlist_and_played_clears_it_too(qtbot, tmp_path) -> None:
    app, item_a, media_a, item_b, media_b = _two_song_app(qtbot, tmp_path)
    _bookmark(app, media_id=media_a.id, name="played", start_us=1_000_000, end_us=1_200_000,
              loop_enabled=True, repeat_count=1, completion_action=CompletionAction.PAUSE)
    bookmark = app._bookmark_repository.list_for_playlist(app.playlists.synchronizer.active_playlist_id)[0]
    app._on_play_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: app._bookmark_playback is not None and app._bookmark_playback.state == "done",
                    timeout=5000)
    qtbot.waitUntil(lambda: app._last_playback_state == "paused", timeout=3000)  # its loop's pause
    app._on_playlist_item_selected(item_b.vlc_id)  # the single click
    app.window._transport.play_pause_button.click()
    assert _nothing_highlighted(app)


@pytest.mark.parametrize("button", ["next_track_button", "previous_track_button"])
def test_next_and_previous_track_clear_it_and_end_a_loop(qtbot, tmp_path, button) -> None:
    app = _make_app(qtbot, tmp_path)
    bookmark = _bookmark(app, name="looping", start_us=1_000_000, end_us=3_000_000, loop_enabled=True)
    app._on_loop_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.PLAYING, timeout=3000)
    getattr(app.window._transport, button).click()
    assert _nothing_highlighted(app) and app._loop_controller.state is LoopState.IDLE


def test_resuming_the_same_song_keeps_the_highlight(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    bookmark = _played_bookmark(app, qtbot)  # its loop ended with a pause
    qtbot.waitUntil(lambda: app._last_playback_state == "paused", timeout=3000)
    app.window._transport.play_pause_button.click()
    assert app._bookmark_playback is not None and app._bookmark_playback.bookmark_id == bookmark.id
