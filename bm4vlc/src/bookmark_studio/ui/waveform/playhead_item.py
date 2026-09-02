"""PlayheadItem: interpolated 60Hz playback position marker (spec #31, #111)."""
from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem

from bookmark_studio.ui.waveform.waveform_item import time_us_to_scene_x

PLAYHEAD_COLOR = QColor(230, 60, 60)
TRACK_HEIGHT = 120


class PlayheadItem(QGraphicsItem):
    def __init__(self, height: float = TRACK_HEIGHT) -> None:
        super().__init__()
        self._height = height
        self._time_us = 0
        self.setZValue(100)

    def set_time_us(self, time_us: int) -> None:
        if time_us == self._time_us:
            return
        self.prepareGeometryChange()
        self._time_us = time_us
        self.setX(time_us_to_scene_x(time_us))

    def time_us(self) -> int:
        return self._time_us

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt override
        return QRectF(-1, 0, 2, self._height)

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
