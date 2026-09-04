"""SelectionItem: temporary paint-to-select highlight with live start/end/duration (spec #37).

Direct follow-up request: "when i press play to listen to the selection, i want
the ability to adjust the selection by dragging the sides" -- left/right edge
handles, same drag-to-resize idiom as BookmarkRegionItem (bookmark_item.py),
just without a "move whole body" mode (dragging the middle does nothing, same
as before this feature existed).
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from bookmark_studio.domain.bookmark import MIN_SEGMENT_DURATION_US
from bookmark_studio.domain.selection import Selection
from bookmark_studio.ui.waveform.waveform_item import time_us_to_scene_x

# Direct user request: "when highlighting to bookmark, its still in blue, change
# to greenish tint" -- a drag-selection destined to become a bookmark.
SELECTION_FILL = QColor(90, 200, 120, 80)
SELECTION_BORDER = QColor(90, 200, 120, 200)
HANDLE_WIDTH_PX = 8.0  # target on-screen handle width, in real device pixels -- same as bookmark_item.py


def _view_scale_x(item: QGraphicsItem) -> float:
    """Same fix as bookmark_item.py's resize handles: a fixed scene-unit hit width
    only corresponds to a constant number of real screen pixels at 1:1 zoom.
    """
    scene = item.scene()
    if scene is None:
        return 1.0
    views = scene.views()
    if not views:
        return 1.0
    scale = views[0].transform().m11()
    return scale if scale > 0 else 1.0


class SelectionItem(QGraphicsObject):
    # object, not int -- same 32-bit signal-marshaling overflow class fixed
    # elsewhere for every other raw-microsecond signal in this package.
    resize_preview = Signal(str, object)  # "start"|"end", value_us -- live, during drag
    resize_finished = Signal(str, object)  # -- once, on mouse release

    def __init__(self, height: float, selection: Selection) -> None:
        super().__init__()
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self._height = height
        self._selection = selection
        self._drag_mode: str | None = None
        self._drag_origin_scene_x = 0.0
        self._orig_start_us = selection.start_us
        self._orig_end_us = selection.end_us
        self._live_start_us = selection.start_us
        self._live_end_us = selection.end_us
        self.setZValue(50)
        self._sync_position()

    def selection(self) -> Selection:
        return self._selection

    def set_selection(self, selection: Selection) -> None:
        self.prepareGeometryChange()
        self._selection = selection
        self._orig_start_us = self._live_start_us = selection.start_us
        self._orig_end_us = self._live_end_us = selection.end_us
        self._sync_position()

    def _handle_width_scene(self) -> float:
        return HANDLE_WIDTH_PX / _view_scale_x(self)

    def _sync_position(self) -> None:
        self.setX(time_us_to_scene_x(self._live_start_us))

    def boundingRect(self) -> QRectF:  # noqa: N802
        width = time_us_to_scene_x(self._live_end_us) - time_us_to_scene_x(self._live_start_us)
        return QRectF(0, 0, max(width, 0.0), self._height)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        rect = self.boundingRect()
        painter.setBrush(QBrush(SELECTION_FILL))
        painter.setPen(QPen(SELECTION_BORDER, 1))
        painter.drawRect(rect)
        handle_width = self._handle_width_scene()
        painter.fillRect(QRectF(rect.left(), 0, handle_width, self._height), SELECTION_BORDER)
        painter.fillRect(QRectF(rect.right() - handle_width, 0, handle_width, self._height), SELECTION_BORDER)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        local_x = event.pos().x()
        width = self.boundingRect().width()
        handle_width = self._handle_width_scene()
        if local_x <= handle_width:
            self._drag_mode = "resize-start"
        elif local_x >= width - handle_width:
            self._drag_mode = "resize-end"
        else:
            # Not on a handle -- let the event fall through rather than swallowing
            # it (the waveform view's own click/drag-to-create-a-new-selection
            # handling never saw this item as "empty space" to begin with).
            event.ignore()
            return
        # Rebase the drag's origin on the CURRENT (possibly already-adjusted-by-a-
        # previous-drag) bounds, not whatever this item was constructed with --
        # otherwise a second drag after the first would compute its delta from a
        # stale starting point and jump.
        self._orig_start_us = self._live_start_us
        self._orig_end_us = self._live_end_us
        self._drag_origin_scene_x = event.scenePos().x()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_mode is None:
            return
        delta_us = int((event.scenePos().x() - self._drag_origin_scene_x) * 1000)
        if self._drag_mode == "resize-start":
            new_start = max(0, min(self._orig_start_us + delta_us, self._orig_end_us - MIN_SEGMENT_DURATION_US))
            self.prepareGeometryChange()
            self._live_start_us = new_start
            self._sync_position()
            self.resize_preview.emit("start", new_start)
        elif self._drag_mode == "resize-end":
            new_end = max(self._orig_start_us + MIN_SEGMENT_DURATION_US, self._orig_end_us + delta_us)
            self.prepareGeometryChange()
            self._live_end_us = new_end
            self.resize_preview.emit("end", new_end)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_mode == "resize-start":
            self._selection = Selection(start_us=self._live_start_us, end_us=self._selection.end_us)
            self.resize_finished.emit("start", self._live_start_us)
        elif self._drag_mode == "resize-end":
            self._selection = Selection(start_us=self._selection.start_us, end_us=self._live_end_us)
            self.resize_finished.emit("end", self._live_end_us)
        self._drag_mode = None
        event.accept()
