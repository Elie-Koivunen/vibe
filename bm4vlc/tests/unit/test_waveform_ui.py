from __future__ import annotations

from uuid import uuid4

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.ui.waveform.scene import WaveformScene
from bookmark_studio.ui.waveform.view import WaveformView
from bookmark_studio.ui.waveform.waveform_item import time_us_to_scene_x


def _make_view(qtbot, duration_us: int = 10_000_000):
    scene = WaveformScene(duration_us)
    view = WaveformView(scene)
    view.resize(800, 200)
    qtbot.addWidget(view)
    view.show()
    return scene, view


def _to_view_pos(view: WaveformView, time_us: int, y: float = 40.0) -> QPoint:
    scene_pos = view.mapToScene(0, 0)  # ensure transform is current
    point = view.mapFromScene(time_us_to_scene_x(time_us), y)
    return point


def _segment_bookmark(**overrides) -> Bookmark:
    defaults = dict(
        id=uuid4(), playlist_id=None, media_id=uuid4(), scope=BookmarkScope.GLOBAL_MEDIA,
        lane_id=None, bookmark_type=BookmarkType.SEGMENT, name="Chorus", start_us=1_000_000,
        end_us=2_000_000, loop_enabled=False, repeat_count=None, loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    defaults.update(overrides)
    return Bookmark(**defaults)


def test_click_empty_waveform_seeks(qtbot) -> None:
    scene, view = _make_view(qtbot)
    seeks = []
    scene.seek_requested.connect(lambda t: seeks.append(t))

    pos = _to_view_pos(view, 3_000_000)
    QTest.mousePress(view.viewport(), Qt.LeftButton, pos=pos)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=pos)

    assert len(seeks) == 1
    assert abs(seeks[0] - 3_000_000) < 5_000  # within 5ms of the click


def test_handle_empty_drag_past_track_end_does_not_raise(qtbot) -> None:
    """Regression, confirmed live: dragging a selection that starts at or past the very
    end of the track spammed "ValueError: end_us must be greater than start_us" on
    every mouse-move and aborted the handler each time -- so the selection (and by
    extension the whole "drag to adjust" flow) simply never updated. Root cause was
    clamping `hi` to duration_us only AFTER deciding "is this drag big enough" using
    the unclamped width, letting a post-clamp zero-length range slip through.
    """
    scene, _view = _make_view(qtbot, duration_us=10_000_000)
    # start exactly at the track's end, drag further right (off the end entirely)
    scene.handle_empty_drag(10_000_000, 15_000_000)
    assert scene.selection() is None  # clamped to zero width -> correctly a no-op

    # a drag spanning from before the end to past it should still clamp sanely
    scene.handle_empty_drag(9_000_000, 20_000_000)
    assert scene.selection() is not None
    assert scene.selection().start_us == 9_000_000
    assert scene.selection().end_us == 10_000_000


def test_drag_empty_waveform_creates_selection(qtbot) -> None:
    scene, view = _make_view(qtbot)
    selections = []
    scene.selection_changed.connect(lambda s: selections.append(s))

    start_pos = _to_view_pos(view, 1_000_000)
    end_pos = _to_view_pos(view, 2_000_000)
    QTest.mousePress(view.viewport(), Qt.LeftButton, pos=start_pos)
    QTest.mouseMove(view.viewport(), pos=end_pos)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=end_pos)

    assert scene.selection() is not None
    assert abs(scene.selection().start_us - 1_000_000) < 25_000
    assert abs(scene.selection().end_us - 2_000_000) < 25_000
    assert selections  # at least one selection_changed fired


def test_escape_clears_selection(qtbot) -> None:
    scene, view = _make_view(qtbot)
    from bookmark_studio.domain.selection import Selection
    scene.set_selection(Selection(start_us=0, end_us=1000))
    assert scene.selection() is not None

    QTest.keyClick(view, Qt.Key_Escape)
    assert scene.selection() is None


def test_double_click_empty_waveform_requests_point_bookmark(qtbot) -> None:
    scene, view = _make_view(qtbot)
    requests = []
    scene.point_bookmark_requested.connect(lambda t: requests.append(t))

    pos = _to_view_pos(view, 4_000_000)
    QTest.mouseDClick(view.viewport(), Qt.LeftButton, pos=pos)

    assert len(requests) == 1
    assert abs(requests[0] - 4_000_000) < 5_000


def test_dragging_bookmark_body_emits_move_finished_preserving_duration(qtbot) -> None:
    scene, view = _make_view(qtbot)
    bookmark = _segment_bookmark(start_us=1_000_000, end_us=2_000_000)
    scene.set_bookmarks([bookmark])

    moves = []
    scene.bookmark_move_finished.connect(lambda bid, s, e: moves.append((bid, s, e)))

    mid_time = 1_500_000  # well inside the body, away from resize handles
    start_pos = _to_view_pos(view, mid_time)
    end_pos = _to_view_pos(view, mid_time + 300_000)  # drag 300ms to the right

    QTest.mousePress(view.viewport(), Qt.LeftButton, pos=start_pos)
    QTest.mouseMove(view.viewport(), pos=end_pos)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=end_pos)

    assert len(moves) == 1
    bid, new_start, new_end = moves[0]
    assert bid == bookmark.id
    assert new_end - new_start == 1_000_000  # duration preserved (spec #49)
    assert abs(new_start - 1_300_000) < 25_000


