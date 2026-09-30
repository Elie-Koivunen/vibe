"""WaveformItem: single custom-painted item selecting the nearest pyramid level (spec #112)."""
from __future__ import annotations

from typing import Any

import numpy as np
from PySide6.QtCore import QLineF, QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPalette, QPen, QPolygon
from PySide6.QtWidgets import QGraphicsItem

from bookmark_studio.waveform.peaks import reduce_peaks
from bookmark_studio.waveform.pyramid import WaveformPyramid

WAVEFORM_FILL = QColor(90, 140, 200, 190)
WAVEFORM_OUTLINE = QColor(50, 100, 160)
CENTER_LINE_COLOR = QColor(160, 180, 200)


def time_us_to_scene_x(time_us: Any) -> Any:
    """1 scene X unit = 1 millisecond (spec #39). Also maps a numpy array element-wise."""
    return time_us / 1000.0


def scene_x_to_time_us(x: float) -> int:
    return max(0, round(x * 1000))


def _over(color: QColor, background: QColor) -> QColor:
    """`color` (possibly translucent) blended onto `background`, as an opaque colour."""
    alpha = color.alphaF()
    return QColor(
        round(color.red() * alpha + background.red() * (1 - alpha)),
        round(color.green() * alpha + background.green() * (1 - alpha)),
        round(color.blue() * alpha + background.blue() * (1 - alpha)),
    )


def cosmetic_pen(color: QColor, width: float = 1.0) -> QPen:
    """A pen `width` screen pixels wide at any zoom. A plain QPen's width is in scene
    units (1 unit = 1 ms): zoomed out it's thinner than a pixel and vanishes; zoomed in
    Qt strokes it dozens of pixels wide, which made every waveform repaint ~50 ms."""
    pen = QPen(color, width)
    pen.setCosmetic(True)
    return pen


def device_pixel_width(painter: QPainter, exposed_scene_rect) -> int:
    """`option.exposedRect` in a QGraphicsItem.paint() is in the item's own SCENE
    coordinates, not real screen pixels -- those only coincide when the view's zoom is
    1:1. After fit_entire_media() scales a multi-minute track down to fit a ~1000px
    viewport, scene-unit width and pixel width can differ by orders of magnitude.
    Using the raw exposedRect.width() as a literal pixel count therefore picks a
    pyramid level (or, worse, a ruler tick interval) calibrated for a view that isn't
    the one actually being rendered (TimeRulerItem would draw a tick about every
    100 ms on a 30 s track). painter.worldTransform() reflects the
    real scene-to-device mapping at paint time and correctly accounts for zoom.
    """
    return max(1, int(painter.worldTransform().mapRect(exposed_scene_rect).width()))


class WaveformItem(QGraphicsItem):
    """One item for the entire waveform (spec #4: never one QGraphicsItem per sample).

    `paint()` recomputes which pyramid level to draw from the currently exposed
    rectangle, so panning/zooming never has to rebuild the scene (spec #112). Renders
    as a filled min/max envelope (Audacity/Peaks.js style), one column per pixel.
    """

    def __init__(self, pyramid: WaveformPyramid, duration_us: int, height: float) -> None:
        super().__init__()
        self.setZValue(-10)  # beneath the bookmarks and the selection (it is opaque)
        self._pyramid = pyramid
        self._duration_us = duration_us
        self._height = height
        # Without this flag Qt passes the item's *whole* rect as option.exposedRect, and
        # every repaint drew the entire song at the current zoom's detail level --
        # hundreds of ms (seconds when zoomed in) per zoom step.
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)

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
            painter.setPen(cosmetic_pen(CENTER_LINE_COLOR))
            painter.drawLine(QLineF(exposed.left(), mid_y, exposed.right(), mid_y))
            return

        us_per_peak = level.us_per_peak(self._pyramid.sample_rate)
        first_index = max(0, int(start_us / us_per_peak))
        # The chosen level has 1-4 peaks per screen pixel: merge them to one min/max
        # column per pixel (numpy does the per-peak work, not a Python loop).
        group = max(1, peaks.shape[0] // max(1, pixel_width))
        if group > 1:
            peaks = reduce_peaks(peaks, group)
        step_us = us_per_peak * group
        xs = time_us_to_scene_x(first_index * us_per_peak + np.arange(peaks.shape[0] + 1) * step_us)
        amplitude = self._height / 2.0
        tops = mid_y - peaks[:, 1] * amplitude
        bottoms = mid_y - peaks[:, 0] * amplitude

        # Drawn in whole screen pixels, as one opaque rectangle per column. Measured
        # (1400 px wide view): a filled polygon costs ~0.3 s -- Qt's polygon filler
        # is quadratic in zig-zag edges and a waveform zig-zags at every pixel -- and
        # any translucent fill ~35 ms; opaque pixel-aligned rectangles ~2 ms.
        transform = painter.worldTransform()
        dev_x = np.floor(xs * transform.m11() + transform.dx()).astype(np.int64)
        dev_top = np.floor(tops * transform.m22() + transform.dy()).astype(np.int64)
        dev_bottom = np.ceil(bottoms * transform.m22() + transform.dy()).astype(np.int64)
        widths = np.maximum(dev_x[1:] - dev_x[:-1], 1)
        heights = np.maximum(dev_bottom - dev_top, 1)
        background = widget.palette().color(QPalette.ColorRole.Base) if widget is not None else QColor(Qt.GlobalColor.white)
        painter.save()
        painter.resetTransform()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(_over(WAVEFORM_FILL, background)))
        lefts = dev_x[:-1].tolist()
        painter.drawRects([
            QRect(left, top, width, height)
            for left, top, width, height in zip(lefts, dev_top.tolist(), widths.tolist(), heights.tolist())
        ])
        centres = (dev_x[:-1] + widths // 2).tolist()
        painter.setPen(QPen(WAVEFORM_OUTLINE, 1))
        painter.drawPolyline(QPolygon([QPoint(x, y) for x, y in zip(centres, dev_top.tolist())]))
        painter.drawPolyline(QPolygon([QPoint(x, y) for x, y in zip(centres, dev_bottom.tolist())]))
        painter.restore()
