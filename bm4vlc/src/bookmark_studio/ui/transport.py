"""Transport bar: prev/next bookmark, prev/next track, play/pause/stop, seek (spec #137)."""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QAbstractSpinBox, QGridLayout, QLabel, QPushButton, QWidget

BUTTON_FONT_POINT_SIZE = 16
BUTTON_MIN_SIZE = 44
BOOKMARK_FIELD_FONT_POINT_SIZE = 12

_TIMECODE_RE = re.compile(
    r"^(?:(?:(\d+):)?(\d+):)?(\d+)(?:\.(\d{1,3}))?$"
)


def parse_timecode(text: str) -> int:
    """Parses '17', '17.450', '1:17', '1:17.450', '00:01:17.450' -> microseconds (spec #43)."""
    text = text.strip()
    match = _TIMECODE_RE.match(text)
    if not match:
        raise ValueError(f"unparseable timecode: {text!r}")
    hours_str, minutes_str, seconds_str, millis_str = match.groups()
    hours = int(hours_str) if hours_str else 0
    minutes = int(minutes_str) if minutes_str else 0
    seconds = int(seconds_str)
    millis = int(millis_str.ljust(3, "0")) if millis_str else 0
    total_ms = ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis
    return total_ms * 1000


def format_timecode(time_us: int) -> str:
    """HH:MM:SS.mmm (spec #43)."""
    total_ms = round(time_us / 1000)
    millis = total_ms % 1000
    total_seconds = total_ms // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


class TimecodeEdit(QAbstractSpinBox):
    """An HH:MM:SS.mmm timecode field with real, visible spinner arrow buttons --
    direct follow-up user request: "the bookmark fields still lack arrow buttons to
    increment/decrement the time for hour, minutes, seconds, etc." A plain QLineEdit
    (the previous implementation) only supported Up/Down as a *keyboard* shortcut,
    with no on-screen control at all -- QAbstractSpinBox draws its own up/down
    buttons for free, and stepBy() below makes them (and the Up/Down keys, which
    QAbstractSpinBox already routes to stepBy()) act on whichever HH/MM/SS/mmm
    section the cursor is currently sitting in, like a QTimeEdit, rather than always
    stepping the whole value by a fixed amount.
    """

    # (section_start, section_end, microseconds_per_step) over the fixed
    # "HH:MM:SS.mmm" layout -- cursor position decides which section steps.
    _SECTIONS = [(0, 2, 3_600_000_000), (3, 5, 60_000_000), (6, 8, 1_000_000), (9, 12, 1_000)]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lineEdit().setText(format_timecode(0))
        self.setAlignment(Qt.AlignCenter)

    # -- QLineEdit-shaped API, so call sites elsewhere don't need to change --

    def text(self) -> str:
        return self.lineEdit().text()

    def setText(self, text: str) -> None:
        self.lineEdit().setText(text)

    def clear(self) -> None:
        self.lineEdit().clear()

    def setPlaceholderText(self, text: str) -> None:
        self.lineEdit().setPlaceholderText(text)

    # -- QAbstractSpinBox hooks --

    def stepBy(self, steps: int) -> None:
        cursor_pos = self.lineEdit().cursorPosition()
        try:
            current_us = parse_timecode(self.text())
        except ValueError:
            current_us = 0
        delta_us = self._delta_for_cursor(cursor_pos) * steps
        self.setText(format_timecode(max(0, current_us + delta_us)))
        self.lineEdit().setCursorPosition(cursor_pos)
        self.editingFinished.emit()

    def stepEnabled(self) -> QAbstractSpinBox.StepEnabledFlag:
        if not self.isEnabled() or self.isReadOnly():
            return QAbstractSpinBox.StepNone
        return QAbstractSpinBox.StepUpEnabled | QAbstractSpinBox.StepDownEnabled

    def _delta_for_cursor(self, pos: int) -> int:
        for start, end, delta in self._SECTIONS:
            if start <= pos <= end:
                return delta
        return self._SECTIONS[-1][2]