def test_dragging_left_handle_resizes_start_only(qtbot) -> None:
    scene, view = _make_view(qtbot)
    bookmark = _segment_bookmark(start_us=1_000_000, end_us=2_000_000)
    scene.set_bookmarks([bookmark])

    resizes = []
    scene.bookmark_resize_finished.connect(lambda bid, handle, value: resizes.append((bid, handle, value)))

    # A couple ms into the region, well within the left handle's hit area.
    start_pos = _to_view_pos(view, 1_003_000)
    end_pos = _to_view_pos(view, 1_200_000)

    QTest.mousePress(view.viewport(), Qt.LeftButton, pos=start_pos)
    QTest.mouseMove(view.viewport(), pos=end_pos)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=end_pos)

    assert len(resizes) == 1
    bid, handle, value = resizes[0]
    assert handle == "start"
    assert value > 1_000_000  # moved right, end boundary untouched


def test_fit_entire_media_before_show_is_corrected_on_resize(qtbot) -> None:
    """Calling fit_entire_media() before the widget reaches its final on-screen size
    (e.g. a startup 'restore fitted view') must not leave click coordinates mapped
    against a stale placeholder viewport size -- caught via a live screenshot demo
    where a click landed on the wrong bookmark after exactly this sequence.

    Deliberately does NOT use the _make_view() helper: that resizes+shows first,
    which is exactly the ordering that hides this bug. This test reproduces the
    real ordering: construct, fit while the view still has Qt's transient
    un-shown placeholder size, only then resize/show to the real final size.
    """
    scene = WaveformScene(10_000_000)
    view = WaveformView(scene)
    qtbot.addWidget(view)

    view.fit_entire_media()  # called before the widget has any real on-screen size
    transform_before_resize = view.transform()

    view.resize(800, 200)
    view.show()
    qtbot.waitExposed(view)

    assert view.transform() != transform_before_resize
    fresh_fit_transform = view.transform()
    # A second resize must keep re-fitting (sticky fit mode), not freeze the first one.
    view.resize(500, 200)
    qtbot.wait(50)
    assert view.transform() != fresh_fit_transform


def test_manual_zoom_disables_sticky_fit_mode(qtbot) -> None:
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtCore import QPointF

    scene, view = _make_view(qtbot)
    view.fit_entire_media()
    fitted_transform = view.transform()

    event = QWheelEvent(
        QPointF(50, 50), QPointF(50, 50), QPoint(0, 0), QPoint(0, 120),
        Qt.NoButton, Qt.ControlModifier, Qt.NoScrollPhase, False,
    )
    view.wheelEvent(event)
    assert view.transform() != fitted_transform

    zoomed_transform = view.transform()
    view.resize(400, 150)
    qtbot.wait(50)
    # Once the user has manually zoomed, a resize must not snap back to "fit".
    assert view.transform() == zoomed_transform


def test_follow_playhead_scrolls_once_past_threshold_when_not_fitted(qtbot) -> None:
    scene, view = _make_view(qtbot, duration_us=120_000_000)  # 2 minutes, wider than any viewport
    # Deliberately NOT calling fit_entire_media(): follow_playhead is a no-op in fit
    # mode since the whole track (and thus the playhead) is always on screen there.
    # Qt centers an oversized scene in the viewport by default (not aligned to x=0),
    # so compute what's actually visible rather than assuming a fixed start position.
    before = view.transform()
    before_h_scroll = view.horizontalScrollBar().value()
    visible_rect = view.mapToScene(view.viewport().rect()).boundingRect()
    inside_time_us = int((visible_rect.left() + visible_rect.width() / 2) * 1000)
    far_outside_time_us = int(visible_rect.right() * 1000) + 50_000_000

    view.follow_playhead(inside_time_us)
    assert view.horizontalScrollBar().value() == before_h_scroll  # no scroll needed yet

    view.follow_playhead(far_outside_time_us)
    assert view.horizontalScrollBar().value() != before_h_scroll  # now it followed
    assert view.transform() == before  # follow scrolls, it never rescales


def test_follow_playhead_is_noop_in_fit_mode(qtbot) -> None:
    scene, view = _make_view(qtbot, duration_us=120_000_000)
    view.fit_entire_media()
    scroll_before = view.horizontalScrollBar().value()
    view.follow_playhead(90_000_000)
    assert view.horizontalScrollBar().value() == scroll_before


def test_ruler_item_present_and_sized_to_duration(qtbot) -> None:
    scene, view = _make_view(qtbot, duration_us=10_000_000)
    ruler = scene._ruler_item
    assert ruler.boundingRect().width() == pytest.approx(time_us_to_scene_x(10_000_000))
    scene.set_duration_us(20_000_000)
    assert ruler.boundingRect().width() == pytest.approx(time_us_to_scene_x(20_000_000))


def test_bookmark_click_activates_without_moving(qtbot) -> None:
    scene, view = _make_view(qtbot)
    bookmark = _segment_bookmark()
    scene.set_bookmarks([bookmark])

    activated = []
    scene.bookmark_activated.connect(lambda bid: activated.append(bid))

    mid_time = (bookmark.start_us + bookmark.end_us) // 2
    pos = _to_view_pos(view, mid_time)
    QTest.mousePress(view.viewport(), Qt.LeftButton, pos=pos)
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=pos)

    assert activated == [bookmark.id]
