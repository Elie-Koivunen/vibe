"""Bookmark list panel beneath the waveform (spec #7)."""
from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.ui.transport import format_timecode

COLUMNS = ["Name", "Start", "End", "Loop"]
USER_ROLE = 32


class BookmarkPanel(QWidget):
    bookmark_selected = Signal(object)  # UUID
    export_requested = Signal()
    play_bookmark_requested = Signal(object)  # UUID
    loop_bookmark_requested = Signal(object)  # UUID
    delete_bookmark_requested = Signal(object)  # UUID

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bookmarks: dict[UUID, Bookmark] = {}
        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        # Direct user request: separate playback controls "that would play explicitly
        # from the bookmark listing itself" -- distinct from the transport bar (which
        # drives the live VLC playlist) and from "Play/Loop Selection" above the
        # waveform (which needs a fresh drag-selection, not a saved bookmark row).
        self._play_bookmark_button = QPushButton("Play Bookmark", self)
        self._play_bookmark_button.setToolTip("Seek VLC to the selected bookmark and play")
        self._play_bookmark_button.clicked.connect(self._on_play_bookmark_clicked)
        toolbar.addWidget(self._play_bookmark_button)

        self._loop_bookmark_button = QPushButton("Loop Bookmark", self)
        self._loop_bookmark_button.setToolTip("Loop the selected bookmark using its own saved loop settings")
        self._loop_bookmark_button.clicked.connect(self._on_loop_bookmark_clicked)
        toolbar.addWidget(self._loop_bookmark_button)

        # Direct user request: "there is no button to select and delete a bookmark" --
        # Delete already worked via the Delete key/Bookmark menu once a row was loaded
        # into the Inspector, but with no visible button it read as a missing feature.
        self._delete_bookmark_button = QPushButton("Delete Bookmark", self)
        self._delete_bookmark_button.setToolTip("Delete the selected bookmark")
        self._delete_bookmark_button.clicked.connect(self._on_delete_bookmark_clicked)
        toolbar.addWidget(self._delete_bookmark_button)

        self._set_playback_buttons_enabled(False, allow_loop=False)

        toolbar.addStretch(1)
        # Direct user feedback: "add a button to save bookmark catalogue" -- the
        # equivalent File > Export Project menu item existed but wasn't discoverable.
        self._export_button = QPushButton("Save Bookmarks...", self)
        self._export_button.setToolTip("Export this playlist's bookmarks to a .vlcbmk file")
        self._export_button.clicked.connect(self.export_requested.emit)
        toolbar.addWidget(self._export_button)
        layout.addLayout(toolbar)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(len(COLUMNS))
        self._tree.setHeaderLabels(COLUMNS)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._tree)

    def _set_playback_buttons_enabled(self, play_enabled: bool, *, allow_loop: bool) -> None:
        self._play_bookmark_button.setEnabled(play_enabled)
        self._loop_bookmark_button.setEnabled(play_enabled and allow_loop)
        self._delete_bookmark_button.setEnabled(play_enabled)

    def _selected_bookmark_id(self) -> UUID | None:
        selected = self._tree.selectedItems()
        return selected[0].data(0, USER_ROLE) if selected else None

    def _on_play_bookmark_clicked(self) -> None:
        bookmark_id = self._selected_bookmark_id()
        if bookmark_id is not None:
            self.play_bookmark_requested.emit(bookmark_id)

    def _on_loop_bookmark_clicked(self) -> None:
        bookmark_id = self._selected_bookmark_id()
        if bookmark_id is not None:
            self.loop_bookmark_requested.emit(bookmark_id)

    def _on_delete_bookmark_clicked(self) -> None:
        bookmark_id = self._selected_bookmark_id()
        if bookmark_id is not None:
            self.delete_bookmark_requested.emit(bookmark_id)

    def set_bookmarks(self, bookmarks: list[Bookmark]) -> None:
        self._bookmarks = {b.id: b for b in bookmarks}
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
        bookmark_id = self._selected_bookmark_id()
        if bookmark_id is not None:
            self.bookmark_selected.emit(bookmark_id)
            bookmark = self._bookmarks.get(bookmark_id)
            self._set_playback_buttons_enabled(True, allow_loop=bookmark is not None and bookmark.end_us is not None)
        else:
            self._set_playback_buttons_enabled(False, allow_loop=False)
