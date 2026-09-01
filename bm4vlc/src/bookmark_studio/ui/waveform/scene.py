"""WaveformScene(QGraphicsScene): owns WaveformItem, playhead, selection, and bookmark
items (spec #4). Never touches the database directly (spec #113) -- everything crosses
out as a signal for a controller to act on.
"""
from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import QRectF, Signal
from PySide6.QtWidgets import QGraphicsScene

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.selection import Selection
from bookmark_studio.ui.waveform.bookmark_item import BookmarkPointItem, BookmarkRegionItem
from bookmark_studio.ui.waveform.playhead_item import PlayheadItem
from bookmark_studio.ui.waveform.selection_item import SelectionItem
from bookmark_studio.ui.waveform.waveform_item import WaveformItem, scene_x_to_time_us, time_us_to_scene_x
from bookmark_studio.waveform.pyramid import WaveformPyramid

TRACK_HEIGHT = 160


class WaveformScene(QGraphicsScene):
    seek_requested = Signal(int)
    selection_changed = Signal(object)  # Selection | None
    point_bookmark_requested = Signal(int)
    bookmark_activated = Signal(object)  # UUID
    bookmark_move_finished = Signal(object, int, int)  # UUID, start_us, end_us
    bookmark_resize_finished = Signal(object, str, int)  # UUID, handle, value_us
    bookmark_context_menu_requested = Signal(object, object)  # UUID, scene QPointF

    def __init__(self, duration_us: int = 0) -> None:
        super().__init__()
        self._duration_us = duration_us
        self._height = TRACK_HEIGHT
        self._waveform_item = WaveformItem(_empty_pyramid(), duration_us, self._height)
        self.addItem(self._waveform_item)
        self._playhead_item = PlayheadItem(self._height)
        self.addItem(self._playhead_item)
        self._selection_item: SelectionItem | None = None
        self._bookmark_items: dict[UUID, BookmarkRegionItem | BookmarkPointItem] = {}
        self.setSceneRect(QRectF(0, 0, time_us_to_scene_x(max(duration_us, 1)), self._height))

    # -- waveform / duration --

    def set_duration_us(self, duration_us: int) -> None:
        """Sets the known media duration independent of waveform pixel data (spec #179:
        bookmarks/clicks must work immediately, without waiting for waveform decoding).
        """
        self._duration_us = duration_us
        self._waveform_item.set_pyramid(self._waveform_item._pyramid, duration_us)
        self.setSceneRect(QRectF(0, 0, time_us_to_scene_x(max(duration_us, 1)), self._height))

    def set_waveform(self, pyramid: WaveformPyramid, duration_us: int) -> None:
        self.set_duration_us(duration_us)
        self._waveform_item.set_pyramid(pyramid, duration_us)

    def set_playhead_time_us(self, time_us: int) -> None:
        self._playhead_item.set_time_us(time_us)

    # -- selection --

    def set_selection(self, selection: Selection | None) -> None:
        if self._selection_item is not None:
            self.removeItem(self._selection_item)
            self._selection_item = None
        if selection is not None:
            self._selection_item = SelectionItem(self._height, selection)
            self.addItem(self._selection_item)
        self.selection_changed.emit(selection)

    def selection(self) -> Selection | None:
        return self._selection_item.selection() if self._selection_item else None

    def clear_selection(self) -> None:
        self.set_selection(None)

    # -- bookmarks --

    def set_bookmarks(self, bookmarks: list[Bookmark]) -> None:
        for item in self._bookmark_items.values():
            self.removeItem(item)
        self._bookmark_items.clear()
        for bookmark in bookmarks:
            self._add_bookmark_item(bookmark)

    def _add_bookmark_item(self, bookmark: Bookmark) -> None:
        if bookmark.end_us is not None:
            item: BookmarkRegionItem | BookmarkPointItem = BookmarkRegionItem(bookmark, self._height)
            item.move_finished.connect(
                lambda start, end, bid=bookmark.id: self.bookmark_move_finished.emit(bid, start, end)
            )
            item.resize_finished.connect(
                lambda handle, value, bid=bookmark.id: self.bookmark_resize_finished.emit(bid, handle, value)
            )
        else:
            item = BookmarkPointItem(bookmark, self._height)
            item.move_finished.connect(
                lambda start, bid=bookmark.id: self.bookmark_move_finished.emit(bid, start, start)
            )
        item.activated.connect(lambda bid=bookmark.id: self.bookmark_activated.emit(bid))
        item.context_menu_requested.connect(
            lambda pos, bid=bookmark.id: self.bookmark_context_menu_requested.emit(bid, pos)
        )
        self.addItem(item)
        self._bookmark_items[bookmark.id] = item

    def bookmark_item(self, bookmark_id: UUID) -> BookmarkRegionItem | BookmarkPointItem | None:
        return self._bookmark_items.get(bookmark_id)

    # -- empty-waveform interaction (spec #6) --

    def handle_empty_click(self, time_us: int) -> None:
        self.seek_requested.emit(max(0, min(time_us, self._duration_us)))

    def handle_empty_drag(self, start_us: int, end_us: int) -> None:
        lo, hi = sorted((max(0, start_us), max(0, end_us)))
        if hi - lo < 1:
            return
        self.set_selection(Selection(start_us=lo, end_us=min(hi, self._duration_us) or lo + 1))

    def handle_empty_double_click(self, time_us: int) -> None:
        self.point_bookmark_requested.emit(max(0, min(time_us, self._duration_us)))


def _empty_pyramid() -> WaveformPyramid:
    from bookmark_studio.waveform.pyramid import PyramidLevel
    import numpy as np

    return WaveformPyramid(levels=(PyramidLevel(block_size=64, peaks=np.zeros((0, 2), dtype="<f4")),), sample_rate=8000)
