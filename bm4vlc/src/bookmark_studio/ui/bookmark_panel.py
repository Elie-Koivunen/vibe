"""Bookmark list panel beneath the waveform (spec #7)."""
from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.ui.transport import format_timecode

COLUMNS = ["Name", "Start", "End", "Loop"]
USER_ROLE = 32


class BookmarkPanel(QWidget):
    bookmark_selected = Signal(object)  # UUID

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(len(COLUMNS))
        self._tree.setHeaderLabels(COLUMNS)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._tree)

    def set_bookmarks(self, bookmarks: list[Bookmark]) -> None:
        self._tree.clear()
        for bookmark in sorted(bookmarks, key=lambda b: b.start_us):
            loop_label = "∞" if bookmark.loop_enabled and bookmark.repeat_count is None else (
                f"×{bookmark.repeat_count}" if bookmark.loop_enabled else ""
            )
            row = QTreeWidgetItem(
                [
                    bookmark.name,
                    format_timecode(bookmark.start_us),
                    format_timecode(bookmark.end_us) if bookmark.end_us is not None else "",
                    loop_label,
                ]
            )
            row.setData(0, USER_ROLE, bookmark.id)
            self._tree.addTopLevelItem(row)

    def select_bookmark(self, bookmark_id: UUID) -> None:
        for i in range(self._tree.topLevelItemCount()):
            row = self._tree.topLevelItem(i)
            if row.data(0, USER_ROLE) == bookmark_id:
                self._tree.setCurrentItem(row)
                return

    def _on_selection_changed(self) -> None:
        selected = self._tree.selectedItems()
        if selected:
            self.bookmark_selected.emit(selected[0].data(0, USER_ROLE))
