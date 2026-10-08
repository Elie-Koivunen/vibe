"""The "Arrange columns" window of the bookmark list (its header's right-click menu): which
columns show, and in which order -- tick them, drag them, or use Move up / Move down."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

COLUMN_ROLE = Qt.ItemDataRole.UserRole


class ColumnsDialog(QDialog):
    """`columns`: (logical index, name, shown) in the current order. layout() hands back
    the arrangement chosen: [(logical index, shown)] in the new order."""

    def __init__(self, columns: list[tuple[int, str, bool]], default_order: list[int],
                 parent: QWidget | None = None, *, default_hidden: set[int] | None = None) -> None:
        super().__init__(parent)
        self._default_order = default_order
        self._default_hidden = default_hidden or set()
        self.setWindowTitle("Arrange columns")
        self.setModal(True)
        self.resize(320, 380)

        layout = QVBoxLayout(self)
        intro = QLabel("Tick the columns to show; drag them, or use the buttons, to set their order.", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        self._list = QListWidget(self)
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.itemChanged.connect(self._keep_one_shown)
        row.addWidget(self._list, 1)
        buttons = QVBoxLayout()
        self._up_button = QPushButton("Move up", self)
        self._up_button.clicked.connect(lambda: self._move(-1))
        self._down_button = QPushButton("Move down", self)
        self._down_button.clicked.connect(lambda: self._move(1))
        self._defaults_button = QPushButton("Defaults", self)
        self._defaults_button.setToolTip("The original columns, in the original order")
        self._defaults_button.clicked.connect(self.restore_defaults)
        for button in (self._up_button, self._down_button, self._defaults_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        row.addLayout(buttons)
        layout.addLayout(row, 1)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self._names = {logical: name for logical, name, _shown in columns}
        self._fill([(logical, shown) for logical, _name, shown in columns])

    def _fill(self, arrangement: list[tuple[int, bool]]) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for logical, shown in arrangement:
            item = QListWidgetItem(self._names[logical])
            item.setData(COLUMN_ROLE, logical)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setCheckState(Qt.CheckState.Checked if shown else Qt.CheckState.Unchecked)
            self._list.addItem(item)
        self._list.blockSignals(False)
        if self._list.count():
            self._list.setCurrentRow(0)

    def _keep_one_shown(self, item: QListWidgetItem) -> None:
        """A list with no column at all can't be used: the last ticked one stays."""
        if item.checkState() == Qt.CheckState.Unchecked and not any(
            self._list.item(i).checkState() == Qt.CheckState.Checked for i in range(self._list.count())
        ):
            self._list.blockSignals(True)
            item.setCheckState(Qt.CheckState.Checked)
            self._list.blockSignals(False)

    def _move(self, step: int) -> None:
        row = self._list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self._list.count():
            return
        item = self._list.takeItem(row)
        self._list.insertItem(target, item)
        self._list.setCurrentRow(target)

    def restore_defaults(self) -> None:
        self._fill([(logical, logical not in self._default_hidden) for logical in self._default_order])

    def set_shown(self, logical: int, shown: bool) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(COLUMN_ROLE) == logical:
                item.setCheckState(Qt.CheckState.Checked if shown else Qt.CheckState.Unchecked)

    def select_column(self, logical: int) -> None:
        for i in range(self._list.count()):
            if self._list.item(i).data(COLUMN_ROLE) == logical:
                self._list.setCurrentRow(i)

    def layout_chosen(self) -> list[tuple[int, bool]]:
        return [
            (int(self._list.item(i).data(COLUMN_ROLE)), self._list.item(i).checkState() == Qt.CheckState.Checked)
            for i in range(self._list.count())
        ]