class TransportBar(QWidget):
    previous_bookmark_clicked = Signal()
    previous_track_clicked = Signal()
    seek_back_clicked = Signal()
    stop_clicked = Signal()
    play_pause_clicked = Signal()
    seek_forward_clicked = Signal()
    next_track_clicked = Signal()
    next_bookmark_clicked = Signal()
    # object, not int: PySide6 marshals a plain `int` signal arg through a 32-bit C++
    # int, which silently wraps for any timecode beyond ~35.8 minutes (2^31 us) --
    # exposed by the new per-section arrow-key stepping (an hour-section step alone
    # already overflows it). microseconds -- edited bookmark start/end.
    bookmark_start_committed = Signal(object)
    bookmark_end_committed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # A 2-row grid, not a single QHBoxLayout: direct user request to "stagger"
        # the bookmark start/end fields on their own row, with the start field
        # column-aligned directly under the live position field above it. A
        # QGridLayout makes that alignment automatic (matching column -> matching
        # width/position) instead of needing fragile manually-tuned spacers.
        layout = QGridLayout(self)
        # Direct user request: "beautify the layout" -- the default zero-margin,
        # zero-spacing grid packed every button and field edge-to-edge.
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(6)
        button_font = QFont()
        button_font.setPointSize(BUTTON_FONT_POINT_SIZE)

        def add_button(text: str, signal: Signal, *, tooltip: str, col: int) -> QPushButton:
            button = QPushButton(text, self)
            button.setFont(button_font)
            button.setMinimumSize(BUTTON_MIN_SIZE, BUTTON_MIN_SIZE)
            button.setToolTip(tooltip)
            button.clicked.connect(signal.emit)
            layout.addWidget(button, 0, col)
            return button

        # Larger, more standard media-control glyphs per direct user feedback
        # ("use better icons in the control button, larger ones"). Play/Pause/Stop
        # (plus the seeks flanking them) are centered as their own cluster -- direct
        # follow-up request: "moved to the middle to reflect that they control the
        # playback of the playlist", distinct from track/bookmark navigation on the
        # outer edges.
        self.previous_bookmark_button = add_button(
            "⏮", self.previous_bookmark_clicked, tooltip="Previous bookmark", col=0
        )
        self.previous_track_button = add_button("⏪", self.previous_track_clicked, tooltip="Previous track", col=1)

        layout.setColumnStretch(2, 1)

        self.seek_back_button = add_button("−5s", self.seek_back_clicked, tooltip="Seek back 5 seconds", col=3)
        self.stop_button = add_button("⏹", self.stop_clicked, tooltip="Stop", col=4)
        self.play_pause_button = add_button("▶ ⏸", self.play_pause_clicked, tooltip="Play / Pause", col=5)
        self.seek_forward_button = add_button(
            "+5s", self.seek_forward_clicked, tooltip="Seek forward 5 seconds", col=6
        )

        layout.setColumnStretch(7, 1)

        self.next_track_button = add_button("⏩", self.next_track_clicked, tooltip="Next track", col=8)
        self.next_bookmark_button = add_button("⏭", self.next_bookmark_clicked, tooltip="Next bookmark", col=9)

        # Direct follow-up request: "the song start end doesnt need to be editable,
        # only the bookmark fields" -- reverted to a plain read-only display (an
        # earlier version made this editable-for-seeking, which the user later
        # decided wasn't needed once the bookmark fields below covered the actual
        # "editable timecode" need).
        self._position_label = QLabel("00:00:00.000", self)
        self._position_label.setFont(button_font)
        layout.addWidget(self._position_label, 0, 10)

        self._duration_label = QLabel("/ 00:00:00.000", self)
        self._duration_label.setFont(button_font)
        layout.addWidget(self._duration_label, 0, 11)

        # Direct follow-up request: "the fields should reflect the bookmark start
        # time and end time" -- mirrors whatever bookmark is currently loaded in the
        # Inspector and edits it the same way; MainWindow wires these signals to the
        # exact same handlers as the Inspector's own Start/End fields, and keeps both
        # in sync whenever either one changes. Row 1, column 10 -- same column as
        # _position_label above, so the two stay visually stacked.
        layout.addWidget(QLabel("Bookmark:", self), 1, 9, alignment=Qt.AlignRight)

        # Direct follow-up request: "the interface needs to be reworked" -- the
        # bookmark fields were being clipped (screenshot showed a lone "3" where the
        # end field's digits should be). Root cause: TimecodeEdit became a real
        # QAbstractSpinBox (so it could grow its own visible spinner buttons), but
        # these two fields still carried the OLD QLineEdit-era setMaximumWidth(150)
        # cap sized for a plain text box with no button area -- at the 16pt button
        # font it was never wide enough for "00:00:00.000" plus those buttons. Now
        # uses its own, slightly smaller font (fits comfortably without dominating
        # the row) and a MINIMUM width instead of a cap, so the widget's own
        # size-aware layout (which already reserves room for the spin buttons) is
        # never squeezed below what it says it actually needs.
        timecode_font = QFont()
        timecode_font.setPointSize(BOOKMARK_FIELD_FONT_POINT_SIZE)

        self._bookmark_start_edit = TimecodeEdit(self)
        self._bookmark_start_edit.setFont(timecode_font)
        self._bookmark_start_edit.setMinimumWidth(130)
        self._bookmark_start_edit.setToolTip(
            "Selected bookmark's start -- edit and press Enter (or use ↑/↓, or the spinner buttons) to adjust it"
        )
        self._bookmark_start_edit.editingFinished.connect(self._on_bookmark_start_committed)
        layout.addWidget(self._bookmark_start_edit, 1, 10)

        layout.addWidget(QLabel("→", self), 1, 11)

        self._bookmark_end_edit = TimecodeEdit(self)
        self._bookmark_end_edit.setFont(timecode_font)
        self._bookmark_end_edit.setMinimumWidth(130)
        self._bookmark_end_edit.setToolTip(
            "Selected bookmark's end -- edit and press Enter (or use ↑/↓, or the spinner buttons) to adjust it"
        )
        self._bookmark_end_edit.editingFinished.connect(self._on_bookmark_end_committed)
        layout.addWidget(self._bookmark_end_edit, 1, 12)

        self.set_bookmark_times(None, None)

        # Direct fix for "the player buttons do not map to vlc player, hence not
        # functioning": set_transport_enabled() (spec #137) existed but was never once
        # called anywhere in app/application.py -- buttons stayed clickable-looking
        # even while genuinely disconnected from VLC, so a click just silently did
        # nothing (the failure was logged at debug level only). The connection
        # indicator itself now lives in PlaylistPanel, right above the Launch VLC
        # button (direct follow-up request: "move the connection status to above the
        # launch vlc button") -- MainWindow.set_connected() drives both it and this
        # button-enable state together.
        self.set_transport_enabled(False)

    def set_time(self, position_us: int, duration_us: int | None) -> None:
        self._position_label.setText(format_timecode(position_us))
        duration_text = format_timecode(duration_us) if duration_us is not None else "--:--:--.---"
        self._duration_label.setText(f"/ {duration_text}")

    def set_bookmark_times(self, start_us: int | None, end_us: int | None) -> None:
        """Called whenever the Inspector's loaded bookmark changes (including to
        None, e.g. nothing selected or a point bookmark with no end) -- keeps this
        mini pair in sync with whatever's actually selected, the same way
        BookmarkInspector.load_bookmark()/clear() do for the main Start/End fields.
        """
        has_bookmark = start_us is not None
        if not self._bookmark_start_edit.hasFocus():
            self._bookmark_start_edit.setText(format_timecode(start_us) if has_bookmark else "")
        self._bookmark_start_edit.setEnabled(has_bookmark)
        has_end = end_us is not None
        if not self._bookmark_end_edit.hasFocus():
            self._bookmark_end_edit.setText(format_timecode(end_us) if has_end else "")
        self._bookmark_end_edit.setEnabled(has_end)

    def _on_bookmark_start_committed(self) -> None:
        try:
            time_us = parse_timecode(self._bookmark_start_edit.text())
        except ValueError:
            return  # caller reverts via the next set_bookmark_times() call
        self.bookmark_start_committed.emit(time_us)

    def _on_bookmark_end_committed(self) -> None:
        try:
            time_us = parse_timecode(self._bookmark_end_edit.text())
        except ValueError:
            return
        self.bookmark_end_committed.emit(time_us)

    def set_transport_enabled(self, enabled: bool) -> None:
        """spec #137: 'VLC offline -> all VLC transport disabled'."""
        for button in (
            self.previous_track_button, self.seek_back_button, self.stop_button,
            self.play_pause_button, self.seek_forward_button, self.next_track_button,
        ):
            button.setEnabled(enabled)

    def set_bookmark_navigation_enabled(self, *, has_previous: bool, has_next: bool) -> None:
        self.previous_bookmark_button.setEnabled(has_previous)
        self.next_bookmark_button.setEnabled(has_next)
