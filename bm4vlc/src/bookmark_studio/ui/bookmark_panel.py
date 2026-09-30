"""Bookmark list panel beneath the waveform (spec #7)."""
from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.timecode import format_timecode
from bookmark_studio.ui.qt_helpers import top_level_rows

# The list spans every song of the playlist, so "Song" says which track a bookmark
# belongs to. Loop/Gap/Fade In/Fade Out are editable in place (dropdowns).
COLUMNS = ["Song", "Name", "Start", "End", "Loop", "Gap", "Fade In", "Fade Out"]
USER_ROLE = 32
LOOP_COLUMN = COLUMNS.index("Loop")
GAP_COLUMN = COLUMNS.index("Gap")
FADE_IN_COLUMN = COLUMNS.index("Fade In")
FADE_OUT_COLUMN = COLUMNS.index("Fade Out")

# (label, loop_enabled, repeat_count) choices for the Loop column's dropdown.
_LOOP_CHOICES: list[tuple[str, bool, int | None]] = [
    ("Off", False, None),
    ("∞", True, None),
    ("×1", True, 1),
    ("×2", True, 2),
    ("×3", True, 3),
    ("×5", True, 5),
    ("×10", True, 10),
]
_MS_CHOICES = [0, 100, 250, 500, 1000, 1500, 2000, 3000, 5000]


def _loop_label(loop_enabled: bool, repeat_count: int | None) -> str:
    """Any repeat count gets a label (the old lookup showed e.g. ×4 as "Off")."""
    if not loop_enabled:
        return "Off"
    return "∞" if repeat_count is None else f"×{repeat_count}"


def _loop_from_label(label: str) -> tuple[bool, int | None] | None:
    label = label.strip()
    if label == "Off":
        return False, None
    if label == "∞":
        return True, None
    if label.startswith("×") and label[1:].isdigit() and int(label[1:]) >= 1:
        return True, int(label[1:])
    return None


def _ms_label(value_ms: int) -> str:
    return "Off" if value_ms <= 0 else f"{value_ms} ms"


def _ms_from_label(label: str) -> int:
    """Parses any "<N> ms" label, not just the preset ones."""
    text = label.strip()
    if text.endswith(" ms") and text[:-3].strip().isdigit():
        return int(text[:-3].strip())
    return 0


class _ComboColumnDelegate(QStyledItemDelegate):
    """Makes specific columns editable via a dropdown instead of free text --
    createEditor() returning None for every other column means Qt's item view
    simply won't enter edit mode there at all, so Song/Name/Start/End (still
    edited via the Inspector, not here) are unaffected.
    """

    def __init__(self, column_options: dict[int, list[str]], parent: QTreeWidget) -> None:
        super().__init__(parent)
        self._column_options = column_options

    def createEditor(self, parent, option, index):  # noqa: N802 - Qt override
        options = self._column_options.get(index.column())
        if options is None:
            return None
        combo = QComboBox(parent)
        combo.addItems(options)
        return combo

    def setEditorData(self, editor, index) -> None:  # noqa: N802 - Qt override
        if not isinstance(editor, QComboBox):
            super().setEditorData(editor, index)
            return
        current = index.data(Qt.ItemDataRole.DisplayRole) or ""
        position = editor.findText(current)
        if position < 0 and current:
            # A value set elsewhere (e.g. a 750 ms gap from the Inspector) that isn't one
            # of the presets. Offer it too, selected: falling back to the first entry
            # ("Off") would save "Off" over it as soon as the editor closes.
            editor.insertItem(1, current)
            position = 1
        editor.setCurrentIndex(max(position, 0))
        editor.showPopup()  # opens immediately -- "clicking would give a drop menu"

    def setModelData(self, editor, model, index) -> None:  # noqa: N802 - Qt override
        if not isinstance(editor, QComboBox):
            super().setModelData(editor, model, index)
            return
        if editor.currentText() != (index.data(Qt.ItemDataRole.DisplayRole) or ""):
            model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)


