"""Transport bar: prev/next bookmark, prev/next track, play/pause/stop, seek (spec #137)."""
from __future__ import annotations

import re

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)

        def add_button(text: str, signal: Signal) -> QPushButton:
            button = QPushButton(text, self)
            button.clicked.connect(signal.emit)
            layout.addWidget(button)
            return button

        self.previous_bookmark_button = add_button("B◀", self.previous_bookmark_clicked)
        self.previous_track_button = add_button("◀ Track", self.previous_track_clicked)
        self.seek_back_button = add_button("-5s", self.seek_back_clicked)
        self.stop_button = add_button("■", self.stop_clicked)
        self.play_pause_button = add_button("▶/Ⅱ", self.play_pause_clicked)
        self.seek_forward_button = add_button("+5s", self.seek_forward_clicked)
        self.next_track_button = add_button("Track ▶", self.next_track_clicked)
        self.next_bookmark_button = add_button("▶B", self.next_bookmark_clicked)

        self._time_label = QLabel("00:00:00.000 / 00:00:00.000", self)
        layout.addWidget(self._time_label)

    def set_time(self, position_us: int, duration_us: int | None) -> None:
        duration_text = format_timecode(duration_us) if duration_us is not None else "--:--:--.---"
        self._time_label.setText(f"{format_timecode(position_us)} / {duration_text}")

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
