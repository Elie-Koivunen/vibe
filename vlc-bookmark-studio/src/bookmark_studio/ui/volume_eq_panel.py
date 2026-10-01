"""The "Volume & EQ" tab: the DJ volume fader, Max / Reset / Mute beside it, and a 10-band
equalizer with a preamp.

Max raises the volume to 100 % and Mute lowers it to silence, each over its own time
(while something plays; at once otherwise). Normalize (80 %) and Reset (50 %) bring it to
their own, adjustable level, gliding at Max's speed coming down and at Mute's going up.
The volume ramps run in Application.

The equalizer faders are painted like the volume fader (dark strip, lit slot, metal cap)
but lit from their neutral mark: 0 dB for a band, +12 dB for the preamp (VLC's neutral
level, see domain/equalizer.py). Click, drag, scroll or use the arrow keys; a double-click
returns a fader to neutral. While the equalizer is switched off its faders are greyed out
(VLC ignores changes then). A chosen preset (or Flat) glides there over the "Glide" time,
under the same rule as the volume ramps; a hand on a fader takes over at once.
"""
from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from bookmark_studio.domain.equalizer import (
    BAND_COUNT,
    MAX_DB,
    MIN_DB,
    NEUTRAL_PREAMP_DB,
    PRESETS,
    VLC_BANDS_HZ,
    EqualizerSettings,
    band_label,
)
from bookmark_studio.ui.deck_fader import _LIT, _PANEL, _SCALE, _SLOT, _UNITY, VolumeStrip

CUSTOM_PRESET = "Custom"
DEFAULT_RAMP_MS = 1500  # Max and Mute
DEFAULT_NORMALIZE_PERCENT = 80
DEFAULT_RESET_PERCENT = 50
DEFAULT_GLIDE_MS = 1000  # the faders moving to a chosen preset
_GLIDE_STEP_MS = 40
_STEPS_PER_DB = 10  # the slider counts tenths of a dB
_CAP_W = 22
_CAP_H = 12
_PAD = _CAP_H // 2 + 3


