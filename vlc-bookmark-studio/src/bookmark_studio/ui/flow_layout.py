"""A layout that lines its widgets up in a row and wraps them onto the next line when the
row is too narrow (0.10.0: the equalizer's controls, so it can narrow instead of
scrolling). After Qt's own Flow Layout example."""
from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, *, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(QMargins(0, 0, 0, 0))

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt override
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt override
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt override
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt override
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return self._arrange(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt override
        super().setGeometry(rect)
        self._arrange(rect, move=True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        """One row, when there is room for it."""
        visible = [item for item in self._items if not item.isEmpty()]
        width = sum(item.sizeHint().width() for item in visible) + self._spacing * max(0, len(visible) - 1)
        height = max((item.sizeHint().height() for item in visible), default=0)
        margins = self.contentsMargins()
        return QSize(width + margins.left() + margins.right(), height + margins.top() + margins.bottom())

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt override
        """The widest single item: everything else wraps."""
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def rows(self) -> int:
        """How many lines the items take at the current width."""
        tops = {item.geometry().top() for item in self._items if not item.isEmpty()}
        return len(tops)

    def _arrange(self, rect: QRect, *, move: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line_height = area.x(), area.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:
                x = area.x()
                y += line_height + self._spacing
                line_height = 0
            if move:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()
