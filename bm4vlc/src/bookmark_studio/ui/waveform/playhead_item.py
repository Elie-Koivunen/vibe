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

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt override
        return QRectF(-1, 0, 2, self._height)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        painter.setPen(QPen(PLAYHEAD_COLOR, 1.5))
        painter.drawLine(0, 0, 0, self._height)
