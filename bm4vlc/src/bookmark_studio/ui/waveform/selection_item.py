"""SelectionItem: temporary paint-to-select highlight with live start/end/duration (spec #37)."""
from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem

from bookmark_studio.domain.selection import Selection
from bookmark_studio.ui.waveform.waveform_item import time_us_to_scene_x

# Direct user request: "when highlighting to bookmark, its still in blue, change
# to greenish tint" -- a drag-selection destined to become a bookmark.
SELECTION_FILL = QColor(90, 200, 120, 80)
SELECTION_BORDER = QColor(90, 200, 120, 200)


class SelectionItem(QGraphicsItem):
    def __init__(self, height: float, selection: Selection) -> None:
        super().__init__()
        self._height = height
        self._selection = selection
        self.setZValue(50)
        self._sync_position()

    def selection(self) -> Selection:
        return self._selection

    def set_selection(self, selection: Selection) -> None:
        self.prepareGeometryChange()
        self._selection = selection
        self._sync_position()

    def _sync_position(self) -> None:
        self.setX(time_us_to_scene_x(self._selection.start_us))

    def boundingRect(self) -> QRectF:  # noqa: N802
        width = time_us_to_scene_x(self._selection.end_us) - time_us_to_scene_x(self._selection.start_us)
        return QRectF(0, 0, max(width, 0.0), self._height)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        painter.setBrush(QBrush(SELECTION_FILL))
        painter.setPen(QPen(SELECTION_BORDER, 1))
        painter.drawRect(self.boundingRect())
