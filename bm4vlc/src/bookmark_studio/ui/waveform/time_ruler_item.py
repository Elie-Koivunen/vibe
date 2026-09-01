"""TimeRulerItem: Audacity-style timestamp ruler above the waveform (spec #4, #7)."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QTransform
from PySide6.QtWidgets import QGraphicsItem

from bookmark_studio.ui.transport import format_timecode
from bookmark_studio.ui.waveform.waveform_item import device_pixel_width, scene_x_to_time_us, time_us_to_scene_x

RULER_HEIGHT = 24
TICK_COLOR = QColor(120, 120, 120)
TEXT_COLOR = QColor(60, 60, 60)
# "Nice" tick spacings in whole microseconds, from very zoomed in to fully zoomed out.
_NICE_INTERVALS_US = [
    1_000, 2_000, 5_000, 10_000, 20_000, 50_000, 100_000, 200_000, 500_000,
    1_000_000, 2_000_000, 5_000_000, 10_000_000, 15_000_000, 30_000_000,
    60_000_000, 120_000_000, 300_000_000, 600_000_000, 1_800_000_000, 3_600_000_000,
]


def _pick_interval_us(us_per_pixel: float, min_pixel_spacing: float = 80.0) -> int:
    """Smallest "nice" interval whose on-screen spacing is at least min_pixel_spacing."""
    for interval in _NICE_INTERVALS_US:
        if interval / us_per_pixel >= min_pixel_spacing:
            return interval
    return _NICE_INTERVALS_US[-1]


class TimeRulerItem(QGraphicsItem):
    def __init__(self, duration_us: int, height: float = RULER_HEIGHT) -> None:
        super().__init__()
        self._duration_us = duration_us
        self._height = height
        self.setZValue(90)

    def set_duration_us(self, duration_us: int) -> None:
        self.prepareGeometryChange()
        self._duration_us = duration_us

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(0, 0, time_us_to_scene_x(max(self._duration_us, 1)), self._height)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        exposed = option.exposedRect if option is not None else self.boundingRect()
        pixel_width = device_pixel_width(painter, exposed)
        visible_us = max(1, scene_x_to_time_us(exposed.right()) - scene_x_to_time_us(exposed.left()))
        us_per_pixel = visible_us / pixel_width
        interval_us = _pick_interval_us(us_per_pixel)

        start_us = scene_x_to_time_us(max(0.0, exposed.left()))
        first_tick = (start_us // interval_us) * interval_us
        end_us = scene_x_to_time_us(exposed.right())

        painter.setPen(QPen(TICK_COLOR, 1))
        painter.drawLine(exposed.left(), self._height - 1, exposed.right(), self._height - 1)

        # Tick marks are drawn in the item's normal (scene) coordinate space -- short
        # vertical lines scale visually fine with zoom. Labels do not: a font's point
        # size is interpreted in the *painter's* logical coordinate space, so once the
        # view is zoomed out (fit_entire_media() on anything longer than a few
        # seconds), the same drawText() call that looks fine at 1:1 zoom renders text
        # a tiny fraction of a pixel tall -- confirmed live, completely invisible on a
        # 30s track. Qt's standard fix: map each anchor point through the current
        # world transform to get its real device-pixel position, reset the painter to
        # identity, and draw the label there -- so labels always render at a normal,
        # constant screen size regardless of zoom (like QGraphicsItem's
        # ItemIgnoresTransformations flag, but per-piece-of-text within one item).
        world_transform = painter.worldTransform()
        tick_us = first_tick
        while tick_us <= end_us + interval_us:
            x = time_us_to_scene_x(tick_us)
            painter.setPen(QPen(TICK_COLOR, 1))
            painter.drawLine(x, self._height - 8, x, self._height - 1)

            label = format_timecode(tick_us) if interval_us < 1_000_000 else format_timecode(tick_us)[:8]
            device_anchor = world_transform.map(QPointF(x + 2, self._height - 10))
            painter.save()
            painter.setWorldTransform(QTransform())
            painter.setPen(QPen(TEXT_COLOR))
            painter.drawText(device_anchor, label)
            painter.restore()

            tick_us += interval_us
