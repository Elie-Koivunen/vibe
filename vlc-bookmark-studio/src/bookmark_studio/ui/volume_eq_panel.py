"""The "Volume & EQ" tab: the DJ volume fader and a 10-band equalizer with a preamp.

The equalizer faders are painted like the volume fader (dark strip, lit slot, metal cap)
but lit from their neutral mark: 0 dB for a band, +12 dB for the preamp (VLC's neutral
level, see domain/equalizer.py). Click, drag, scroll or use the arrow keys; a double-click
returns a fader to neutral. While the equalizer is switched off its faders are greyed out
(VLC ignores changes then).
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
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
        eq.addLayout(header)

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
        self._on_user_edit(EqualizerSettings.from_preset(name, enabled=self._settings.enabled))

    def _on_user_edit(self, settings: EqualizerSettings) -> None:
        if settings == self._settings:
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
        preset = settings.matching_preset() or CUSTOM_PRESET
        self._preset_combo.setCurrentIndex(max(0, self._preset_combo.findText(preset)))


__all__ = ["CUSTOM_PRESET", "EqFader", "VolumeEqPanel"]
