"""A sliding on/off switch (0.9.0: switches the BPM panel on and off)."""
from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QWidget

from bookmark_studio.ui.qt_helpers import SELECTED_TAB_COLOR

_ON = QColor(SELECTED_TAB_COLOR)  # the app's orange
_OFF = QColor(150, 150, 150)
_KNOB = QColor(255, 255, 255)
_SLIDE_MS = 120


class ToggleSwitch(QAbstractButton):
    """Checkable: on (orange, the knob right) or off (grey, the knob left). A click or the
    space bar slides it over; setChecked() moves it the same way."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._knob = 0.0  # 0: off .. 1: on (animated in between)
        self._slide = QPropertyAnimation(self, b"knob", self)
        self._slide.setDuration(_SLIDE_MS)
        self._slide.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.toggled.connect(self._on_toggled)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(38, 20)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def _get_knob(self) -> float:
        return self._knob

    def _set_knob(self, value: float) -> None:
        self._knob = value
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def _on_toggled(self, checked: bool) -> None:
        target = 1.0 if checked else 0.0
        self._slide.stop()
        if self.isVisible():
            self._slide.setStartValue(self._knob)
            self._slide.setEndValue(target)
            self._slide.start()
        else:
            self._set_knob(target)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = track.height() / 2
        colour = QColor(
            round(_OFF.red() + (_ON.red() - _OFF.red()) * self._knob),
            round(_OFF.green() + (_ON.green() - _OFF.green()) * self._knob),
            round(_OFF.blue() + (_ON.blue() - _OFF.blue()) * self._knob),
        )
        if not self.isEnabled():
            colour.setAlpha(110)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawRoundedRect(track, radius, radius)
        diameter = track.height() - 4
        x = track.left() + 2 + (track.width() - 4 - diameter) * self._knob
        painter.setBrush(_KNOB)
        painter.setPen(QPen(colour.darker(130), 1))
        painter.drawEllipse(QRectF(x, track.top() + 2, diameter, diameter))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(self.palette().highlight().color(), 1, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(track.adjusted(-1, -1, 1, 1), radius + 1, radius + 1)
