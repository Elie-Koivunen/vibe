"""The BM playback view (0.10.0): three waveform tracks --

    ◀ PREVIOUS   the bookmark ⏮ would play          (dimmed)
    ▶ PLAYING    the bookmark ⏮ / ⏭ count from: the one playing, else the one selected
                 in the list, else the one played last          (with the red line)
    ▷ NEXT       the bookmark ⏭ would play          (dimmed)

Each track shows its bookmark's own range only, all three at one time scale -- the
longest of them fills the width -- so they can be compared (and, later, lined up). A
double-click does what ⏮ / ⏭ do on the outer tracks, and plays the middle one (as a
double-click in the bookmark list)."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import numpy as np
from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPalette, QPen, QPolygon
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from bookmark_studio.domain.timecode import format_timecode
from bookmark_studio.ui.waveform.bookmark_item import PLAYBACK_COLORS, POINT_COLOR, REGION_BORDER, REGION_FILL
from bookmark_studio.ui.waveform.waveform_item import WAVEFORM_FILL, WAVEFORM_OUTLINE
from bookmark_studio.waveform.peaks import reduce_peaks
from bookmark_studio.waveform.pyramid import WaveformPyramid

PREVIOUS, CURRENT, NEXT = -1, 0, 1
PLAYHEAD_COLOR = QColor(220, 40, 40)
DIM_OPACITY = 0.55
POINT_SPAN_US = 2_000_000  # a point bookmark is drawn as the 2 s from its time


@dataclass(frozen=True)
class TrackBookmark:
    """What a track shows of a bookmark."""

    bookmark_id: UUID
    media_id: UUID
    name: str
    song: str
    start_us: int
    end_us: int | None  # None: a point bookmark
    loop_text: str  # "∞", "×4", "" (no loop)
    state: str | None  # "playing" / "done" (the list's colours), or None
    pyramid: WaveformPyramid | None  # None while its waveform is on its way

    @property
    def span_us(self) -> int:
        return (self.end_us - self.start_us) if self.end_us is not None else POINT_SPAN_US

    def header(self) -> str:
        times = format_timecode(self.start_us)
        if self.end_us is not None:
            times += f" → {format_timecode(self.end_us)}"
        else:
            times += " (point)"
        parts = [self.name, self.song, times] + ([self.loop_text] if self.loop_text else [])
        return " · ".join(parts)


def _over(color: QColor, background: QColor) -> QColor:
    alpha = color.alphaF()
    return QColor(
        round(color.red() * alpha + background.red() * (1 - alpha)),
        round(color.green() * alpha + background.green() * (1 - alpha)),
        round(color.blue() * alpha + background.blue() * (1 - alpha)),
    )


class _TrackCanvas(QWidget):
    double_clicked = Signal()

    def __init__(self, role: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._role = role
        self._bookmark: TrackBookmark | None = None
        self._scale_span_us = 1  # the longest of the three: it fills the width
        self._playhead_us: int | None = None
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(400, 90)

    def set_bookmark(self, bookmark: TrackBookmark | None, scale_span_us: int) -> None:
        self._bookmark = bookmark
        self._scale_span_us = max(1, scale_span_us)
        self.update()

    def set_playhead(self, time_us: int | None) -> None:
        if time_us != self._playhead_us:
            self._playhead_us = time_us
            self.update()

    def region_rect(self) -> QRect:
        """Where the bookmark's range is drawn (empty when there is none)."""
        if self._bookmark is None:
            return QRect()
        width = round(self.width() * self._bookmark.span_us / self._scale_span_us)
        return QRect(0, 0, max(1, min(self.width(), width)), self.height())

    def playhead_x(self) -> int | None:
        bookmark, time_us = self._bookmark, self._playhead_us
        if bookmark is None or bookmark.end_us is None or time_us is None:
            return None
        if not bookmark.start_us <= time_us <= bookmark.end_us:
            return None
        return round((time_us - bookmark.start_us) * self.width() / self._scale_span_us)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._bookmark is not None:
            self.double_clicked.emit()
        event.accept()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        background = self.palette().color(QPalette.ColorRole.Base)
        painter.fillRect(self.rect(), background)
        painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Mid), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        bookmark = self._bookmark
        if bookmark is None:
            painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
            text = {PREVIOUS: "No bookmark before it in the list", CURRENT: "Play or select a bookmark",
                    NEXT: "No bookmark after it in the list"}[self._role]
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)
            return
        if self._role != CURRENT:
            painter.setOpacity(DIM_OPACITY)
        region = self.region_rect()
        fill, border, _handles = PLAYBACK_COLORS.get(bookmark.state or "", (REGION_FILL, REGION_BORDER, None))
        painter.fillRect(region, _over(fill, background))
        painter.setPen(QPen(border, 2))
        painter.drawRect(region.adjusted(1, 1, -1, -1))
        if bookmark.end_us is None:
            painter.setPen(QPen(POINT_COLOR, 2))
            painter.drawLine(1, 0, 1, self.height())
        elif bookmark.pyramid is not None:
            self._paint_waveform(painter, bookmark, region, _over(fill, background))
        else:
            painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(region, Qt.AlignmentFlag.AlignCenter, "Waveform on its way…")
        playhead = self.playhead_x()
        if playhead is not None:
            painter.setOpacity(1.0)
            painter.setPen(QPen(PLAYHEAD_COLOR, 2))
            painter.drawLine(playhead, 0, playhead, self.height())

    def _paint_waveform(self, painter: QPainter, bookmark: TrackBookmark, region: QRect, behind: QColor) -> None:
        pyramid = bookmark.pyramid
        assert pyramid is not None and bookmark.end_us is not None
        width = region.width()
        level = pyramid.best_level(max(1, bookmark.span_us), width)
        peaks = level.slice(bookmark.start_us, bookmark.end_us, pyramid.sample_rate)
        if peaks.shape[0] == 0:
            return
        group = max(1, peaks.shape[0] // max(1, width))
        if group > 1:
            peaks = reduce_peaks(peaks, group)
        columns = peaks.shape[0]
        xs = np.floor(np.arange(columns + 1) * width / columns).astype(np.int64)
        mid = region.height() / 2.0
        amplitude = region.height() / 2.0 - 3
        tops = np.floor(mid - peaks[:, 1] * amplitude).astype(np.int64)
        bottoms = np.ceil(mid - peaks[:, 0] * amplitude).astype(np.int64)
        widths = np.maximum(xs[1:] - xs[:-1], 1)
        heights = np.maximum(bottoms - tops, 1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(_over(WAVEFORM_FILL, behind)))
        painter.drawRects([QRect(x, t, w, h) for x, t, w, h in zip(xs[:-1].tolist(), tops.tolist(),
                                                                     widths.tolist(), heights.tolist())])
        centres = (xs[:-1] + widths // 2).tolist()
        painter.setPen(QPen(WAVEFORM_OUTLINE, 1))
        painter.drawPolyline(QPolygon([QPoint(x, y) for x, y in zip(centres, tops.tolist())]))
        painter.drawPolyline(QPolygon([QPoint(x, y) for x, y in zip(centres, bottoms.tolist())]))


class BookmarkTracksView(QWidget):
    track_double_clicked = Signal(int)  # PREVIOUS / CURRENT / NEXT

    _ROLE_TEXT = {PREVIOUS: "◀ PREVIOUS", NEXT: "▷ NEXT"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 6, 4, 4)
        layout.setSpacing(2)
        self._headers: dict[int, QLabel] = {}
        self._canvases: dict[int, _TrackCanvas] = {}
        self._bookmarks: dict[int, TrackBookmark | None] = {PREVIOUS: None, CURRENT: None, NEXT: None}
        tips = {PREVIOUS: "The bookmark before it in the list -- double-click to play it (as ⏮)",
                CURRENT: "The bookmark playing (or selected, or played last) -- double-click to play it",
                NEXT: "The bookmark after it in the list -- double-click to play it (as ⏭)"}
        for index, role in enumerate((PREVIOUS, CURRENT, NEXT)):
            header = QLabel(self)
            header.setTextFormat(Qt.TextFormat.PlainText)
            header.setMinimumWidth(1)  # a long name is cut off, never widens the window
            canvas = _TrackCanvas(role, self)
            canvas.setToolTip(tips[role])
            canvas.double_clicked.connect(lambda r=role: self.track_double_clicked.emit(r))
            if index:
                layout.addSpacing(6)
            layout.addWidget(header)
            layout.addWidget(canvas, 1)
            self._headers[role] = header
            self._canvases[role] = canvas
        self.show_tracks(None, None, None)

    def show_tracks(self, previous: TrackBookmark | None, current: TrackBookmark | None,
                    following: TrackBookmark | None) -> None:
        self._bookmarks = {PREVIOUS: previous, CURRENT: current, NEXT: following}
        spans = [b.span_us for b in (previous, current, following) if b is not None]
        scale = max(spans) if spans else 1
        for role, bookmark in self._bookmarks.items():
            self._canvases[role].set_bookmark(bookmark, scale)
            self._headers[role].setText(f"{self._role_text(role, bookmark)}   "
                                        f"{bookmark.header() if bookmark is not None else ''}".rstrip())
            self._headers[role].setStyleSheet("font-weight: 600;" if role == CURRENT else "color: palette(mid);")

    def _role_text(self, role: int, bookmark: TrackBookmark | None) -> str:
        if role != CURRENT:
            return self._ROLE_TEXT[role]
        if bookmark is not None and bookmark.state == "playing":
            return "▶ PLAYING"
        return "● CURRENT"

    def set_playhead(self, media_id: UUID | None, time_us: int | None) -> None:
        """The player's position -- shown on the middle track while inside its range."""
        current = self._bookmarks[CURRENT]
        shown = time_us if current is not None and media_id == current.media_id else None
        self._canvases[CURRENT].set_playhead(shown)

    # -- for tests / the window --

    def tracks(self) -> tuple[TrackBookmark | None, TrackBookmark | None, TrackBookmark | None]:
        return self._bookmarks[PREVIOUS], self._bookmarks[CURRENT], self._bookmarks[NEXT]

    def canvas(self, role: int) -> _TrackCanvas:
        return self._canvases[role]

    def header_text(self, role: int) -> str:
        return self._headers[role].text()


__all__ = ["CURRENT", "NEXT", "PREVIOUS", "BookmarkTracksView", "TrackBookmark"]
