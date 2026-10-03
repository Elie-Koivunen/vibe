"""0.9.0 (requested on its build): while a song plays from the playlist, the bookmarks
whose range the red line is crossing show orange in the bookmark list -- only while it is."""
from __future__ import annotations

from PySide6.QtGui import QColor

from bookmark_studio.domain.enums import BookmarkType
from bookmark_studio.ui.bookmark_panel import CROSSING_ROLE, CROSSING_ROW_COLOR, USER_ROLE
from tests.unit.test_050_ui import _bookmark, _make_app
from tests.unit.test_090_after_loop_and_fades import _play_from
from tests.unit.test_090_source_playlist_and_bpm_switch import _two_song_app


def _crossing(app) -> frozenset:  # noqa: ANN001
    return app.window._bookmark_panel.crossing()


def _row_colour(app, bookmark_id) -> QColor:  # noqa: ANN001
    """The colour the bookmark's row is painted (at its left edge, before the text)."""
    tree = app.window._bookmark_panel._tree
    row = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
               if tree.topLevelItem(i).data(0, USER_ROLE) == bookmark_id)
    rect = tree.visualItemRect(row)
    return tree.viewport().grab().toImage().pixelColor(rect.left() + 1, rect.center().y())


def test_a_bookmark_is_orange_while_the_red_line_crosses_it(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    adapter = app.session.adapter
    first = _bookmark(app, name="first", start_us=1_000_000, end_us=2_000_000)
    second = _bookmark(app, name="second", start_us=1_500_000, end_us=3_000_000)  # overlaps the first
    _bookmark(app, name="point", bookmark_type=BookmarkType.POINT, start_us=1_100_000, end_us=None)
    _play_from(app, qtbot, 500_000)
    assert _crossing(app) == frozenset()
    adapter.advance_time_us(700_000)  # 1.2 s
    qtbot.waitUntil(lambda: _crossing(app) == {first.id}, timeout=3000)
    assert _row_colour(app, first.id) == CROSSING_ROW_COLOR
    assert _row_colour(app, second.id) != CROSSING_ROW_COLOR
    assert window.selected_bookmark_id() is None  # shown, not selected
    adapter.advance_time_us(500_000)  # 1.7 s: in both
    qtbot.waitUntil(lambda: _crossing(app) == {first.id, second.id}, timeout=3000)
    adapter.advance_time_us(500_000)  # 2.2 s: past the first
    qtbot.waitUntil(lambda: _crossing(app) == {second.id}, timeout=3000)
    assert _row_colour(app, first.id) != CROSSING_ROW_COLOR
    adapter.advance_time_us(1_000_000)  # 3.2 s: past both
    qtbot.waitUntil(lambda: _crossing(app) == frozenset(), timeout=3000)
    tree = window._bookmark_panel._tree
    assert not any(tree.topLevelItem(i).data(0, CROSSING_ROLE) for i in range(tree.topLevelItemCount()))


def test_stopping_clears_it(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    bookmark = _bookmark(app, start_us=1_000_000, end_us=3_000_000)
    _play_from(app, qtbot, 1_500_000)
    qtbot.waitUntil(lambda: _crossing(app) == {bookmark.id}, timeout=3000)
    app._on_stop_clicked()
    qtbot.waitUntil(lambda: _crossing(app) == frozenset(), timeout=3000)


def test_not_while_a_bookmark_plays(qtbot, tmp_path) -> None:
    """A bookmark playing is green; the ones its range overlaps don't light up."""
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    played = _bookmark(app, name="played", start_us=1_000_000, end_us=3_000_000)
    overlapped = _bookmark(app, name="overlapped", start_us=1_500_000, end_us=2_500_000)
    app._on_play_bookmark_requested(played.id)
    qtbot.waitUntil(lambda: app._bookmark_playback is not None and app._last_playback_state == "playing",
                    timeout=3000)
    adapter.advance_time_us(800_000)  # 1.8 s: inside both
    qtbot.wait(1000)  # a couple of polls
    assert _crossing(app) == frozenset()
    assert app.window._bookmark_panel.playback_state(played.id) == "playing"
    assert overlapped.id not in _crossing(app)


def test_only_the_playing_songs_bookmarks(qtbot, tmp_path) -> None:
    app, item_a, media_a, item_b, media_b = _two_song_app(qtbot, tmp_path)
    adapter = app.session.adapter
    in_a = _bookmark(app, media_id=media_a.id, name="a", start_us=1_000_000, end_us=2_000_000)
    in_b = _bookmark(app, media_id=media_b.id, name="b", start_us=1_000_000, end_us=2_000_000)  # same times
    qtbot.waitUntil(lambda: app._current_media_id == media_a.id, timeout=3000)
    _play_from(app, qtbot, 1_500_000)
    qtbot.waitUntil(lambda: _crossing(app) == {in_a.id}, timeout=3000)
    app._on_playlist_item_double_clicked(item_b.vlc_id)  # the second song, from its start
    qtbot.waitUntil(lambda: adapter.get_status().current_playlist_item_id == item_b.vlc_id, timeout=3000)
    qtbot.waitUntil(lambda: _crossing(app) == frozenset(), timeout=3000)
    adapter.advance_time_us(1_500_000)
    qtbot.waitUntil(lambda: _crossing(app) == {in_b.id}, timeout=3000)


def test_it_stays_orange_when_the_list_is_rebuilt(qtbot, tmp_path) -> None:
    """E.g. a bookmark changed while the song plays through another one."""
    app = _make_app(qtbot, tmp_path)
    app.window.show()
    qtbot.waitExposed(app.window)
    crossed = _bookmark(app, name="crossed", start_us=1_000_000, end_us=3_000_000)
    _play_from(app, qtbot, 1_500_000)
    qtbot.waitUntil(lambda: _crossing(app) == {crossed.id}, timeout=3000)
    _bookmark(app, name="new", start_us=4_000_000, end_us=5_000_000)  # the list is rebuilt
    qtbot.wait(500)
    assert _row_colour(app, crossed.id) == CROSSING_ROW_COLOR


def test_a_bookmark_further_down_the_list_is_scrolled_into_view(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    for i in range(60):  # listed by start time: these first
        _bookmark(app, name=f"n{i}", start_us=100_000 + i * 5_000, end_us=104_000 + i * 5_000)
    far = _bookmark(app, name="far", start_us=5_000_000, end_us=5_800_000)  # the last row
    tree = window._bookmark_panel._tree
    tree.scrollToTop()

    def far_in_view() -> bool:
        row = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                   if tree.topLevelItem(i).data(0, USER_ROLE) == far.id)
        return tree.viewport().rect().intersects(tree.visualItemRect(row))

    assert not far_in_view()
    _play_from(app, qtbot, 5_200_000)
    qtbot.waitUntil(lambda: _crossing(app) == {far.id}, timeout=3000)
    assert far_in_view()