class BookmarkPanel(QWidget):
    bookmark_selected = Signal(object)  # UUID -- only emitted when exactly one row is selected
    export_requested = Signal()
    play_bookmark_requested = Signal(object)  # UUID
    loop_bookmark_requested = Signal(object)  # UUID
    delete_bookmark_requested = Signal(list)  # list of UUIDs -- one or more
    reorder_requested = Signal(list)  # ordered list of every bookmark UUID in the list
    # In-place edits of the Loop/Gap/Fade columns.
    loop_edited = Signal(object, bool, object)  # bookmark_id, loop_enabled, repeat_count|None
    gap_edited = Signal(object, int)  # bookmark_id, loop_gap_ms
    fade_in_edited = Signal(object, int)  # bookmark_id, fade_in_ms
    fade_out_edited = Signal(object, int)  # bookmark_id, fade_out_ms

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bookmarks: dict[UUID, Bookmark] = {}
        self._song_names: dict[UUID, str] = {}
        self._bookmark_order: list[Bookmark] = []
        self._shown_names: dict[UUID, str] = {}
        self._restoring = False
        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        # Plays a saved bookmark row -- unlike the transport bar (the player's playlist)
        # and Play/Loop Selection (a fresh drag-selection on the waveform). Play/Loop
        # act on one bookmark, so they are enabled only for a single selected row.
        self._play_bookmark_button = QPushButton("Play Bookmark", self)
        self._play_bookmark_button.setToolTip("Seek VLC to the selected bookmark and play")
        self._play_bookmark_button.clicked.connect(self._on_play_bookmark_clicked)
        toolbar.addWidget(self._play_bookmark_button)

        self._loop_bookmark_button = QPushButton("Loop Bookmark", self)
        self._loop_bookmark_button.setToolTip("Loop the selected bookmark using its own saved loop settings")
        self._loop_bookmark_button.clicked.connect(self._on_loop_bookmark_clicked)
        toolbar.addWidget(self._loop_bookmark_button)

        # Deletes every selected row (the Delete key and Bookmark menu do the same).
        self._delete_bookmark_button = QPushButton("Delete Bookmark", self)
        self._delete_bookmark_button.setToolTip("Delete every selected bookmark")
        self._delete_bookmark_button.clicked.connect(self._on_delete_bookmark_clicked)
        toolbar.addWidget(self._delete_bookmark_button)

        # Manual order (drag and drop does the same, see below).
        self._move_up_button = QPushButton("Move Up", self)
        self._move_up_button.setToolTip("Move every selected bookmark up in this list")
        self._move_up_button.clicked.connect(lambda: self._move_selected(-1))
        toolbar.addWidget(self._move_up_button)

        self._move_down_button = QPushButton("Move Down", self)
        self._move_down_button.setToolTip("Move every selected bookmark down in this list")
        self._move_down_button.clicked.connect(lambda: self._move_selected(1))
        toolbar.addWidget(self._move_down_button)

        toolbar.addStretch(1)
        # Same as File > Export Project, where it is easy to miss.
        self._export_button = QPushButton("Save Bookmarks...", self)
        self._export_button.setToolTip("Export this playlist's bookmarks to a .vlcbmk file")
        self._export_button.clicked.connect(self.export_requested.emit)
        toolbar.addWidget(self._export_button)
        layout.addLayout(toolbar)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(len(COLUMNS))
        self._tree.setHeaderLabels(COLUMNS)
        # Ctrl/Shift-click or a rubber-band drag selects several rows.
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Columns can be resized (Interactive) and dragged into another order (Movable).
        header = self._tree.header()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # Rows are reordered by dragging; InternalMove moves the whole selection together.
        self._tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._tree.setDragEnabled(True)
        self._tree.setAcceptDrops(True)
        self._tree.setDropIndicatorShown(True)
        self._tree.setRootIsDecorated(False)
        self._tree.model().rowsMoved.connect(self._on_rows_moved)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)

        # CurrentChanged opens a column's dropdown on the first click (SelectedClicked
        # would need the row selected first, which reads as "the dropdown doesn't
        # open"). DoubleClicked is left out: a double click plays the bookmark.
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.CurrentChanged | QAbstractItemView.EditTrigger.EditKeyPressed)
        loop_labels = [label for label, _enabled, _count in _LOOP_CHOICES]
        ms_labels = [_ms_label(ms) for ms in _MS_CHOICES]
        self._tree.setItemDelegate(
            _ComboColumnDelegate(
                {
                    LOOP_COLUMN: loop_labels,
                    GAP_COLUMN: ms_labels,
                    FADE_IN_COLUMN: ms_labels,
                    FADE_OUT_COLUMN: ms_labels,
                },
                self._tree,
            )
        )
        self._tree.itemChanged.connect(self._on_item_changed)
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
        # Same as Play Bookmark.
        bookmark_id = item.data(0, USER_ROLE)
        if bookmark_id is not None:
            self.play_bookmark_requested.emit(bookmark_id)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Fires when the Loop/Gap/Fade In/Fade Out combo delegate commits a choice.
        Only those four columns are editable (see _ComboColumnDelegate).
        """
        if column not in (LOOP_COLUMN, GAP_COLUMN, FADE_IN_COLUMN, FADE_OUT_COLUMN):
            return
        bookmark_id = item.data(0, USER_ROLE)
        bookmark = self._bookmarks.get(bookmark_id)
        if bookmark is None:
            return

        if column == LOOP_COLUMN:
            if bookmark.end_us is None:
                # A point bookmark has nothing to loop over -- reject and revert,
                # same "reject and revert the field" convention Inspector uses for
                # an invalid Start/End edit.
                item.setText(LOOP_COLUMN, _loop_label(bookmark.loop_enabled, bookmark.repeat_count))
                return
            parsed = _loop_from_label(item.text(LOOP_COLUMN))
            if parsed is not None and parsed != (bookmark.loop_enabled, bookmark.repeat_count):
                self.loop_edited.emit(bookmark_id, parsed[0], parsed[1])
        elif column == GAP_COLUMN:
            self.gap_edited.emit(bookmark_id, _ms_from_label(item.text(GAP_COLUMN)))
        elif column == FADE_IN_COLUMN:
            self.fade_in_edited.emit(bookmark_id, _ms_from_label(item.text(FADE_IN_COLUMN)))
        elif column == FADE_OUT_COLUMN:
            self.fade_out_edited.emit(bookmark_id, _ms_from_label(item.text(FADE_OUT_COLUMN)))

    def _move_selected(self, delta: int) -> None:
        """Moves the whole selected block up or down by one position, keeping the
        selected rows' relative order. Moving up swaps each selected index with its
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
        ordered_ids = [row.data(0, USER_ROLE) for row in top_level_rows(self._tree)]
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
        ordered_ids = [row.data(0, USER_ROLE) for row in top_level_rows(self._tree)]
        self.reorder_requested.emit(ordered_ids)

    def set_bookmarks(self, bookmarks: list[Bookmark], song_names: dict[UUID, str] | None = None) -> None:
        """`bookmarks` is displayed in the order given -- the caller (Application,
        via BookmarkRepository.list_for_playlist) is responsible for ordering, so a
        manual reorder (see _move_selected/reorder_requested) actually sticks instead
        of being immediately re-sorted away by this panel re-deriving its own order.

        A refresh that changes nothing is a no-op: a rebuild re-selects the previous
        rows, which re-emits bookmark_selected (the Inspector would reload and lose
        half-typed text, and the view would jump back to that bookmark's song) -- on
        every ~2 s playlist poll.
        """
        song_names = dict(song_names or {})
        shown_names = {b.media_id: song_names.get(b.media_id, "") for b in bookmarks}
        if list(bookmarks) == self._bookmark_order and shown_names == self._shown_names:
            return
        previously_selected = set(self._selected_bookmark_ids())
        current_ids = [b.id for b in bookmarks]
        refit_columns = current_ids != [b.id for b in self._bookmark_order] or shown_names != self._shown_names
        scroll_value = self._tree.verticalScrollBar().value()

        self._bookmarks = {b.id: b for b in bookmarks}
        self._bookmark_order = list(bookmarks)
        self._song_names = song_names
        self._shown_names = shown_names
        self._restoring = True
        try:
            self._tree.clear()
            for bookmark in bookmarks:
                row = QTreeWidgetItem(
                    [
                        shown_names.get(bookmark.media_id, ""),
                        bookmark.name,
                        format_timecode(bookmark.start_us),
                        format_timecode(bookmark.end_us) if bookmark.end_us is not None else "",
                        _loop_label(bookmark.loop_enabled, bookmark.repeat_count),
                        _ms_label(bookmark.loop_gap_ms),
                        _ms_label(bookmark.fade_in_ms),
                        _ms_label(bookmark.fade_out_ms),
                    ]
                )
                row.setData(0, USER_ROLE, bookmark.id)
                # Without ItemIsEditable, Qt never opens the Loop/Gap/Fade dropdown
                # editors at all -- QTreeWidgetItem isn't editable by default.
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsEditable)
                self._tree.addTopLevelItem(row)
            # Columns fit their contents only when the rows changed, so a width the user
            # set survives ordinary refreshes.
            if refit_columns:
                for column in range(len(COLUMNS)):
                    self._tree.resizeColumnToContents(column)
            if previously_selected:
                self.select_bookmarks(previously_selected)
            self._tree.verticalScrollBar().setValue(scroll_value)
        finally:
            self._restoring = False
        self._update_buttons_for_selection()


    def select_bookmark(self, bookmark_id: UUID) -> None:
        self.select_bookmarks({bookmark_id})

    def select_bookmarks(self, bookmark_ids: set[UUID]) -> None:
        for row in top_level_rows(self._tree):
            row.setSelected(row.data(0, USER_ROLE) in bookmark_ids)

    def _on_selection_changed(self) -> None:
        if self._restoring:
            return  # re-selecting after a refresh is not a user selection
        ids = self._selected_bookmark_ids()
        if len(ids) == 1:
            self.bookmark_selected.emit(ids[0])
        self._update_buttons_for_selection()

    def _update_buttons_for_selection(self) -> None:
        ids = self._selected_bookmark_ids()
        if len(ids) == 1:
            bookmark = self._bookmarks.get(ids[0])
            self._set_playback_buttons_enabled(1, allow_loop=bookmark is not None and bookmark.end_us is not None)
        else:
            self._set_playback_buttons_enabled(len(ids), allow_loop=False)
