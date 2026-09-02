"""Bookmark list panel beneath the waveform (spec #7)."""
from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.ui.transport import format_timecode

# Direct user request: a "Song" column identifying which track each bookmark belongs
# to, since this list now spans every song in the playlist, not just the one on screen.
COLUMNS = ["Song", "Name", "Start", "End", "Loop"]
USER_ROLE = 32


class BookmarkPanel(QWidget):
    bookmark_selected = Signal(object)  # UUID -- only emitted when exactly one row is selected
    export_requested = Signal()
    play_bookmark_requested = Signal(object)  # UUID
    loop_bookmark_requested = Signal(object)  # UUID
    delete_bookmark_requested = Signal(list)  # list of UUIDs -- one or more
    reorder_requested = Signal(list)  # ordered list of every bookmark UUID in the list

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bookmarks: dict[UUID, Bookmark] = {}
        self._song_names: dict[UUID, str] = {}
        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        # Direct user request: separate playback controls "that would play explicitly
        # from the bookmark listing itself" -- distinct from the transport bar (which
        # drives the live VLC playlist) and from "Play/Loop Selection" above the
        # waveform (which needs a fresh drag-selection, not a saved bookmark row).
        # Play/Loop only ever act on ONE bookmark (playing several at once isn't a
        # thing), so they stay enabled only when the selection is exactly one row.
        self._play_bookmark_button = QPushButton("Play Bookmark", self)
        self._play_bookmark_button.setToolTip("Seek VLC to the selected bookmark and play")
        self._play_bookmark_button.clicked.connect(self._on_play_bookmark_clicked)
        toolbar.addWidget(self._play_bookmark_button)

        self._loop_bookmark_button = QPushButton("Loop Bookmark", self)
        self._loop_bookmark_button.setToolTip("Loop the selected bookmark using its own saved loop settings")
        self._loop_bookmark_button.clicked.connect(self._on_loop_bookmark_clicked)
        toolbar.addWidget(self._loop_bookmark_button)

        # Direct user request: "there is no button to select and delete a bookmark"
        # (later: "i should be able to multiple select and delete or move") -- Delete
        # already worked via the Delete key/Bookmark menu once a row was loaded into
        # the Inspector, but with no visible button, and no multi-select support, it
        # read as a missing feature.
        self._delete_bookmark_button = QPushButton("Delete Bookmark", self)
        self._delete_bookmark_button.setToolTip("Delete every selected bookmark")
        self._delete_bookmark_button.clicked.connect(self._on_delete_bookmark_clicked)
        toolbar.addWidget(self._delete_bookmark_button)

        # Direct user request: "the row entries should also be possible to manually
        # reorder them moving up/down".
        self._move_up_button = QPushButton("Move Up", self)
        self._move_up_button.setToolTip("Move every selected bookmark up in this list")
        self._move_up_button.clicked.connect(lambda: self._move_selected(-1))
        toolbar.addWidget(self._move_up_button)

        self._move_down_button = QPushButton("Move Down", self)
        self._move_down_button.setToolTip("Move every selected bookmark down in this list")
        self._move_down_button.clicked.connect(lambda: self._move_selected(1))
        toolbar.addWidget(self._move_down_button)

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
        # Direct user request: "i should be able to multiple select and delete or
        # move" -- Ctrl/Shift-click or a rubber-band drag selects more than one row.
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # Direct user request: "nice to have all columns adjustable and reorderable".
        # Interactive (drag-to-resize) is QHeaderView's default already; Movable adds
        # drag-to-reorder.
        header = self._tree.header()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(QHeaderView.Interactive)
        # Direct user request: "i should be able to just click and drag the entry up
        # and down instead of relying on separate buttons" -- Move Up/Down (below)
        # stay as a fallback, but this is the primary way to reorder now. Qt's
        # InternalMove drags the whole current selection together, multi-select included.
        self._tree.setDragDropMode(QTreeWidget.InternalMove)
        self._tree.setDragEnabled(True)
        self._tree.setAcceptDrops(True)
        self._tree.setDropIndicatorShown(True)
        self._tree.setRootIsDecorated(False)
        self._tree.model().rowsMoved.connect(self._on_rows_moved)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self._tree)

        self._set_playback_buttons_enabled(0, allow_loop=False)

    def _set_playback_buttons_enabled(self, selected_count: int, *, allow_loop: bool) -> None:
        self._play_bookmark_button.setEnabled(selected_count == 1)
        self._loop_bookmark_button.setEnabled(selected_count == 1 and allow_loop)
        self._delete_bookmark_button.setEnabled(selected_count > 0)
        self._update_move_buttons_enabled()

    def _selected_indices(self) -> list[int]:
        return sorted(self._tree.indexOfTopLevelItem(item) for item in self._tree.selectedItems())

    def _update_move_buttons_enabled(self) -> None:
        indices = self._selected_indices()
        if not indices:
            self._move_up_button.setEnabled(False)
            self._move_down_button.setEnabled(False)
            return
        self._move_up_button.setEnabled(indices[0] > 0)
        self._move_down_button.setEnabled(indices[-1] < self._tree.topLevelItemCount() - 1)

    def _selected_bookmark_ids(self) -> list[UUID]:
        return [item.data(0, USER_ROLE) for item in self._tree.selectedItems()]

    def _selected_bookmark_id(self) -> UUID | None:
        """Single-selection convenience for Play/Loop, which only ever act on one row."""
        ids = self._selected_bookmark_ids()
        return ids[0] if len(ids) == 1 else None

    def _on_play_bookmark_clicked(self) -> None:
        bookmark_id = self._selected_bookmark_id()
        if bookmark_id is not None:
            self.play_bookmark_requested.emit(bookmark_id)

    def _on_loop_bookmark_clicked(self) -> None:
        bookmark_id = self._selected_bookmark_id()
        if bookmark_id is not None:
            self.loop_bookmark_requested.emit(bookmark_id)

    def _on_delete_bookmark_clicked(self) -> None:
        bookmark_ids = self._selected_bookmark_ids()
        if bookmark_ids:
            self.delete_bookmark_requested.emit(bookmark_ids)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        # Direct user request: "double clicking on a bookmark should play that
        # bookmark as well" -- same intent as Play Bookmark, just a faster gesture.
        bookmark_id = item.data(0, USER_ROLE)
        if bookmark_id is not None:
            self.play_bookmark_requested.emit(bookmark_id)

    def _move_selected(self, delta: int) -> None:
        """Moves the whole selected block up or down by one position, keeping the
        selected rows' relative order -- direct user request: "i should be able to
        multiple select and ... move". Moving up swaps each selected index with its
        upward neighbor top-to-bottom; moving down does the mirror image
        bottom-to-top, so earlier swaps never disturb indices not yet processed.
        """
        indices = self._selected_indices()
        if not indices:
            return
        count = self._tree.topLevelItemCount()
        if delta < 0 and indices[0] == 0:
            return
        if delta > 0 and indices[-1] == count - 1:
            return
        ordered_ids = [self._tree.topLevelItem(i).data(0, USER_ROLE) for i in range(count)]
        ordered_indices = indices if delta < 0 else list(reversed(indices))
        for index in ordered_indices:
            other = index + delta
            ordered_ids[other], ordered_ids[index] = ordered_ids[index], ordered_ids[other]
        self.reorder_requested.emit(ordered_ids)

    def _on_rows_moved(self, *_args) -> None:
        """Fires after Qt's own internal drag-drop reorder has already rearranged the
        tree's rows (including a multi-row drag) -- just read the new order back out
        and ask the caller to persist it, same as a Move Up/Down click.
        """
        ordered_ids = [self._tree.topLevelItem(i).data(0, USER_ROLE) for i in range(self._tree.topLevelItemCount())]
        self.reorder_requested.emit(ordered_ids)

    def set_bookmarks(self, bookmarks: list[Bookmark], song_names: dict[UUID, str] | None = None) -> None:
        """`bookmarks` is displayed in the order given -- the caller (Application,
        via BookmarkRepository.list_for_playlist) is responsible for ordering, so a
        manual reorder (see _move_selected/reorder_requested) actually sticks instead
        of being immediately re-sorted away by this panel re-deriving its own order.
        """
        previously_selected = set(self._selected_bookmark_ids())
        self._bookmarks = {b.id: b for b in bookmarks}
        self._song_names = song_names or {}
        self._tree.clear()
        for bookmark in bookmarks:
            loop_label = "∞" if bookmark.loop_enabled and bookmark.repeat_count is None else (
                f"×{bookmark.repeat_count}" if bookmark.loop_enabled else ""
            )
            row = QTreeWidgetItem(
                [
                    self._song_names.get(bookmark.media_id, ""),
                    bookmark.name,
                    format_timecode(bookmark.start_us),
                    format_timecode(bookmark.end_us) if bookmark.end_us is not None else "",
                    loop_label,
                ]
            )
            row.setData(0, USER_ROLE, bookmark.id)
            self._tree.addTopLevelItem(row)
        if previously_selected:
            self.select_bookmarks(previously_selected)

    def select_bookmark(self, bookmark_id: UUID) -> None:
        self.select_bookmarks({bookmark_id})

    def select_bookmarks(self, bookmark_ids: set[UUID]) -> None:
        for i in range(self._tree.topLevelItemCount()):
            row = self._tree.topLevelItem(i)
            row.setSelected(row.data(0, USER_ROLE) in bookmark_ids)

    def _on_selection_changed(self) -> None:
        ids = self._selected_bookmark_ids()
        if len(ids) == 1:
            self.bookmark_selected.emit(ids[0])
            bookmark = self._bookmarks.get(ids[0])
            self._set_playback_buttons_enabled(1, allow_loop=bookmark is not None and bookmark.end_us is not None)
        else:
            self._set_playback_buttons_enabled(len(ids), allow_loop=False)
