"""WaveformItem: single custom-painted item selecting the nearest pyramid level (spec #112)."""
from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem

from bookmark_studio.waveform.pyramid import WaveformPyramid

WAVEFORM_COLOR = QColor(120, 170, 220)


def time_us_to_scene_x(time_us: int) -> float:
    """1 scene X unit = 1 millisecond (spec #39)."""
    return time_us / 1000.0


def scene_x_to_time_us(x: float) -> int:
    return max(0, round(x * 1000))


class WaveformItem(QGraphicsItem):
    """One item for the entire waveform (spec #4: never one QGraphicsItem per sample).

    `paint()` recomputes which pyramid level to draw from the currently exposed
    rectangle, so panning/zooming never has to rebuild the scene (spec #112).
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
        pixel_width = max(1, int(exposed.width()))

        level = self._pyramid.best_level(max(1, end_us - start_us), pixel_width)
        peaks = level.slice(start_us, end_us, self._pyramid.sample_rate)
        if peaks.shape[0] == 0:
            return

        us_per_peak = level.us_per_peak(self._pyramid.sample_rate)
        mid_y = self._height / 2.0
        amplitude = self._height / 2.0

        painter.setPen(QPen(WAVEFORM_COLOR, 1))
        base_time_us = start_us - (start_us % max(1, int(us_per_peak)))
        for index in range(peaks.shape[0]):
            peak_time_us = base_time_us + index * us_per_peak
            x = time_us_to_scene_x(int(peak_time_us))
            minimum, maximum = float(peaks[index, 0]), float(peaks[index, 1])
            top = mid_y - maximum * amplitude
            bottom = mid_y - minimum * amplitude
            painter.drawLine(x, top, x, bottom)
