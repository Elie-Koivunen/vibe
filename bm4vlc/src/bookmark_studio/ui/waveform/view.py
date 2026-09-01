"""WaveformView(QGraphicsView): mouse/wheel/key handling, zoom-to-cursor (spec #38, #54)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGraphicsView

from bookmark_studio.ui.waveform.scene import WaveformScene
from bookmark_studio.ui.waveform.waveform_item import scene_x_to_time_us

CLICK_VS_DRAG_THRESHOLD_US = 20_000  # 20ms of pointer movement before it counts as a drag


class WaveformView(QGraphicsView):
    def __init__(self, scene: WaveformScene) -> None:
        super().__init__(scene)
        self._waveform_scene = scene
        self.setDragMode(QGraphicsView.NoDrag)
        self.setRenderHint(self.renderHints())
        self._press_time_us: int | None = None
        self._dragging_selection = False
        self._fit_mode = False

    def _is_empty_space(self, view_pos) -> bool:
        item = self.itemAt(view_pos)
        return item is None or item is self._waveform_scene._waveform_item

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._is_empty_space(event.position().toPoint()):
            self._press_time_us = scene_x_to_time_us(self.mapToScene(event.position().toPoint()).x())
            self._dragging_selection = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_time_us is not None:
            time_us = scene_x_to_time_us(self.mapToScene(event.position().toPoint()).x())
            if abs(time_us - self._press_time_us) > CLICK_VS_DRAG_THRESHOLD_US:
                self._dragging_selection = True
                self._waveform_scene.handle_empty_drag(self._press_time_us, time_us)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._press_time_us is not None:
            time_us = scene_x_to_time_us(self.mapToScene(event.position().toPoint()).x())
            if not self._dragging_selection:
                self._waveform_scene.handle_empty_click(time_us)
            self._press_time_us = None
            self._dragging_selection = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self._is_empty_space(event.position().toPoint()):
            time_us = scene_x_to_time_us(self.mapToScene(event.position().toPoint()).x())
            self._waveform_scene.handle_empty_double_click(time_us)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & Qt.ControlModifier:
            self._fit_mode = False  # manual zoom overrides a prior "fit entire media"
            factor = 1.25 if event.angleDelta().y() > 0 else 0.8
            anchor = self.transformationAnchor()
            self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
            self.scale(factor, 1.0)
            self.setTransformationAnchor(anchor)
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self._waveform_scene.clear_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def fit_entire_media(self) -> None:
        """Ctrl+0 (spec #84). Sticky: re-applied on resize (see resizeEvent) so a
        fit computed before the widget reaches its final on-screen size -- e.g. a
        startup "restore fitted view" -- doesn't silently go stale. Confirmed live:
        calling this immediately after construction, before the widget is shown,
        computed its scale against a transient ~638x461 placeholder viewport size
        that Qt's layout later resized to the real ~1200x750, leaving click/drag
        coordinate mapping silently off from what was visually rendered.
        """
        self._fit_mode = True
        self._apply_fit()

    def _apply_fit(self) -> None:
        self.resetTransform()
        self.fitInView(self.scene().sceneRect(), Qt.IgnoreAspectRatio)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fit_mode:
            self._apply_fit()
