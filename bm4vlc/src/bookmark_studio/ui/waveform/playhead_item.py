"""PlayheadItem: interpolated 60Hz playback position marker (spec #31, #111),
draggable to seek -- direct user request: "make it usable where as a user can move
it and the song would start from there when played".
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from bookmark_studio.ui.waveform.waveform_item import scene_x_to_time_us, time_us_to_scene_x

PLAYHEAD_COLOR = QColor(230, 60, 60)
TRACK_HEIGHT = 120
HIT_WIDTH_PX = 10.0  # grabbable width in real device pixels -- wider than the visible 2px line


def _view_scale_x(item: QGraphicsItem) -> float:
    """Same fix as bookmark_item.py's resize handles: a fixed scene-unit hit width
    only corresponds to a constant number of real screen pixels at 1:1 zoom. Once
    fit_entire_media() zooms out, a fixed-width hit area would shrink to sub-pixel
    and become impossible to grab. Divides a target device-pixel width through the
    view's current horizontal scale instead.
    """
    scene = item.scene()
    if scene is None:
        return 1.0
    views = scene.views()
    if not views:
        return 1.0
    scale = views[0].transform().m11()
    return scale if scale > 0 else 1.0


class PlayheadItem(QGraphicsObject):
    seek_requested = Signal(int)  # microseconds -- emitted once, when the drag ends

    def __init__(self, height: float = TRACK_HEIGHT) -> None:
        super().__init__()
        self._height = height
        self._time_us = 0
        self._dragging = False
        self.setZValue(100)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setCursor(Qt.SizeHorCursor)

    def set_time_us(self, time_us: int) -> None:
        if self._dragging:
            # Don't let the ~400ms status poll's own update fight the user's drag --
            # it would otherwise snap the line back to the pre-seek position mid-drag.
            return
        if time_us == self._time_us:
            return
        self.prepareGeometryChange()
        self._time_us = time_us
        self.setX(time_us_to_scene_x(time_us))

    def time_us(self) -> int:
        return self._time_us

    def _hit_half_width(self) -> float:
        return max(1.0, HIT_WIDTH_PX / _view_scale_x(self))

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt override
        half = self._hit_half_width()
        return QRectF(-half, 0, half * 2, self._height)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        # A non-cosmetic pen's width is in the item's own SCENE units, same device-
        # pixel-vs-scene-coordinate bug fixed elsewhere in this package (ruler ticks,
        # waveform pyramid selection, bookmark resize handles). fit_entire_media() on
        # anything longer than a few seconds scales the view down so far that 1.5
        # scene-ms of width rounds to a fraction of a real screen pixel -- the moving
        # position marker was reported live as simply never visible. setCosmetic(True)
        # is Qt's built-in fix for exactly this: the pen's width is then always in
        # real device pixels, regardless of the view's current zoom.
        pen = QPen(PLAYHEAD_COLOR, 2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLine(0, 0, 0, self._height)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._dragging = True
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            return
        new_time_us = max(0, scene_x_to_time_us(event.scenePos().x()))
        self.prepareGeometryChange()
        self._time_us = new_time_us
        self.setX(time_us_to_scene_x(new_time_us))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._dragging = False
            self.seek_requested.emit(self._time_us)
        event.accept()
