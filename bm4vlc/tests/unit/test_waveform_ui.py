from __future__ import annotations

from uuid import uuid4

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
