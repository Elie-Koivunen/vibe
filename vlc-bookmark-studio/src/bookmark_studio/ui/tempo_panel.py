"""The BPM panel of the Volume & EQ tab (0.9.0; first called Tempo):

    BPM (on/off)   BPM Skew      +10      <- red while off the Detected BPM
    [Align skew]   Current BPM   130
    ┃ fader ┃      Detected BPM  120      <- double-click to correct
    ┃  ±50  ┃      [ Increase ]
    ┃   0 ━━┃      [  Reset   ]           <- level with the fader's 0
    ┃       ┃      [  Lower   ]
                   Step  [5 BPM]   Glide [2000 ms]

BPM Skew is how far what plays (the Current BPM) is from the song's Detected BPM. The
fader is the change from its middle -- the Detected BPM until Align skew makes the
Current BPM its middle (nothing you hear changes; the fader goes back to its middle, the
button and the fader's 0 turn red). Reset returns to the Detected BPM and cancels both.
Increase and Lower move by the Step; all three glide over the Glide time (Application).
Switched off, the song plays at its own tempo and the panel is greyed out (nothing in it
can be changed by accident); switched on, the tempo set plays again. None of it is saved
with a bookmark; the switch, Step and Glide are remembered."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from bookmark_studio.settings.settings_service import TempoButtons
from bookmark_studio.ui.deck_fader import DEFAULT_SONG_BPM, TempoFader
from bookmark_studio.ui.toggle_switch import ToggleSwitch

TEMPO_FADER_HEIGHT = 190  # shorter than the volume fader
SKEWED_STYLE = "color: #e0403a; font-weight: 700;"
SKEWED_BUTTON_STYLE = "QPushButton { color: #e0403a; border: 1px solid #e0403a; font-weight: 700; }"


def bpm_text(bpm: float) -> str:
    return f"{bpm:.0f}" if abs(bpm - round(bpm)) < 0.05 else f"{bpm:.1f}"


def signed_bpm_text(bpm: float) -> str:
    """+10, -2.5, 0."""
    text = bpm_text(abs(bpm))
    if abs(bpm) < 0.05:
        return "0"
    return ("+" if bpm > 0 else "-") + text


class _DoubleClickLabel(QLabel):
    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.double_clicked.emit()
        event.accept()


AskBpm = Callable[[float], "float | None"]  # current value -> the user's, or None


class TempoPanel(QWidget):
    fader_moved = Signal(int)  # the fader's value: the change from the BPM Skew
    align_skew_requested = Signal()
    increase_requested = Signal()
    reset_requested = Signal()
    lower_requested = Signal()
    detected_bpm_corrected = Signal(float)
    buttons_changed = Signal(object)  # TempoButtons (Step, Glide)
    switched = Signal(bool)  # the on/off switch, by the user

    def __init__(self, parent: QWidget | None = None, *, ask_bpm: AskBpm | None = None) -> None:
        super().__init__(parent)
        self._ask_bpm = ask_bpm or self._ask_bpm_dialog
        self._detected: float | None = None
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)

        # The caption, and beside it the switch that turns the whole panel on and off.
        caption = QWidget(self)
        caption_layout = QHBoxLayout(caption)
        caption_layout.setContentsMargins(0, 0, 0, 0)
        caption_layout.setSpacing(6)
        title = QLabel("BPM", caption)
        title.setStyleSheet("font-weight: 700; letter-spacing: 1px;")
        self.switch = ToggleSwitch(caption)
        self.switch.setToolTip("BPM on/off -- off, the song plays at its own tempo and nothing here can be "
                               "changed by accident")
        self.switch.toggled.connect(self._on_switch_toggled)
        self._quiet = False  # set_tempo_enabled: no `switched`
        caption_layout.addStretch(1)
        caption_layout.addWidget(title)
        caption_layout.addWidget(self.switch)
        caption_layout.addStretch(1)
        self.fader = TempoFader(self)
        self.fader.setFixedHeight(TEMPO_FADER_HEIGHT)
        self.fader.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.fader.volume_changed.connect(self.fader_moved.emit)  # (the fader's "moved by the user")
        self.align_skew_button = QPushButton("Align skew", self)
        self.align_skew_button.setFixedWidth(self.fader.maximumWidth())  # (the fader is fixed-width)
        self.align_skew_button.setToolTip("The fader's middle becomes the Current BPM -- nothing you hear "
                                          "changes; the fader's ±50 is around it from then on")
        self.align_skew_button.clicked.connect(self.align_skew_requested.emit)

        readings = QGridLayout()
        readings.setHorizontalSpacing(10)
        readings.setVerticalSpacing(2)
        self._skew_label = QLabel("BPM Skew", self)
        self._skew_value = QLabel(self)
        self._skew_label.setToolTip("How far the Current BPM is from the Detected BPM")
        self._skew_value.setToolTip(self._skew_label.toolTip())
        self._current_value = QLabel(self)
        self._detected_value = _DoubleClickLabel(self)
        self._detected_value.setToolTip("The song's own tempo, measured from its waveform -- double-click to "
                                        "correct it")
        self._detected_value.double_clicked.connect(self._on_detected_double_clicked)
        detected_label = _DoubleClickLabel("Detected BPM", self)
        detected_label.setToolTip(self._detected_value.toolTip())
        detected_label.double_clicked.connect(self._on_detected_double_clicked)
        current_label = QLabel("Current BPM", self)
        for row, (label, value) in enumerate(((self._skew_label, self._skew_value),
                                              (current_label, self._current_value),
                                              (detected_label, self._detected_value))):
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value.setMinimumWidth(44)
            readings.addWidget(label, row, 0)
            readings.addWidget(value, row, 1)
        self._current_value.setStyleSheet("font-weight: 700;")

        stack = QWidget(self)
        stack_layout = QVBoxLayout(stack)
        stack_layout.setContentsMargins(0, 0, 0, 0)
        stack_layout.setSpacing(2)
        self.increase_button = QPushButton("Increase", stack)
        self.increase_button.setToolTip("One Step faster, over the Glide time")
        self.increase_button.clicked.connect(self.increase_requested.emit)
        self.reset_button = QPushButton("Reset", stack)
        self.reset_button.setToolTip("Back to the Detected BPM (the skew cancelled), over the Glide time")
        self.reset_button.clicked.connect(self.reset_requested.emit)
        self.lower_button = QPushButton("Lower", stack)
        self.lower_button.setToolTip("One Step slower, over the Glide time")
        self.lower_button.clicked.connect(self.lower_requested.emit)
        for button in (self.increase_button, self.reset_button, self.lower_button):
            stack_layout.addWidget(button)

        settings = QGridLayout()
        settings.setHorizontalSpacing(8)
        settings.setVerticalSpacing(4)
        defaults = TempoButtons()
        self._step = QSpinBox(self)
        self._step.setRange(1, 50)
        self._step.setSuffix(" BPM")
        self._step.setValue(defaults.step_bpm)
        self._step.setToolTip("How much Increase and Lower change the tempo")
        self._glide_ms = QSpinBox(self)
        self._glide_ms.setRange(0, 60_000)
        self._glide_ms.setSingleStep(250)
        self._glide_ms.setSuffix(" ms")
        self._glide_ms.setSpecialValueText("At once")
        self._glide_ms.setValue(defaults.glide_ms)
        self._glide_ms.setToolTip("How long Increase, Reset and Lower take to get there "
                                  "(while something plays; at once otherwise)")
        for spin in (self._step, self._glide_ms):
            spin.setMinimumWidth(96)
            spin.valueChanged.connect(lambda _value: self.buttons_changed.emit(self.buttons()))
        step_label, glide_label = QLabel("Step", self), QLabel("Glide", self)
        settings.addWidget(step_label, 0, 0)
        settings.addWidget(self._step, 0, 1)
        settings.addWidget(glide_label, 1, 0)
        settings.addWidget(self._glide_ms, 1, 1)
        # Everything the switch greys out.
        self._switched_widgets: list[QWidget] = [
            self.align_skew_button, self.fader, self._skew_label, self._skew_value, current_label,
            self._current_value, detected_label, self._detected_value, stack, step_label, self._step,
            glide_label, self._glide_ms,
        ]

        grid.addWidget(caption, 0, 0)
        grid.addLayout(readings, 0, 1, 2, 1, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.align_skew_button, 1, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter)
        grid.addWidget(self.fader, 2, 0, Qt.AlignmentFlag.AlignHCenter)
        grid.addWidget(stack, 2, 1, Qt.AlignmentFlag.AlignVCenter)  # Reset level with the fader's 0
        grid.addLayout(settings, 3, 1)
        grid.setRowStretch(4, 1)
        self.show_tempo(None, 0.0, 0.0)
        self.set_tempo_enabled(False)

    # -- on / off --

    def set_tempo_enabled(self, enabled: bool) -> None:
        """The saved state (no `switched`)."""
        self._quiet = True
        try:
            self.switch.setChecked(enabled)
        finally:
            self._quiet = False
        self._grey_out(enabled)

    def is_tempo_enabled(self) -> bool:
        return self.switch.isChecked()

    def _on_switch_toggled(self, enabled: bool) -> None:
        self._grey_out(enabled)
        if not self._quiet:
            self.switched.emit(enabled)

    def _grey_out(self, enabled: bool) -> None:
        for widget in self._switched_widgets:
            widget.setEnabled(enabled)

    # -- what plays --

    def show_tempo(self, detected: float | None, skew: float, change: float) -> None:
        """`detected`: the song's BPM (None: not detected -- counted as 120); `skew`: where
        the fader's middle is, from the detected BPM; `change`: what plays, from it -- the
        BPM Skew shown."""
        self._detected = detected
        base = detected if detected is not None else DEFAULT_SONG_BPM
        off = abs(change) >= 0.05
        aligned = abs(skew) >= 0.05  # the fader's middle isn't the detected BPM
        self._skew_value.setText(signed_bpm_text(change))
        self._current_value.setText(bpm_text(base + change))
        self._detected_value.setText(bpm_text(detected) if detected is not None else "–")
        style = SKEWED_STYLE if off else ""
        self._skew_value.setStyleSheet(style)
        self._skew_label.setStyleSheet(style)
        self.align_skew_button.setStyleSheet(SKEWED_BUTTON_STYLE if aligned else "")
        self.fader.set_skewed(aligned)
        self.fader.set_volume_percent(round(change - skew))

    def readings(self) -> tuple[str, str, str]:
        """(BPM Skew, Current BPM, Detected BPM) as shown."""
        return self._skew_value.text(), self._current_value.text(), self._detected_value.text()

    def is_shown_skewed(self) -> bool:
        """The BPM Skew in red: what plays is off the detected BPM."""
        return bool(self._skew_value.styleSheet())

    def is_shown_aligned(self) -> bool:
        """Align skew's button and the fader's 0 in red: its middle isn't the detected BPM."""
        return self.fader.skewed()

    # -- the detected BPM, put right by hand --

    def _on_detected_double_clicked(self) -> None:
        current = self._detected if self._detected is not None else DEFAULT_SONG_BPM
        bpm = self._ask_bpm(current)
        if bpm is not None and bpm > 0:
            self.detected_bpm_corrected.emit(float(bpm))

    def _ask_bpm_dialog(self, current: float) -> float | None:
        bpm, ok = QInputDialog.getDouble(self, "Detected BPM", "The song's own tempo (BPM):", current, 30.0, 300.0, 1)
        return bpm if ok else None

    # -- Step and Glide --

    def buttons(self) -> TempoButtons:
        return TempoButtons(step_bpm=self._step.value(), glide_ms=self._glide_ms.value())

    def set_buttons(self, buttons: TempoButtons) -> None:
        """The saved values (no buttons_changed)."""
        for spin, value in ((self._step, buttons.step_bpm), (self._glide_ms, buttons.glide_ms)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