class EqFader(QSlider):
    """One equalizer fader, MIN_DB..MAX_DB in 0.1 dB steps."""

    db_changed = Signal(float)  # only for the user's own moves

    def __init__(self, neutral_db: float = 0.0, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self._neutral = neutral_db
        self.setRange(int(MIN_DB * _STEPS_PER_DB), int(MAX_DB * _STEPS_PER_DB))
        self.setSingleStep(5)  # 0.5 dB per arrow key / wheel notch
        self.setPageStep(20)  # 2 dB
        self.setValue(round(neutral_db * _STEPS_PER_DB))
        self.setMinimumSize(24, 96)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._dragging = False
        self.actionTriggered.connect(lambda _action: self._set_by_user(self.sliderPosition()))

    def sizeHint(self):  # noqa: N802 - Qt override
        hint = super().sizeHint()
        hint.setWidth(32)
        hint.setHeight(140)
        return hint

    def db(self) -> float:
        return self.value() / _STEPS_PER_DB

    def set_db(self, db: float) -> None:
        """From the settings or a preset: no signal; ignored while the user drags."""
        if self._dragging:
            return
        self.blockSignals(True)
        self.setValue(round(db * _STEPS_PER_DB))
        self.blockSignals(False)
        self.update()

    def _set_by_user(self, steps: int) -> None:
        steps = max(self.minimum(), min(self.maximum(), steps))
        if steps != self.value():
            self.blockSignals(True)
            self.setValue(steps)
            self.blockSignals(False)
            self.update()
            self.db_changed.emit(steps / _STEPS_PER_DB)

    # -- geometry --

    def _scale_rect(self) -> QRectF:
        return QRectF(0, _PAD, self.width(), max(1, self.height() - 2 * _PAD))

    def _y_for(self, db: float) -> float:
        rect = self._scale_rect()
        return rect.bottom() - rect.height() * (db - MIN_DB) / (MAX_DB - MIN_DB)

    def _steps_at(self, y: float) -> int:
        rect = self._scale_rect()
        db = MIN_DB + (rect.bottom() - y) / rect.height() * (MAX_DB - MIN_DB)
        return round(db * _STEPS_PER_DB)

    # -- mouse --

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._dragging = True
            self._set_by_user(self._steps_at(event.position().y()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._set_by_user(self._steps_at(event.position().y()))
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
        if self.isEnabled():
            self._set_by_user(round(self._neutral * _STEPS_PER_DB))
        event.accept()

    # -- painting --

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        cx = width / 2
        enabled = self.isEnabled()

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_PANEL)
        painter.drawRoundedRect(QRectF(0, 0, width, self.height()), 5, 5)

        for db in range(int(MIN_DB), int(MAX_DB) + 1, 5):
            y = self._y_for(db)
            major = db % 10 == 0
            painter.setPen(QPen(_SCALE, 1))
            length = 5 if major else 3
            painter.drawLine(QPointF(cx - 5 - length, y), QPointF(cx - 5, y))
            painter.drawLine(QPointF(cx + 5, y), QPointF(cx + 5 + length, y))
        neutral_y = self._y_for(self._neutral)
        painter.setPen(QPen(_UNITY, 1.5))
        painter.drawLine(QPointF(2, neutral_y), QPointF(width - 2, neutral_y))

        top, bottom = self._y_for(MAX_DB), self._y_for(MIN_DB)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_SLOT)
        painter.drawRoundedRect(QRectF(cx - 2.5, top, 5, bottom - top), 2.5, 2.5)
        cap_y = self._y_for(self.db())
        painter.setBrush(_LIT if enabled else _SCALE)
        lit_top, lit_bottom = sorted((cap_y, neutral_y))
        painter.drawRect(QRectF(cx - 1.5, lit_top, 3, lit_bottom - lit_top))

        cap = QRectF(cx - _CAP_W / 2, cap_y - _CAP_H / 2, _CAP_W, _CAP_H)
        gradient = QLinearGradient(cap.topLeft(), cap.bottomLeft())
        gradient.setColorAt(0.0, QColor(236, 238, 242) if enabled else QColor(150, 152, 158))
        gradient.setColorAt(1.0, QColor(120, 124, 134) if enabled else QColor(96, 98, 104))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(20, 20, 24), 1))
        painter.drawRoundedRect(cap, 3, 3)
        painter.setPen(QPen(QColor(255, 255, 255) if enabled else _SCALE, 1.5))
        painter.drawLine(QPointF(cap.left() + 3, cap_y), QPointF(cap.right() - 3, cap_y))
        if self.hasFocus():
            painter.setPen(QPen(_LIT, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(0.5, 0.5, width - 1, self.height() - 1), 5, 5)
        painter.end()


def _format_db(db: float) -> str:
    return f"{db:+.1f}" if abs(db) >= 0.05 else "0.0"


class VolumeEqPanel(QWidget):
    """The volume fader (VLC's 0-512 scale) and the equalizer, side by side."""

    volume_changed = Signal(int)
    equalizer_changed = Signal(object)  # EqualizerSettings
    max_requested = Signal()
    mute_requested = Signal()
    reset_requested = Signal()
    ramp_times_changed = Signal(int, int)  # Max ms, Mute ms
    normalize_requested = Signal()
    levels_changed = Signal(int, int)  # Normalize %, Reset %
    glide_ms_changed = Signal(int)  # how long a preset change takes

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = EqualizerSettings()
        self._supported = True

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        self.volume_strip = VolumeStrip(self)
        self.volume_strip.setToolTip("Player volume")
        self.volume_strip.volume_changed.connect(self.volume_changed.emit)
        layout.addWidget(self.volume_strip)
        layout.addWidget(self._build_ramp_column())

        divider = QFrame(self)
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(divider)

        eq = QVBoxLayout()
        eq.setSpacing(6)
        layout.addLayout(eq, 1)

        header = QHBoxLayout()
        self._enabled_check = QCheckBox("Equalizer", self)
        self._enabled_check.setStyleSheet("font-weight: 700;")
        self._enabled_check.setToolTip("Switch the player's equalizer on or off")
        self._enabled_check.toggled.connect(self._on_enabled_toggled)
        header.addWidget(self._enabled_check)
        header.addStretch(1)
        header.addWidget(QLabel("Preset", self))
        self._preset_combo = QComboBox(self)
        self._preset_combo.addItem(CUSTOM_PRESET)
        for name in PRESETS:
            self._preset_combo.addItem(name)
        self._preset_combo.setToolTip("VLC's equalizer presets")
        self._preset_combo.activated.connect(self._on_preset_chosen)
        header.addWidget(self._preset_combo)
        self._reset_button = QPushButton("Flat", self)
        self._reset_button.setToolTip("Back to flat (no change to the sound)")
        self._reset_button.clicked.connect(lambda: self._apply_preset("Flat"))
        header.addWidget(self._reset_button)
        header.addSpacing(8)
        header.addWidget(QLabel("Glide", self))
        self._glide_ms = QSpinBox(self)
        self._glide_ms.setRange(0, 60_000)
        self._glide_ms.setSingleStep(250)
        self._glide_ms.setSuffix(" ms")
        self._glide_ms.setSpecialValueText("At once")
        self._glide_ms.setValue(DEFAULT_GLIDE_MS)
        self._glide_ms.setToolTip(
            "How long the faders take to move to a chosen preset (or Flat), in ms -- while "
            "something plays, like Max and Mute; at once otherwise"
        )
        self._glide_ms.valueChanged.connect(self.glide_ms_changed.emit)
        header.addWidget(self._glide_ms)
        eq.addLayout(header)

        # A preset glides there: every step goes to the player like a fader drag.
        self._glide: tuple[EqualizerSettings, EqualizerSettings, int, int] | None = None
        self._glide_timer = QTimer(self)
        self._glide_timer.setInterval(_GLIDE_STEP_MS)
        self._glide_timer.timeout.connect(self._on_glide_tick)
        self._glide_allowed: Callable[[], bool] = lambda: True
        self._pending_preset: str | None = None  # shown in the preset list while gliding to it

        rack = QGridLayout()
        rack.setHorizontalSpacing(4)
        rack.setVerticalSpacing(2)
        self._preamp = EqFader(NEUTRAL_PREAMP_DB, self)
        self._preamp.setToolTip("Preamp (+12 dB is VLC's neutral level; double-click for it)")
        self._preamp.db_changed.connect(lambda db: self._on_user_edit(self._settings.with_preamp(db)))
        self._preamp_value = self._value_label()
        rack.addWidget(self._preamp_value, 0, 0)
        rack.addWidget(self._preamp, 1, 0, Qt.AlignmentFlag.AlignHCenter)
        rack.addWidget(self._caption("Pre"), 2, 0)
        rack.setColumnMinimumWidth(1, 6)
        self._bands: list[EqFader] = []
        self._band_values: list[QLabel] = []
        self._band_captions: list[QLabel] = []
        for band in range(BAND_COUNT):
            fader = EqFader(0.0, self)
            fader.db_changed.connect(lambda db, band=band: self._on_user_edit(self._settings.with_band(band, db)))
            value = self._value_label()
            caption = self._caption("")
            rack.addWidget(value, 0, band + 2)
            rack.addWidget(fader, 1, band + 2, Qt.AlignmentFlag.AlignHCenter)
            rack.addWidget(caption, 2, band + 2)
            self._bands.append(fader)
            self._band_values.append(value)
            self._band_captions.append(caption)
        rack.setRowStretch(1, 1)
        eq.addLayout(rack, 1)

        self._note = QLabel(self)
        self._note.setWordWrap(True)
        self._note.setStyleSheet("font-style: italic;")
        eq.addWidget(self._note)

        self.set_band_frequencies(VLC_BANDS_HZ)
        self._show(self._settings)

    def _build_ramp_column(self) -> QWidget:
        """Beside the fader, top to bottom: Max (with its ramp time), Normalize and Reset
        (each with the level it goes to: 80 % and 50 % to begin with), Mute (with its ramp
        time). The times work like a bookmark's fades: milliseconds, 0 = at once.
        Normalize and Reset glide at Max's speed coming down, at Mute's going up."""
        column = QWidget(self)
        column.setFixedWidth(100)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        def ramp_spin(tooltip: str) -> QSpinBox:
            spin = QSpinBox(column)
            spin.setRange(0, 60_000)
            spin.setSingleStep(250)
            spin.setSuffix(" ms")
            spin.setSpecialValueText("At once")
            spin.setValue(DEFAULT_RAMP_MS)
            spin.setToolTip(tooltip)
            spin.valueChanged.connect(lambda _value: self.ramp_times_changed.emit(*self.ramp_times()))
            return spin

        self._max_button = QPushButton("Max", column)
        self._max_button.setToolTip("Raise the volume to 100 % over the time below")
        self._max_button.clicked.connect(self.max_requested.emit)
        self._max_ms = ramp_spin("How long Max takes to raise the volume")

        def level_spin(value: int, tooltip: str) -> QSpinBox:
            spin = QSpinBox(column)
            spin.setRange(0, 125)  # the fader's range
            spin.setSuffix(" %")
            spin.setValue(value)
            spin.setToolTip(tooltip)
            spin.valueChanged.connect(lambda _value: self.levels_changed.emit(*self.levels()))
            return spin

        self._normalize_button = QPushButton("Normalize", column)
        self._normalize_button.setToolTip(
            "Bring the volume to the level below -- gliding at Max's speed coming down, at "
            "Mute's speed going up (at once when nothing plays)"
        )
        self._normalize_button.clicked.connect(self.normalize_requested.emit)
        self._normalize_level = level_spin(DEFAULT_NORMALIZE_PERCENT, "The level Normalize brings the volume to")
        self._volume_reset_button = QPushButton("Reset", column)
        self._volume_reset_button.setToolTip(
            "Bring the volume to the level below -- gliding at Max's speed coming down, at "
            "Mute's speed going up (at once when nothing plays)"
        )
        self._volume_reset_button.clicked.connect(self.reset_requested.emit)
        self._reset_level = level_spin(DEFAULT_RESET_PERCENT, "The level Reset brings the volume to")
        self._mute_ms = ramp_spin("How long Mute takes to lower the volume (while something plays)")
        self._mute_button = QPushButton("Mute", column)
        self._mute_button.setToolTip("Lower the volume to silence over the time above")
        self._mute_button.clicked.connect(self.mute_requested.emit)

        layout.addSpacing(18)  # level with the fader's top
        layout.addWidget(self._max_button)
        layout.addWidget(self._max_ms)
        layout.addStretch(1)
        layout.addWidget(self._normalize_button)
        layout.addWidget(self._normalize_level)
        layout.addSpacing(4)
        layout.addWidget(self._volume_reset_button)
        layout.addWidget(self._reset_level)
        layout.addStretch(1)
        layout.addWidget(self._mute_ms)
        layout.addWidget(self._mute_button)
        layout.addSpacing(22)  # level with the fader's bottom, above its readout
        return column

    def ramp_times(self) -> tuple[int, int]:
        """(Max, Mute) ramp times in ms."""
        return self._max_ms.value(), self._mute_ms.value()

    def set_ramp_times(self, max_ms: int, mute_ms: int) -> None:
        """Saved ramp times; no ramp_times_changed."""
        for spin, value in ((self._max_ms, max_ms), (self._mute_ms, mute_ms)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def levels(self) -> tuple[int, int]:
        """(Normalize, Reset) levels in percent."""
        return self._normalize_level.value(), self._reset_level.value()

    def set_levels(self, normalize_percent: int, reset_percent: int) -> None:
        """Saved levels; no levels_changed."""
        for spin, value in ((self._normalize_level, normalize_percent), (self._reset_level, reset_percent)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    @staticmethod
    def _value_label() -> QLabel:
        label = QLabel("0.0")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("font-size: 8pt;")
        return label

    @staticmethod
    def _caption(text: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("font-size: 8pt; font-weight: 600;")
        return label

    # -- volume --

    def set_level(self, level: int) -> None:
        self.volume_strip.set_level(level)

    def level(self) -> int:
        return self.volume_strip.level()

    def focus_volume(self) -> None:
        self.volume_strip.fader.setFocus()

    # -- equalizer --

    def equalizer(self) -> EqualizerSettings:
        return self._settings

    def set_equalizer(self, settings: EqualizerSettings) -> None:
        """Shows saved settings; no signal."""
        self._stop_glide()
        self._settings = settings
        self._show(settings)

    def set_band_frequencies(self, band_hz: tuple[float, ...]) -> None:
        """Band captions for the connected player (a VLC window and libVLC differ)."""
        for caption, hz in zip(self._band_captions, band_hz):
            caption.setText(band_label(hz))
        for fader, hz in zip(self._bands, band_hz):
            fader.setToolTip(f"{band_label(hz)}Hz band (double-click for 0 dB)")

    def set_equalizer_supported(self, supported: bool, note: str = "") -> None:
        self._supported = supported
        self._note.setText(note)
        self._note.setVisible(bool(note))
        self._show(self._settings)

    def _on_enabled_toggled(self, checked: bool) -> None:
        self._on_user_edit(self._settings.with_enabled(checked))

    def _on_preset_chosen(self, index: int) -> None:
        name = self._preset_combo.itemText(index)
        if name in PRESETS:
            self._apply_preset(name)
        else:  # "Custom" is only a label for hand-made settings
            self._show(self._settings)

    def _apply_preset(self, name: str) -> None:
        self.glide_to(EqualizerSettings.from_preset(name, enabled=self._settings.enabled))

    # -- gliding to a preset --

    def glide_ms(self) -> int:
        return self._glide_ms.value()

    def set_glide_ms(self, value_ms: int) -> None:
        """The saved glide time; no glide_ms_changed."""
        self._glide_ms.blockSignals(True)
        self._glide_ms.setValue(value_ms)
        self._glide_ms.blockSignals(False)

    def set_glide_condition(self, allowed: Callable[[], bool]) -> None:
        """When a preset may glide (Application: while something plays); at once otherwise."""
        self._glide_allowed = allowed

    def is_gliding(self) -> bool:
        return self._glide is not None

    def glide_to(self, target: EqualizerSettings) -> None:
        self._stop_glide()
        duration_ms = self._glide_ms.value()
        if duration_ms <= 0 or target == self._settings or not self._glide_allowed():
            self._on_user_edit(target)
            return
        self._pending_preset = target.matching_preset()
        self._glide = (self._settings, target, time.monotonic_ns(), duration_ms * 1_000_000)
        self._show(self._settings)  # the preset list shows where it is going
        self._glide_timer.start()

    def _on_glide_tick(self) -> None:
        if self._glide is None:
            self._glide_timer.stop()
            return
        start, target, started_ns, duration_ns = self._glide
        fraction = min(1.0, (time.monotonic_ns() - started_ns) / duration_ns)
        step = target if fraction >= 1.0 else start.blend(target, fraction)
        if fraction >= 1.0:
            self._stop_glide()
        if step != self._settings:
            self._settings = step
            self._show(step)
            self.equalizer_changed.emit(step)

    def _stop_glide(self) -> None:
        self._glide = None
        self._glide_timer.stop()
        self._pending_preset = None

    def _on_user_edit(self, settings: EqualizerSettings) -> None:
        self._stop_glide()  # a hand on a fader (or the switch) takes over from a glide
        if settings == self._settings:
            self._show(settings)
            return
        self._settings = settings
        self._show(settings)
        self.equalizer_changed.emit(settings)

    def _show(self, settings: EqualizerSettings) -> None:
        self._enabled_check.blockSignals(True)
        self._enabled_check.setChecked(settings.enabled)
        self._enabled_check.blockSignals(False)
        self._enabled_check.setEnabled(self._supported)
        active = self._supported and settings.enabled
        self._preamp.set_db(settings.preamp_db)
        self._preamp_value.setText(f"{settings.preamp_db:.1f}")
        for fader, value, db in zip(self._bands, self._band_values, settings.bands_db):
            fader.set_db(db)
            value.setText(_format_db(db))
        for widget in (self._preamp, *self._bands, self._preset_combo, self._reset_button):
            widget.setEnabled(active)
        preset = self._pending_preset or settings.matching_preset() or CUSTOM_PRESET
        self._preset_combo.setCurrentIndex(max(0, self._preset_combo.findText(preset)))


__all__ = ["CUSTOM_PRESET", "EqFader", "VolumeEqPanel"]
