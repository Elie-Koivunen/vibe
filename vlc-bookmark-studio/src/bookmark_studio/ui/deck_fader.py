"""A DJ-mixer style channel fader for the player's volume.

`DeckFader` is a vertical QSlider (0-125 %, VLC's own GUI range) painted like a mixer
channel: a dark strip with a scale, a lit slot up to the level, a marker at unity (100 %)
and a ribbed fader cap. Clicking anywhere moves the cap there and drags it; the wheel and
the arrow keys step 1 %, Page Up/Down 10 %, and a double-click returns to 100 %.
`VolumeStrip` adds the caption and the percentage readout.

Volumes cross the app's boundary on VLC's 0-512 scale (256 = 100 %).
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QLabel, QSizePolicy, QSlider, QVBoxLayout, QWidget

MAX_PERCENT = 125
UNITY_PERCENT = 100
_CAP_HEIGHT = 26
_CAP_WIDTH = 40
_PAD = _CAP_HEIGHT // 2 + 4  # the cap's centre never leaves the scale

_PANEL = QColor(34, 36, 42)
_SLOT = QColor(12, 13, 16)
_LIT = QColor(46, 160, 255)
_SCALE = QColor(150, 156, 168)
_UNITY = QColor(255, 176, 60)


def level_to_percent(level: int) -> int:
    return round(max(0, level) * 100 / 256)


def percent_to_level(percent: int) -> int:
    return round(max(0, percent) * 256 / 100)


class DeckFader(QSlider):
    volume_changed = Signal(int)  # percent, only for the user's own moves

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self.setRange(0, MAX_PERCENT)
        self.setSingleStep(1)
        self.setPageStep(10)
        self.setValue(UNITY_PERCENT)
        self.setFixedWidth(86)
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip("Volume (drag, scroll or arrow keys; double-click for 100 %)")
        self._dragging = False
        self.actionTriggered.connect(self._on_action)  # keyboard / wheel steps

    # -- values --

    def set_volume_percent(self, percent: int) -> None:
        """Shows the player's volume. Ignored while the user is holding the cap."""
        if self._dragging:
            return
        self.blockSignals(True)
        self.setValue(max(0, min(MAX_PERCENT, percent)))
        self.blockSignals(False)
        self.update()

    def _set_by_user(self, percent: int) -> None:
        percent = max(0, min(MAX_PERCENT, percent))
        if percent != self.value():
            self.blockSignals(True)
            self.setValue(percent)
            self.blockSignals(False)
            self.update()
            self.volume_changed.emit(percent)

    def _on_action(self, _action: int) -> None:
        # QSlider has already applied the step to sliderPosition(); report the result.
        self._set_by_user(self.sliderPosition())

    # -- geometry --

    def _scale_rect(self) -> QRectF:
        return QRectF(0, _PAD, self.width(), max(1, self.height() - 2 * _PAD))

    def _y_for(self, percent: float) -> float:
        rect = self._scale_rect()
        return rect.bottom() - rect.height() * percent / MAX_PERCENT

    def _percent_at(self, y: float) -> int:
        rect = self._scale_rect()
        return round((rect.bottom() - y) / rect.height() * MAX_PERCENT)

    # -- mouse: the cap goes where you click, then follows the pointer --

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._set_by_user(self._percent_at(event.position().y()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._set_by_user(self._percent_at(event.position().y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._set_by_user(UNITY_PERCENT)
        event.accept()

    # -- painting --

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        centre_x = width / 2

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_PANEL)
        painter.drawRoundedRect(QRectF(0, 0, width, self.height()), 6, 6)

        # Scale: a tick every 5 %, long and labelled every 25 %; unity in amber.
        font = QFont(self.font())
        font.setPointSizeF(max(6.5, font.pointSizeF() - 2))
        painter.setFont(font)
        for percent in range(0, MAX_PERCENT + 1, 5):
            y = self._y_for(percent)
            major = percent % 25 == 0
            colour = _UNITY if percent == UNITY_PERCENT else _SCALE
            painter.setPen(QPen(colour, 1.4 if major else 1))
            length = 9 if major else 5
            painter.drawLine(QPointF(centre_x - 8 - length, y), QPointF(centre_x - 8, y))
            painter.drawLine(QPointF(centre_x + 8, y), QPointF(centre_x + 8 + length, y))
            if major:
                label_rect = QRectF(2, y - 7, centre_x - _CAP_WIDTH / 2 - 4, 14)
                painter.drawText(label_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(percent))

        # Slot, lit from the bottom up to the level.
        top, bottom = self._y_for(MAX_PERCENT), self._y_for(0)
        slot = QRectF(centre_x - 3, top, 6, bottom - top)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_SLOT)
        painter.drawRoundedRect(slot, 3, 3)
        cap_y = self._y_for(self.value())
        lit = QRectF(slot.left() + 1, cap_y, slot.width() - 2, bottom - cap_y)
        painter.setBrush(_LIT if self.isEnabled() else _SCALE)
        painter.drawRoundedRect(lit, 2, 2)

        # Fader cap: brushed-metal gradient, grip ribs, a bright index line.
        cap = QRectF(centre_x - _CAP_WIDTH / 2, cap_y - _CAP_HEIGHT / 2, _CAP_WIDTH, _CAP_HEIGHT)
        gradient = QLinearGradient(cap.topLeft(), cap.bottomLeft())
        gradient.setColorAt(0.0, QColor(236, 238, 242))
        gradient.setColorAt(0.45, QColor(186, 190, 198))
        gradient.setColorAt(0.55, QColor(170, 174, 184))
        gradient.setColorAt(1.0, QColor(120, 124, 134))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(20, 20, 24), 1))
        painter.drawRoundedRect(cap, 4, 4)
        painter.setPen(QPen(QColor(95, 99, 108), 1))
        for offset in (-8, -5, 5, 8):
            painter.drawLine(QPointF(cap.left() + 5, cap_y + offset), QPointF(cap.right() - 5, cap_y + offset))
        painter.setPen(QPen(QColor(255, 255, 255) if self.isEnabled() else _SCALE, 2))
        painter.drawLine(QPointF(cap.left() + 3, cap_y), QPointF(cap.right() - 3, cap_y))
        if self.hasFocus():
            painter.setPen(QPen(_LIT, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(0.5, 0.5, width - 1, self.height() - 1), 6, 6)
        painter.end()


class VolumeStrip(QWidget):
    """The fader with its caption and readout. `volume_changed` carries VLC's 0-512
    scale (256 = 100 %)."""

    volume_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 0, 0)
        layout.setSpacing(4)
        caption = QLabel("VOL", self)
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setStyleSheet("font-weight: 700; letter-spacing: 1px;")
        self.fader = DeckFader(self)
        self._readout = QLabel(self)
        self._readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._readout.setStyleSheet("font-weight: 700;")
        self._readout.setMinimumWidth(48)
        layout.addWidget(caption)
        layout.addWidget(self.fader, 1, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._readout)
        self.fader.volume_changed.connect(self._on_user_volume)
        self._show_percent(self.fader.value())

    def set_level(self, level: int) -> None:
        """Shows the player's volume (0-512)."""
        self.fader.set_volume_percent(level_to_percent(level))
        self._show_percent(self.fader.value())

    def level(self) -> int:
        return percent_to_level(self.fader.value())

    def _on_user_volume(self, percent: int) -> None:
        self._show_percent(percent)
        self.volume_changed.emit(percent_to_level(percent))

    def _show_percent(self, percent: int) -> None:
        self._readout.setText(f"{percent} %")


__all__ = ["DeckFader", "VolumeStrip", "level_to_percent", "percent_to_level"]
