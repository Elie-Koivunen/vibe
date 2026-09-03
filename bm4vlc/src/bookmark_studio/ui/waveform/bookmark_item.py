"""BookmarkRegionItem/BookmarkPointItem(QGraphicsObject): drag/resize, never writes DB
directly (spec #113) -- emits intent signals; a controller decides what to persist.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from bookmark_studio.domain.bookmark import MIN_SEGMENT_DURATION_US, Bookmark
from bookmark_studio.ui.waveform.waveform_item import scene_x_to_time_us, time_us_to_scene_x

HANDLE_WIDTH_PX = 8.0  # target on-screen handle width, in real device pixels
REGION_FILL = QColor(240, 180, 60, 130)
REGION_BORDER = QColor(200, 140, 30)
HANDLE_COLOR = QColor(180, 110, 10)
POINT_COLOR = QColor(60, 170, 90)


def _view_scale_x(item: QGraphicsItem) -> float:
    """A fixed scene-unit handle width only corresponds to a constant number of real
    screen pixels when the view's zoom happens to be 1:1. Once fit_entire_media()
    zooms out to show a whole multi-minute track, 8 scene-ms became sub-pixel --
    confirmed live: the resize handles were reported as effectively impossible to grab
    ("make it so the highlighting box can be resized"). Handles (and hit-testing) now
    size themselves in real screen pixels by dividing through the view's current
    horizontal scale, the same fix applied to ruler/waveform text elsewhere in this
    package. Falls back to 1.0 (no view attached yet, e.g. mid-construction) so this
    never raises.
    """
    scene = item.scene()
    if scene is None:
        return 1.0
    views = scene.views()
    if not views:
        return 1.0
    scale = views[0].transform().m11()
    return scale if scale > 0 else 1.0


class BookmarkRegionItem(QGraphicsObject):
    """A segment bookmark: draggable body, left/right resize handles (spec #41, #49-50)."""

    # object, not int: these carry raw microsecond timecodes, which PySide6 would
    # otherwise marshal through a 32-bit C++ int and silently wrap past ~35.8 minutes
    # (2^31 us) -- same bug class fixed for TransportBar/BookmarkInspector's signals.
    move_started = Signal()
    move_preview = Signal(object, object)
    move_finished = Signal(object, object)
    resize_started = Signal(str)
    resize_preview = Signal(str, object)
    resize_finished = Signal(str, object)
    activated = Signal()
    context_menu_requested = Signal(QPointF)

    def __init__(self, bookmark: Bookmark, height: float) -> None:
        super().__init__()
        if bookmark.end_us is None:
            raise ValueError("BookmarkRegionItem requires a segment bookmark (end_us set)")
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self._height = height
        self._bookmark = bookmark
        self._drag_mode: str | None = None
        self._drag_origin_scene_x = 0.0
        self._orig_start_us = bookmark.start_us
        self._orig_end_us = bookmark.end_us
        self._live_start_us = bookmark.start_us
        self._live_end_us = bookmark.end_us
        self._sync_position()

    def bookmark(self) -> Bookmark:
        return self._bookmark

    def set_bookmark(self, bookmark: Bookmark) -> None:
        if bookmark.end_us is None:
            raise ValueError("BookmarkRegionItem requires a segment bookmark (end_us set)")
        self.prepareGeometryChange()
        self._bookmark = bookmark
        self._orig_start_us = self._live_start_us = bookmark.start_us
        self._orig_end_us = self._live_end_us = bookmark.end_us
        self._sync_position()

    def _handle_width_scene(self) -> float:
        return HANDLE_WIDTH_PX / _view_scale_x(self)

    def boundingRect(self) -> QRectF:  # noqa: N802
        width = time_us_to_scene_x(self._live_end_us) - time_us_to_scene_x(self._live_start_us)
        return QRectF(0, 0, max(width, self._handle_width_scene() * 2), self._height)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        rect = self.boundingRect()
        handle_width = self._handle_width_scene()
        painter.setBrush(QBrush(REGION_FILL))
        painter.setPen(QPen(REGION_BORDER, 1))
        painter.drawRect(rect)
        painter.fillRect(QRectF(rect.left(), 0, handle_width, self._height), HANDLE_COLOR)
        painter.fillRect(QRectF(rect.right() - handle_width, 0, handle_width, self._height), HANDLE_COLOR)

        # Same scene-vs-device-pixel bug as TimeRulerItem (see waveform_item.device_pixel_width):
        # a font drawn in scene coordinates shrinks to invisible once the view is zoomed
        # out via fit_entire_media(). Map the label anchor through the world transform,
        # reset to identity, and draw at the real device position.
        world_transform = painter.worldTransform()
        anchor_scene = QPointF(rect.left() + handle_width + 2, rect.top() + 2)
        device_anchor = world_transform.map(anchor_scene)
        painter.save()
        painter.setWorldTransform(QTransform())
        painter.setPen(QPen(QColor(30, 20, 0)))
        painter.drawText(device_anchor + QPointF(0, 10), self._bookmark.name)
        painter.restore()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        local_x = event.pos().x()
        width = self.boundingRect().width()
        handle_width = self._handle_width_scene()
        if local_x <= handle_width:
            self._drag_mode = "resize-start"
            self.resize_started.emit("start")
        elif local_x >= width - handle_width:
            self._drag_mode = "resize-end"
            self.resize_started.emit("end")
        else:
            self._drag_mode = "move"
            self.move_started.emit()
        self._drag_origin_scene_x = event.scenePos().x()
        self.activated.emit()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_mode is None:
            return
        delta_us = int((event.scenePos().x() - self._drag_origin_scene_x) * 1000)

        if self._drag_mode == "move":
            duration = self._orig_end_us - self._orig_start_us
            new_start = max(0, self._orig_start_us + delta_us)
            new_end = new_start + duration
            self.prepareGeometryChange()
            self._live_start_us, self._live_end_us = new_start, new_end
            self._sync_position()
            self.move_preview.emit(new_start, new_end)
        elif self._drag_mode == "resize-start":
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
        if self._drag_mode == "move":
            self.move_finished.emit(self._live_start_us, self._live_end_us)
        elif self._drag_mode == "resize-start":
            self.resize_finished.emit("start", self._live_start_us)
        elif self._drag_mode == "resize-end":
            self.resize_finished.emit("end", self._live_end_us)
        self._drag_mode = None
        event.accept()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.context_menu_requested.emit(event.scenePos())
        event.accept()

    def _sync_position(self) -> None:
        self.setX(time_us_to_scene_x(self._live_start_us))


class BookmarkPointItem(QGraphicsObject):
    """A point bookmark: a small diamond marker, draggable but not resizable (spec #40)."""

    # object, not int -- see BookmarkRegionItem above.
    move_started = Signal()
    move_preview = Signal(object)
    move_finished = Signal(object)
    activated = Signal()
    context_menu_requested = Signal(QPointF)

    _MARKER_HALF_WIDTH = 6.0

    def __init__(self, bookmark: Bookmark, height: float) -> None:
        super().__init__()
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._height = height
        self._bookmark = bookmark
        self._dragging = False
        self._orig_start_us = bookmark.start_us
        self._live_start_us = bookmark.start_us
        self._sync_position()

    def bookmark(self) -> Bookmark:
        return self._bookmark

    def set_bookmark(self, bookmark: Bookmark) -> None:
        self._bookmark = bookmark
        self._orig_start_us = self._live_start_us = bookmark.start_us
        self._sync_position()

    def boundingRect(self) -> QRectF:  # noqa: N802
        half = self._MARKER_HALF_WIDTH
        return QRectF(-half, 0, half * 2, self._height)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        half = self._MARKER_HALF_WIDTH
        painter.setPen(QPen(POINT_COLOR, 1.5))
        painter.drawLine(0, 0, 0, self._height)
        painter.setBrush(QBrush(POINT_COLOR))
        painter.drawPolygon([QPointF(0, 0), QPointF(half, half), QPointF(0, half * 2), QPointF(-half, half)])

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._dragging = True
        self.move_started.emit()
        self.activated.emit()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            return
        new_start = max(0, scene_x_to_time_us(event.scenePos().x()))
        self._live_start_us = new_start
        self._sync_position()
        self.move_preview.emit(new_start)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self.move_finished.emit(self._live_start_us)
        self._dragging = False
        event.accept()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.context_menu_requested.emit(event.scenePos())
        event.accept()

    def _sync_position(self) -> None:
        self.setX(time_us_to_scene_x(self._live_start_us))
