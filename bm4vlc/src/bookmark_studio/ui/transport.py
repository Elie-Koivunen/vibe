"""Transport bar: prev/next bookmark, prev/next track, play/pause/stop, seek (spec #137)."""
from __future__ import annotations

import re

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

BUTTON_FONT_POINT_SIZE = 16
BUTTON_MIN_SIZE = 44

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


class TransportBar(QWidget):
    previous_bookmark_clicked = Signal()
    previous_track_clicked = Signal()
    seek_back_clicked = Signal()
    stop_clicked = Signal()
    play_pause_clicked = Signal()
    seek_forward_clicked = Signal()
    next_track_clicked = Signal()
    next_bookmark_clicked = Signal()
    position_seek_requested = Signal(int)  # microseconds -- manually typed into the position field
    bookmark_start_committed = Signal(int)  # microseconds -- edited bookmark start
    bookmark_end_committed = Signal(int)  # microseconds -- edited bookmark end

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        button_font = QFont()
        button_font.setPointSize(BUTTON_FONT_POINT_SIZE)

        def add_button(text: str, signal: Signal, *, tooltip: str) -> QPushButton:
            button = QPushButton(text, self)
            button.setFont(button_font)
            button.setMinimumSize(BUTTON_MIN_SIZE, BUTTON_MIN_SIZE)
            button.setToolTip(tooltip)
            button.clicked.connect(signal.emit)
            layout.addWidget(button)
            return button

        # Larger, more standard media-control glyphs per direct user feedback
        # ("use better icons in the control button, larger ones"). Play/Pause/Stop
        # (plus the seeks flanking them) are centered as their own cluster -- direct
        # follow-up request: "moved to the middle to reflect that they control the
        # playback of the playlist", distinct from track/bookmark navigation on the
        # outer edges.
        self.previous_bookmark_button = add_button("⏮", self.previous_bookmark_clicked, tooltip="Previous bookmark")
        self.previous_track_button = add_button("⏪", self.previous_track_clicked, tooltip="Previous track")

        layout.addStretch(1)

        self.seek_back_button = add_button("−5s", self.seek_back_clicked, tooltip="Seek back 5 seconds")
        self.stop_button = add_button("⏹", self.stop_clicked, tooltip="Stop")
        self.play_pause_button = add_button("▶ ⏸", self.play_pause_clicked, tooltip="Play / Pause")
        self.seek_forward_button = add_button("+5s", self.seek_forward_clicked, tooltip="Seek forward 5 seconds")

        layout.addStretch(1)

        self.next_track_button = add_button("⏩", self.next_track_clicked, tooltip="Next track")
        self.next_bookmark_button = add_button("⏭", self.next_bookmark_clicked, tooltip="Next bookmark")

        # Direct user request: "the timer are still not manually editable" -- the
        # current-position half of the transport display is now a real editable
        # field (type a timecode, press Enter, VLC seeks there), not just a label.
        # Duration stays a plain label since seeking "to the duration" isn't a thing.
        self._position_edit = QLineEdit("00:00:00.000", self)
        self._position_edit.setFont(button_font)
        self._position_edit.setMaximumWidth(150)
        self._position_edit.setToolTip("Current position -- edit and press Enter to seek")
        self._position_edit.editingFinished.connect(self._on_position_committed)
        layout.addWidget(self._position_edit)

        self._duration_label = QLabel("/ 00:00:00.000", self)
        self._duration_label.setFont(button_font)
        layout.addWidget(self._duration_label)

        # Direct follow-up request: "the fields should reflect the bookmark start
        # time and end time, a different timer should be adjacent for the duration
        # of the whole song" -- added alongside (not replacing) live position/
        # duration above. Mirrors the currently selected/loaded bookmark (same one
        # the Inspector shows) and edits it the same way; MainWindow wires these
        # signals to the exact same handlers as the Inspector's own Start/End fields,
        # and keeps both in sync whenever either one changes.
        layout.addWidget(QLabel("Bookmark:", self))

        self._bookmark_start_edit = QLineEdit(self)
        self._bookmark_start_edit.setFont(button_font)
        self._bookmark_start_edit.setMaximumWidth(150)
        self._bookmark_start_edit.setToolTip("Selected bookmark's start -- edit and press Enter to adjust it")
        self._bookmark_start_edit.editingFinished.connect(self._on_bookmark_start_committed)
        layout.addWidget(self._bookmark_start_edit)

        layout.addWidget(QLabel("→", self))

        self._bookmark_end_edit = QLineEdit(self)
        self._bookmark_end_edit.setFont(button_font)
        self._bookmark_end_edit.setMaximumWidth(150)
        self._bookmark_end_edit.setToolTip("Selected bookmark's end -- edit and press Enter to adjust it")
        self._bookmark_end_edit.editingFinished.connect(self._on_bookmark_end_committed)
        layout.addWidget(self._bookmark_end_edit)

        self.set_bookmark_times(None, None)

        # Direct fix for "the player buttons do not map to vlc player, hence not
        # functioning": set_transport_enabled() (spec #137) existed but was never once
        # called anywhere in app/application.py -- buttons stayed clickable-looking
        # even while genuinely disconnected from VLC, so a click just silently did
        # nothing (the failure was logged at debug level only). This label makes the
        # actual connection state visible instead of a click doing nothing unexplained.
        self._connection_label = QLabel("● Offline", self)
        self._connection_label.setStyleSheet("color: #a33;")
        layout.addWidget(self._connection_label)

        self.set_connected(False)

    def set_time(self, position_us: int, duration_us: int | None) -> None:
        # Never overwrite the field while the user has it focused (e.g. mid-edit) --
        # this is called on every ~400ms status poll, which would otherwise stomp
        # whatever they'd typed before they finished entering it.
        if not self._position_edit.hasFocus():
            self._position_edit.setText(format_timecode(position_us))
        duration_text = format_timecode(duration_us) if duration_us is not None else "--:--:--.---"
        self._duration_label.setText(f"/ {duration_text}")

    def _on_position_committed(self) -> None:
        try:
            time_us = parse_timecode(self._position_edit.text())
        except ValueError:
            return  # will be overwritten back to the real position on the next poll
        self.position_seek_requested.emit(time_us)

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

    def set_connected(self, connected: bool) -> None:
        self.set_transport_enabled(connected)
        if connected:
            self._connection_label.setText("● Connected")
            self._connection_label.setStyleSheet("color: #2a2;")
        else:
            self._connection_label.setText("● Offline")
            self._connection_label.setStyleSheet("color: #a33;")

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
