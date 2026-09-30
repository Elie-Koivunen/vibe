"""Transport bar: prev/next bookmark, prev/next track, play/pause/stop, seek (spec #137)."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal, SignalInstance
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bookmark_studio.domain.timecode import format_timecode as format_timecode  # re-exported
from bookmark_studio.domain.timecode import parse_timecode as parse_timecode  # re-exported

BUTTON_FONT_POINT_SIZE = 16
BUTTON_MIN_SIZE = 44
TIME_FONT_POINT_SIZE = 12


class _UnitSpinBox(QSpinBox):
    """One zero-padded HH/MM/SS/mmm segment of a TimecodeEdit, with its own spin
    arrows (as in Audacity). Stepping is delegated to the owning TimecodeEdit (see
    `on_stepped`) rather than handled
    locally: independently wrapping/carrying each box in isolation has a nasty edge
    case at the very start of the timecode -- stepping "seconds" down from
    00:00:00 would wrap+carry all the way up to 00:59:59 instead of just refusing to
    go negative. TimecodeEdit instead recomputes and clamps the WHOLE value as one
    number, then redistributes it back into all four boxes.
    """

    def __init__(self, maximum: int, digits: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._digits = digits
        self.setRange(0, maximum)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFrame(False)
        self.on_stepped: Callable[[int], None] | None = None  # set by TimecodeEdit: called with `steps` (usually +-1)

    def textFromValue(self, value: int) -> str:
        return str(value).zfill(self._digits)

    def valueFromText(self, text: str) -> int:
        try:
            return int(text or 0)
        except ValueError:
            return 0

    def stepBy(self, steps: int) -> None:
        if self.on_stepped is not None:
            self.on_stepped(steps)
        else:
            super().stepBy(steps)

    def stepEnabled(self) -> QAbstractSpinBox.StepEnabledFlag:
        # The default implementation gates Up/Down on THIS box's own value vs. its
        # own min/max (e.g. refuses to step "seconds" down any further once it's at
        # 0) -- but stepBy() above hands stepping off to the owning TimecodeEdit,
        # which clamps the WHOLE value instead. Without this override, Qt would
        # silently swallow the key/click before stepBy() (and TimecodeEdit's own
        # clamp) ever runs at all, e.g. refusing to step "seconds" down at :00 even
        # when the overall timecode is still well above zero (minutes > 0).
        # TimecodeEdit's own clamp is the only limit that should apply.
        if not self.isEnabled() or self.isReadOnly():
            return QAbstractSpinBox.StepEnabledFlag.StepNone
        return QAbstractSpinBox.StepEnabledFlag.StepUpEnabled | QAbstractSpinBox.StepEnabledFlag.StepDownEnabled


class TimecodeEdit(QWidget):
    """An HH:MM:SS.mmm timecode field built from four _UnitSpinBox segments, each
    with its own spin arrows -- see _UnitSpinBox's docstring. Exposes the same
    text()/setText()/clear()/setPlaceholderText()/editingFinished surface the
    previous single-field QLineEdit- and QAbstractSpinBox-based versions had, so
    every call site elsewhere (selection bar, Inspector, transport bar) needed no
    changes.
    """

    editingFinished = Signal()

    # microseconds represented by one step of each unit, in the same order the
    # boxes are laid out left-to-right.
    _UNIT_STEP_US = {"hours": 3_600_000_000, "minutes": 60_000_000, "seconds": 1_000_000, "millis": 1_000}
    _MAX_US = 99 * _UNIT_STEP_US["hours"] + 59 * _UNIT_STEP_US["minutes"] + 59 * _UNIT_STEP_US["seconds"] + 999_000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._suppress_commit = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._hours = _UnitSpinBox(99, 2, self)
        self._minutes = _UnitSpinBox(59, 2, self)
        self._seconds = _UnitSpinBox(59, 2, self)
        self._millis = _UnitSpinBox(999, 3, self)

        layout.addWidget(self._hours)
        layout.addWidget(QLabel(":", self))
        layout.addWidget(self._minutes)
        layout.addWidget(QLabel(":", self))
        layout.addWidget(self._seconds)
        layout.addWidget(QLabel(".", self))
        layout.addWidget(self._millis)

        self._hours.on_stepped = lambda steps: self._step_total(steps * self._UNIT_STEP_US["hours"])
        self._minutes.on_stepped = lambda steps: self._step_total(steps * self._UNIT_STEP_US["minutes"])
        self._seconds.on_stepped = lambda steps: self._step_total(steps * self._UNIT_STEP_US["seconds"])
        self._millis.on_stepped = lambda steps: self._step_total(steps * self._UNIT_STEP_US["millis"])

        # Typed entry (not an arrow/spin step) still only commits on Enter/blur,
        # same as every other editable field in this app -- native editingFinished,
        # not valueChanged, which would fire on every half-typed keystroke.
        for box in (self._hours, self._minutes, self._seconds, self._millis):
            box.editingFinished.connect(self._commit)

    def _step_total(self, delta_us: int) -> None:
        # An arrow press commits immediately; no Enter needed.
        new_us = max(0, min(self._MAX_US, self._value_us() + delta_us))
        self.setText(format_timecode(new_us))
        self._commit()

    def _commit(self) -> None:
        if not self._suppress_commit:
            self.editingFinished.emit()

    # -- QLineEdit-shaped API, so call sites elsewhere don't need to change --

    def text(self) -> str:
        return format_timecode(self._value_us())

    def setText(self, text: str) -> None:
        try:
            time_us = parse_timecode(text) if text else 0
        except ValueError:
            time_us = 0
        self._suppress_commit = True
        try:
            total_ms = time_us // 1000
            self._millis.setValue(total_ms % 1000)
            total_seconds = total_ms // 1000
            self._seconds.setValue(total_seconds % 60)
            total_minutes = total_seconds // 60
            self._minutes.setValue(total_minutes % 60)
            self._hours.setValue(min(total_minutes // 60, self._hours.maximum()))
        finally:
            self._suppress_commit = False

    def clear(self) -> None:
        self.setText("")

    def setPlaceholderText(self, _text: str) -> None:
        pass  # no single text field to place it in -- kept as a harmless no-op

    def setToolTip(self, text: str) -> None:  # noqa: N802 - Qt override
        super().setToolTip(text)
        for box in (self._hours, self._minutes, self._seconds, self._millis):
            box.setToolTip(text)

    def hasFocus(self) -> bool:  # noqa: N802 - Qt override
        return any(box.hasFocus() for box in (self._hours, self._minutes, self._seconds, self._millis))

    def _value_us(self) -> int:
        total_ms = (
            ((self._hours.value() * 60 + self._minutes.value()) * 60 + self._seconds.value()) * 1000
            + self._millis.value()
        )
        return total_ms * 1000


class TransportBar(QWidget):
    """Playback buttons as one centred group -- previous bookmark, previous track, stop,
    play/pause, next track, next bookmark -- with the volume on the left and the position
    readout on the right. (Seeking by 5 s is on the Left/Right arrow keys, Playback menu.)"""

    previous_bookmark_clicked = Signal()
    previous_track_clicked = Signal()
    stop_clicked = Signal()
    play_pause_clicked = Signal()
    next_track_clicked = Signal()
    next_bookmark_clicked = Signal()
    volume_clicked = Signal()  # the volume readout: opens the Volume & EQ tab

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)
        button_font = QFont()
        button_font.setPointSize(BUTTON_FONT_POINT_SIZE)

        # Equal stretch on both sides keeps the button group centred, whatever the
        # widths of the volume (left) and time (right) readouts.
        left = QWidget(self)
        centre = QWidget(self)
        right = QWidget(self)
        group = QHBoxLayout(centre)
        group.setContentsMargins(0, 0, 0, 0)
        group.setSpacing(6)
        volume_side = QHBoxLayout(left)
        volume_side.setContentsMargins(0, 0, 0, 0)
        readout = QVBoxLayout(right)
        readout.setContentsMargins(0, 0, 0, 0)
        readout.setSpacing(0)
        layout.addWidget(left, 1)
        layout.addWidget(centre, 0)
        layout.addWidget(right, 1)

        # Always visible, whichever tab is open below; a click opens Volume & EQ.
        self.volume_button = QToolButton(left)
        self.volume_button.setAutoRaise(True)
        self.volume_button.setToolTip("Player volume -- click for Volume & EQ")
        self.volume_button.clicked.connect(self.volume_clicked.emit)
        volume_font = QFont()
        volume_font.setBold(True)
        self.volume_button.setFont(volume_font)
        self.volume_button.setText("VOL 125 %")
        self.volume_button.setMinimumWidth(self.volume_button.sizeHint().width())
        self.set_volume_percent(100)
        volume_side.addWidget(self.volume_button)
        volume_side.addStretch(1)

        def add_button(text: str, signal: SignalInstance, *, tooltip: str) -> QPushButton:
            button = QPushButton(text, centre)
            button.setFont(button_font)
            # Never narrower than its symbols (a squeezed "▶ ⏸" spilled out of its button).
            text_width = button.fontMetrics().horizontalAdvance(text) + 16
            button.setMinimumSize(max(BUTTON_MIN_SIZE, text_width), BUTTON_MIN_SIZE)
            button.setToolTip(tooltip)
            button.clicked.connect(signal.emit)
            group.addWidget(button)
            return button

        self.previous_bookmark_button = add_button("⏮", self.previous_bookmark_clicked, tooltip="Previous bookmark")
        self.previous_track_button = add_button("⏪", self.previous_track_clicked, tooltip="Previous track")
        self.stop_button = add_button("⏹", self.stop_clicked, tooltip="Stop")
        self.play_pause_button = add_button("▶ ⏸", self.play_pause_clicked, tooltip="Play / Pause (Space)")
        self.next_track_button = add_button("⏩", self.next_track_clicked, tooltip="Next track")
        self.next_bookmark_button = add_button("⏭", self.next_bookmark_clicked, tooltip="Next bookmark")

        # Read-only: timecodes are edited on bookmarks (Inspector, bookmark list). Two
        # lines (position over length) keep the row narrow enough to sit above the
        # waveform beside the playlist.
        time_font = QFont()
        time_font.setPointSize(TIME_FONT_POINT_SIZE)
        time_font.setBold(True)
        self._position_label = QLabel("00:00:00.000", right)
        self._position_label.setFont(time_font)
        self._position_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._position_label.setToolTip("Playback position")
        readout.addWidget(self._position_label)
        self._duration_label = QLabel("/ 00:00:00.000", right)
        self._duration_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._duration_label.setToolTip("Length of the song")
        readout.addWidget(self._duration_label)
        # Both sides get the wider side's width as their minimum, so equal stretch
        # really centres the buttons.
        side_width = max(left.sizeHint().width(), right.sizeHint().width())
        left.setMinimumWidth(side_width)
        right.setMinimumWidth(side_width)

        # Disabled until a player is connected, so a click can't silently do nothing.
        # MainWindow.set_connected() drives this and PlaylistPanel's indicator together.
        self.set_transport_enabled(False)

    def set_time(self, position_us: int, duration_us: int | None) -> None:
        self._position_label.setText(format_timecode(position_us))
        duration_text = format_timecode(duration_us) if duration_us is not None else "--:--:--.---"
        self._duration_label.setText(f"/ {duration_text}")

    def set_volume_percent(self, percent: int) -> None:
        self.volume_button.setText(f"VOL {percent} %")

    def set_transport_enabled(self, enabled: bool) -> None:
        """spec #137: 'VLC offline -> all VLC transport disabled'."""
        for button in (
            self.previous_track_button, self.stop_button, self.play_pause_button, self.next_track_button,
        ):
            button.setEnabled(enabled)

    def set_bookmark_navigation_enabled(self, *, has_previous: bool, has_next: bool) -> None:
        self.previous_bookmark_button.setEnabled(has_previous)
        self.next_bookmark_button.setEnabled(has_next)
