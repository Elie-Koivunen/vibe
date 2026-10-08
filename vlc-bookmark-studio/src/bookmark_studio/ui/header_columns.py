"""The right-click menu on a list's column titles: which columns show, and in which order.
Shared by the bookmark list and the source playlist (0.9.0)."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QDialog, QMenu, QTreeWidget

from bookmark_studio.ui.dialogs.columns_dialog import ColumnsDialog


class HeaderColumns(QObject):
    """Every column ticked when shown (the last one shown can't be unticked), the column
    right-clicked moved left or right, Arrange Columns... for both at once, and Restore
    Default Columns. Columns can also be dragged into another order by their titles."""

    # The columns shown changed. True: one was shown (the list may need more room).
    changed = Signal(bool)

    def __init__(self, tree: QTreeWidget, names: list[str], default_order: list[str], *,
                 fit: Callable[[], None] | None = None, hidden: tuple[str, ...] = ()) -> None:
        """`fit`: sizes the columns once the defaults are back (default: to contents);
        `hidden`: the columns not shown by default (shown again from the menu)."""
        super().__init__(tree)
        self._tree = tree
        self._names = names
        self._default_order = default_order
        self._hidden_by_default = hidden
        self._fit = fit or self._fit_to_contents
        header = tree.header()
        header.setSectionsMovable(True)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._show_menu)

    def apply_default_order(self) -> None:
        """The default order, and which columns show by default."""
        header = self._tree.header()
        for visual, name in enumerate(self._default_order):
            header.moveSection(header.visualIndex(self._names.index(name)), visual)
        for logical, name in enumerate(self._names):
            header.setSectionHidden(logical, name in self._hidden_by_default)

    def _show_menu(self, pos) -> None:  # noqa: ANN001 - QPoint
        header = self._tree.header()
        self.menu(header.logicalIndexAt(pos)).exec(header.mapToGlobal(pos))

    def menu(self, clicked: int = -1) -> QMenu:
        header = self._tree.header()
        menu = QMenu(self._tree)
        order = self.order()
        shown_count = sum(1 for logical in order if not header.isSectionHidden(logical))
        for logical in order:
            action = menu.addAction(self._names[logical])
            action.setCheckable(True)
            shown = not header.isSectionHidden(logical)
            action.setChecked(shown)
            action.setEnabled(not (shown and shown_count == 1))
            action.toggled.connect(lambda checked, column=logical: self.set_shown(column, checked))
        if 0 <= clicked < len(self._names):
            menu.addSeparator()
            name = self._names[clicked]
            for label, step in ((f"Move “{name}” Left", -1), (f"Move “{name}” Right", 1)):
                move = menu.addAction(label)
                move.setEnabled(self._shown_neighbour(clicked, step) is not None)
                move.triggered.connect(lambda _checked=False, s=step: self.move(clicked, s))
        menu.addSeparator()
        menu.addAction("Arrange Columns...").triggered.connect(lambda: self.arrange(clicked))
        menu.addAction("Restore Default Columns").triggered.connect(self.restore_defaults)
        return menu

    def order(self) -> list[int]:
        """The columns (logical indexes) left to right, hidden ones included."""
        header = self._tree.header()
        return [header.logicalIndex(visual) for visual in range(header.count())]

    def shown(self) -> list[str]:
        header = self._tree.header()
        return [self._names[logical] for logical in self.order() if not header.isSectionHidden(logical)]

    def _shown_neighbour(self, logical: int, step: int) -> int | None:
        header = self._tree.header()
        shown = [c for c in self.order() if not header.isSectionHidden(c)]
        if logical not in shown:
            return None
        index = shown.index(logical) + step
        return shown[index] if 0 <= index < len(shown) else None

    def move(self, logical: int, step: int) -> None:
        """Swaps the column with the shown one to its left (-1) or right (1)."""
        other = self._shown_neighbour(logical, step)
        if other is not None:
            header = self._tree.header()
            header.moveSection(header.visualIndex(logical), header.visualIndex(other))

    def set_shown(self, logical: int, shown: bool) -> None:
        header = self._tree.header()
        if not shown and sum(1 for c in range(header.count()) if not header.isSectionHidden(c)) <= 1:
            return  # one column always stays
        header.setSectionHidden(logical, not shown)
        if shown and header.sectionSize(logical) < 24:
            self._tree.resizeColumnToContents(logical)
        self.changed.emit(shown)

    def set_layout(self, arrangement: list[tuple[int, bool]]) -> None:
        """[(logical index, shown)] left to right (the Arrange Columns window)."""
        if not any(shown for _logical, shown in arrangement):
            return
        header = self._tree.header()
        for visual, (logical, _shown) in enumerate(arrangement):
            header.moveSection(header.visualIndex(logical), visual)
        for logical, shown in arrangement:  # show first: one column always stays
            if shown:
                self.set_shown(logical, True)
        for logical, shown in arrangement:
            if not shown:
                self.set_shown(logical, False)

    def arrange(self, clicked: int = -1) -> None:
        header = self._tree.header()
        dialog = ColumnsDialog(
            [(logical, self._names[logical], not header.isSectionHidden(logical)) for logical in self.order()],
            [self._names.index(name) for name in self._default_order], self._tree,
            default_hidden={self._names.index(name) for name in self._hidden_by_default},
        )
        if clicked >= 0:
            dialog.select_column(clicked)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_layout(dialog.layout_chosen())

    def restore_defaults(self) -> None:
        self.set_layout([(self._names.index(name), name not in self._hidden_by_default)
                         for name in self._default_order])
        self._fit()
        self.changed.emit(True)

    def _fit_to_contents(self) -> None:
        for column in range(len(self._names)):
            self._tree.resizeColumnToContents(column)
