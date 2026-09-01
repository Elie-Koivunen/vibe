"""WaveformItem: single custom-painted item selecting the nearest pyramid level (spec #112)."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsItem

from bookmark_studio.waveform.pyramid import WaveformPyramid

WAVEFORM_FILL = QColor(90, 140, 200, 190)
WAVEFORM_OUTLINE = QColor(50, 100, 160)
CENTER_LINE_COLOR = QColor(160, 180, 200)


def time_us_to_scene_x(time_us: int) -> float:
    """1 scene X unit = 1 millisecond (spec #39)."""
    return time_us / 1000.0


def scene_x_to_time_us(x: float) -> int:
    return max(0, round(x * 1000))


def device_pixel_width(painter: QPainter, exposed_scene_rect) -> int:
    """`option.exposedRect` in a QGraphicsItem.paint() is in the item's own SCENE
    coordinates, not real screen pixels -- those only coincide when the view's zoom is
    1:1. After fit_entire_media() scales a multi-minute track down to fit a ~1000px
    viewport, scene-unit width and pixel width can differ by orders of magnitude.
    Using the raw exposedRect.width() as a literal pixel count therefore picks a
    pyramid level (or, worse, a ruler tick interval) calibrated for a view that isn't
    the one actually being rendered -- confirmed live: this made TimeRulerItem draw a
    tick roughly every 100ms on a 30s track. painter.worldTransform() reflects the
    real scene-to-device mapping at paint time and correctly accounts for zoom.
    """
    return max(1, int(painter.worldTransform().mapRect(exposed_scene_rect).width()))


class WaveformItem(QGraphicsItem):
    """One item for the entire waveform (spec #4: never one QGraphicsItem per sample).

    `paint()` recomputes which pyramid level to draw from the currently exposed
    rectangle, so panning/zooming never has to rebuild the scene (spec #112). Renders
    as a single filled min/max envelope polygon (Audacity/Peaks.js style) rather than
    discrete per-column lines, per direct user request for an Audacity-like look.
    """

    def __init__(self, pyramid: WaveformPyramid, duration_us: int, height: float) -> None:
        super().__init__()
        self._pyramid = pyramid
        self._duration_us = duration_us
        self._height = height

    def set_pyramid(self, pyramid: WaveformPyramid, duration_us: int) -> None:
        self.prepareGeometryChange()
        self._pyramid = pyramid
        self._duration_us = duration_us
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt override
        return QRectF(0, 0, time_us_to_scene_x(self._duration_us), self._height)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        exposed = option.exposedRect if option is not None else self.boundingRect()
        start_us = scene_x_to_time_us(max(0.0, exposed.left()))
        end_us = scene_x_to_time_us(exposed.right())
        pixel_width = device_pixel_width(painter, exposed)

        mid_y = self._height / 2.0

        level = self._pyramid.best_level(max(1, end_us - start_us), pixel_width)
        peaks = level.slice(start_us, end_us, self._pyramid.sample_rate)
        if peaks.shape[0] == 0:
            painter.setPen(QPen(CENTER_LINE_COLOR, 1))
            painter.drawLine(exposed.left(), mid_y, exposed.right(), mid_y)
            return

        us_per_peak = level.us_per_peak(self._pyramid.sample_rate)
        amplitude = self._height / 2.0
        base_time_us = start_us - (start_us % max(1, int(us_per_peak)))

        top_points: list[QPointF] = []
        bottom_points: list[QPointF] = []
        for index in range(peaks.shape[0]):
            peak_time_us = base_time_us + index * us_per_peak
            x = time_us_to_scene_x(int(peak_time_us))
            minimum, maximum = float(peaks[index, 0]), float(peaks[index, 1])
            top_points.append(QPointF(x, mid_y - maximum * amplitude))
            bottom_points.append(QPointF(x, mid_y - minimum * amplitude))

        polygon = QPolygonF(top_points + list(reversed(bottom_points)))
        painter.setPen(QPen(WAVEFORM_OUTLINE, 1))
        painter.setBrush(QBrush(WAVEFORM_FILL))
        painter.drawPolygon(polygon)
