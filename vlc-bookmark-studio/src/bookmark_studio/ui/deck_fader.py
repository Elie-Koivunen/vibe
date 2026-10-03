"""DJ-mixer style faders: the player's volume, and its tempo.

`DeckFader` is a vertical QSlider (0-125 %, VLC's own GUI range) painted like a mixer
channel: a dark strip with a scale, a lit slot up to the level, a marker at unity (100 %)
and a ribbed fader cap. Clicking anywhere moves the cap there and drags it; the wheel and
the arrow keys step 1 %, Page Up/Down 10 %, and a double-click returns to 100 %.
`VolumeStrip` adds the caption and the percentage readout.

`TempoFader` is the same fader for a change of tempo: -50 to +50 BPM in steps of 5, 0 in
the middle (lit from there to the cap; the 0 mark red while the tempo panel is skewed).
The panel around it is ui/tempo_panel.py.

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
_SKEWED = QColor(224, 64, 58)  # the tempo fader's middle, while skewed
DISABLED_OPACITY = 0.4


def level_to_percent(level: int) -> int:
    return round(max(0, level) * 100 / 256)


def percent_to_level(percent: int) -> int:
    return round(max(0, percent) * 256 / 100)


class DeckFader(QSlider):
    volume_changed = Signal(int)  # the value (percent), only for the user's own moves

    # The scale; a subclass sets its own (see TempoFader).
    MINIMUM = 0
    MAXIMUM = MAX_PERCENT
    HOME = UNITY_PERCENT  # the amber mark, where a double-click goes
    STEP = 1  # what a value snaps to; the wheel's and arrow keys' step
    PAGE = 10
    TICK_EVERY = 5
    LABEL_EVERY = 25
    LIT_FROM = 0  # the lit slot runs from here to the cap
    TOOLTIP = "Volume (drag, scroll or arrow keys; double-click for 100 %)"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self.setRange(self.MINIMUM, self.MAXIMUM)
        self.setSingleStep(self.STEP)
        self.setPageStep(self.PAGE)
        self.setValue(self.HOME)
        self.setFixedWidth(86)
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(self.TOOLTIP)
        self._dragging = False
        self.actionTriggered.connect(self._on_action)  # keyboard / wheel steps

    # -- values --

    def _clamped(self, value: int) -> int:
        value = round(value / self.STEP) * self.STEP
        return max(self.MINIMUM, min(self.MAXIMUM, value))

    def set_volume_percent(self, percent: int) -> None:
        """Shows the player's value. Ignored while the user is holding the cap."""
        if self._dragging:
            return
        self.blockSignals(True)
        self.setValue(self._clamped(percent))
        self.blockSignals(False)
        self.update()

    def _set_by_user(self, percent: int) -> None:
        percent = self._clamped(percent)
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
        return rect.bottom() - rect.height() * (percent - self.MINIMUM) / (self.MAXIMUM - self.MINIMUM)

    def _percent_at(self, y: float) -> int:
        rect = self._scale_rect()
        return round(self.MINIMUM + (rect.bottom() - y) / rect.height() * (self.MAXIMUM - self.MINIMUM))

    def _label(self, value: int) -> str:
        return str(value)

    def _home_colour(self) -> QColor:
        return _UNITY

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
        self._set_by_user(self.HOME)
        event.accept()

    # -- painting --

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(DISABLED_OPACITY)  # greyed out as a whole (the BPM switch, EQ off)
        width = self.width()
        centre_x = width / 2

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_PANEL)
        painter.drawRoundedRect(QRectF(0, 0, width, self.height()), 6, 6)

        # Scale: a tick every 5, long and labelled every 25; home (unity, 0 BPM) in amber.
        font = QFont(self.font())
        font.setPointSizeF(max(6.5, font.pointSizeF() - 2))
        painter.setFont(font)
        for percent in range(self.MINIMUM, self.MAXIMUM + 1, self.TICK_EVERY):
            y = self._y_for(percent)
            major = (percent - self.MINIMUM) % self.LABEL_EVERY == 0
            colour = self._home_colour() if percent == self.HOME else _SCALE
            painter.setPen(QPen(colour, 1.4 if major else 1))
            length = 9 if major else 5
            painter.drawLine(QPointF(centre_x - 8 - length, y), QPointF(centre_x - 8, y))
            painter.drawLine(QPointF(centre_x + 8, y), QPointF(centre_x + 8 + length, y))
            if major:
                label_rect = QRectF(2, y - 7, centre_x - _CAP_WIDTH / 2 - 4, 14)
                painter.drawText(label_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                 self._label(percent))

        # Slot, lit from LIT_FROM (the bottom; the middle for the tempo) to the cap.
        top, bottom = self._y_for(self.MAXIMUM), self._y_for(self.MINIMUM)
        slot = QRectF(centre_x - 3, top, 6, bottom - top)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_SLOT)
        painter.drawRoundedRect(slot, 3, 3)
        cap_y = self._y_for(self.value())
        lit_y = self._y_for(self.LIT_FROM)
        lit = QRectF(slot.left() + 1, min(cap_y, lit_y), slot.width() - 2, abs(lit_y - cap_y))
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


TEMPO_RANGE_BPM = 50
TEMPO_STEP_BPM = 5
DEFAULT_SONG_BPM = 120.0  # counted with when a song's BPM isn't known


class TempoFader(DeckFader):
    """The change of tempo, in BPM: -50 to +50 in steps of 5, 0 (the song's own) in the middle."""

    MINIMUM = -TEMPO_RANGE_BPM
    MAXIMUM = TEMPO_RANGE_BPM
    HOME = 0
    STEP = TEMPO_STEP_BPM
    PAGE = 2 * TEMPO_STEP_BPM
    TICK_EVERY = TEMPO_STEP_BPM
    LABEL_EVERY = 25
    LIT_FROM = 0
    TOOLTIP = "BPM change from the middle (drag, scroll or arrow keys, 5 BPM a step; double-click for the middle)"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._skewed = False

    def set_skewed(self, skewed: bool) -> None:
        """Its middle is off the song's detected BPM (Align skew): the 0 mark turns red."""
        if skewed != self._skewed:
            self._skewed = skewed
            self.update()

    def skewed(self) -> bool:
        return self._skewed

    def _home_colour(self) -> QColor:
        return _SKEWED if self._skewed else _UNITY

    def _label(self, value: int) -> str:
        return f"{value:+d}" if value else "0"


__all__ = ["DeckFader", "TempoFader", "VolumeStrip", "level_to_percent", "percent_to_level"]
