"""Bookmark list panel beneath the waveform (spec #7): the list in a "Bookmarks" tab, and
its Delete/Move/Save buttons stacked beside it (towards the Bookmark Studio tab). A
double-click plays a bookmark; looping it is the 🔁 among the playback buttons, which
follows this list's selection (loop_target_changed)."""
from __future__ import annotations

import time
from uuid import UUID

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import CompletionAction
from bookmark_studio.domain.timecode import format_timecode
from bookmark_studio.ui.header_columns import HeaderColumns
from bookmark_studio.ui.inspector import COMPLETION_LABELS, OFFERED_COMPLETION_ACTIONS
from bookmark_studio.ui.qt_helpers import style_tabs, top_level_rows

# The list spans every song of the playlist, so "Song" says which track a bookmark
# belongs to. Loop/Gap/Fade In/Fade Out are editable in place (dropdowns). Tags came in
# 0.7.0: added last, so every other column keeps its number, and shown before Name. The
# user can drag columns into any order; MainWindow saves the header's state.
# After Loop came in 0.9.0, also added last and shown after Loop.
COLUMNS = ["Song", "Name", "Start", "End", "Loop", "Gap", "Fade In", "Fade Out", "Tags", "After Loop"]
TAGS_COLUMN = COLUMNS.index("Tags")
AFTER_LOOP_COLUMN = COLUMNS.index("After Loop")
DEFAULT_ORDER = ["Song", "Tags", "Name", "Start", "End", "Loop", "After Loop", "Gap", "Fade In", "Fade Out"]
# Saved column layouts from before a column existed have this many columns (0.7.0-0.8.0).
LEGACY_SAVED_COLUMNS = 9
USER_ROLE = 32
FLASH_ROLE = USER_ROLE + 1  # set while a row shows the orange "saved" flash
SAVED_ROW_COLOR = QColor(255, 159, 26)
SAVED_TEXT_COLOR = QColor(26, 26, 26)
PLAYBACK_ROLE = USER_ROLE + 2  # "playing" / "done": the bookmark playing, or the one played last
PLAYBACK_ROW_COLORS = {"playing": QColor("#8fd98f"), "done": QColor("#ffe27a")}
# Set while the playhead of a song playing (not a bookmark) is inside the bookmark's range.
CROSSING_ROLE = USER_ROLE + 3
CROSSING_ROW_COLOR = SAVED_ROW_COLOR  # orange
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


loop_label = _loop_label  # (the BM playback view's tracks say it the list's way)


def _loop_from_label(label: str) -> tuple[bool, int | None] | None:
    label = label.strip()
    if label == "Off":
        return False, None
    if label == "∞":
        return True, None
    if label.startswith("×") and label[1:].isdigit() and int(label[1:]) >= 1:
        return True, int(label[1:])
    return None


def _completion_from_label(label: str) -> CompletionAction | None:
    for action, text in COMPLETION_LABELS.items():
        if text == label.strip():
            return action
    return None


def _ms_label(value_ms: int) -> str:
    return "Off" if value_ms <= 0 else f"{value_ms} ms"


def _ms_from_label(label: str) -> int:
    """Parses any "<N> ms" label, not just the preset ones."""
    text = label.strip()
    if text.endswith(" ms") and text[:-3].strip().isdigit():
        return int(text[:-3].strip())
    return 0


def widen_popup(combo: QComboBox) -> None:
    """The dropdown's list as wide as its longest choice -- an in-place editor is only as
    wide as its column, and the list would cut the choices off (reported on 0.9.0)."""
    view = combo.view()
    widest = max((combo.fontMetrics().horizontalAdvance(combo.itemText(i)) for i in range(combo.count())), default=0)
    margins = view.contentsMargins()
    scroll = view.verticalScrollBar().sizeHint().width() if combo.count() > combo.maxVisibleItems() else 0
    view.setMinimumWidth(widest + 32 + scroll + margins.left() + margins.right())


class _ComboColumnDelegate(QStyledItemDelegate):
    """Makes specific columns editable via a dropdown instead of free text --
    createEditor() returning None for every other column means Qt's item view
    simply won't enter edit mode there at all, so Song/Name/Start/End (still
    edited via the Inspector, not here) are unaffected.
    """

    def __init__(self, column_options: dict[int, list[str]], parent: QTreeWidget) -> None:
        super().__init__(parent)
        self._column_options = column_options

    def paint(self, painter, option, index) -> None:  # noqa: N802 - Qt override
        # A row whose change was just saved shows orange -- also when it is the selected
        # row (as the edited one usually is), which a plain background brush would not.
        # Below the flash: green while the bookmark plays, yellow once it has played.
        if index.data(FLASH_ROLE):
            color = SAVED_ROW_COLOR
        elif index.data(CROSSING_ROLE):
            color = CROSSING_ROW_COLOR
        elif index.data(PLAYBACK_ROLE) in PLAYBACK_ROW_COLORS:
            color = PLAYBACK_ROW_COLORS[index.data(PLAYBACK_ROLE)]
        else:
            super().paint(painter, option, index)
            return
        flashed = QStyleOptionViewItem(option)
        self.initStyleOption(flashed, index)
        flashed.state &= ~QStyle.StateFlag.State_Selected
        flashed.backgroundBrush = QBrush(color)
        flashed.palette.setColor(QPalette.ColorRole.Text, SAVED_TEXT_COLOR)
        style = flashed.widget.style() if flashed.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, flashed, painter, flashed.widget)

    def createEditor(self, parent, option, index):  # noqa: N802 - Qt override
        options = self._column_options.get(index.column())
        if options is None:
            return None
        combo = QComboBox(parent)
        combo.addItems(options)
        widen_popup(combo)
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
        widen_popup(editor)  # (the value inserted above may be the longest)
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
    extract_requested = Signal(list)  # the selected bookmarks' ids, in list order
    duplicate_requested = Signal(list)  # the selected bookmarks' ids, in list order
    # The columns shown, or their fitted widths, changed: the window may make room for
    # them. True: the user's doing (a column shown, the defaults back).
    columns_changed = Signal(bool)
    play_bookmark_requested = Signal(object)  # UUID
    loop_bookmark_requested = Signal(object)  # UUID
    delete_bookmark_requested = Signal(list)  # list of UUIDs -- one or more
    reorder_requested = Signal(list)  # ordered list of every bookmark UUID in the list
    # In-place edits of the Loop/Gap/Fade columns.
    loop_edited = Signal(object, bool, object)  # bookmark_id, loop_enabled, repeat_count|None
    gap_edited = Signal(object, int)  # bookmark_id, loop_gap_ms
    fade_in_edited = Signal(object, int)  # bookmark_id, fade_in_ms
    fade_out_edited = Signal(object, int)  # bookmark_id, fade_out_ms
    completion_edited = Signal(object, object)  # bookmark_id, CompletionAction (the After Loop column)
    # The one selected bookmark that can loop (it has a range), or None: what the 🔁
    # among the playback buttons acts on.
    loop_target_changed = Signal(object)
    selection_changed = Signal()  # any change of the rows selected

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bookmarks: dict[UUID, Bookmark] = {}
        self._song_names: dict[UUID, str] = {}
        self._bookmark_order: list[Bookmark] = []
        self._shown_names: dict[UUID, str] = {}
        self._restoring = False
        # Columns fit their contents until the user (or a saved layout) sizes them.
        self._fitting = False
        self._user_sized_columns = False
        # Rows showing the orange "saved" flash: bookmark id -> when it ends (monotonic s).
        self._flashing: dict[UUID, float] = {}
        self._playback: tuple[UUID, str] | None = None  # the bookmark playing / played last
        self._crossing: frozenset[UUID] = frozenset()  # the playhead inside their range
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._expire_flashes)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._tabs = QTabWidget(self)
        self._tabs.setDocumentMode(True)
        style_tabs(self._tabs)
        layout.addWidget(self._tabs, 1)

        # The buttons, stacked between this list and the Bookmark / Volume & EQ tabs.
        self.action_column = QFrame(self)
        actions = QVBoxLayout(self.action_column)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(4)
        layout.addWidget(self.action_column)

        def button(text: str, tooltip: str) -> QPushButton:
            widget = QPushButton(text, self.action_column)
            widget.setToolTip(tooltip)
            actions.addWidget(widget)
            return widget

        # (No Play/Loop here since 0.9.0: a double-click plays a row, and the 🔁 among the
        # playback buttons loops the selected one -- see loop_target_changed.)
        self._loop_target: UUID | None = None
        # Manual order (drag and drop does the same, see below).
        self._move_up_button = button("Move up", "Move every selected bookmark up in this list")
        self._move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self._move_down_button = button("Move down", "Move every selected bookmark down in this list")
        self._move_down_button.clicked.connect(lambda: self._move_selected(1))
        actions.addSpacing(10)
        # Copies of the selected bookmarks, each with a new name of its own, at the top of
        # the list like every new bookmark (main window, Ctrl+D).
        self._duplicate_button = button("Duplicate", "Copy the selected bookmarks (Ctrl+D)")
        self._duplicate_button.clicked.connect(self.request_duplicate)
        # Same as File > Export Project, where it is easy to miss.
        self._export_button = button("Save...", "Save this playlist's bookmarks to a .vlcbmk file")
        self._export_button.clicked.connect(self.export_requested.emit)
        # The selected bookmarks as audio files of their own (the Extract audio window).
        self._extract_button = button("Extract...", "Save the selected bookmarks as audio files")
        self._extract_button.clicked.connect(self.request_extract)
        # Deletes every selected row (the Delete key and Bookmark menu do the same).
        # Below Save... (0.9.0), away from the buttons that only move rows.
        self._delete_bookmark_button = button("Delete", "Delete every selected bookmark (Delete)")
        self._delete_bookmark_button.clicked.connect(self._on_delete_bookmark_clicked)
        actions.addStretch(1)

        self._tree = QTreeWidget(self)
        self._tabs.addTab(self._tree, "Bookmarks")
        # Level with the tab pages, below the tab bars.
        actions.setContentsMargins(0, self._tabs.tabBar().sizeHint().height() + 2, 0, 0)
        self._tree.setColumnCount(len(COLUMNS))
        self._tree.setHeaderLabels(COLUMNS)
        # Ctrl/Shift-click or a rubber-band drag selects several rows.
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Columns can be resized (Interactive) and dragged into another order; a right-click
        # on a column title: which columns show, and in which order.
        header = self._tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._columns = HeaderColumns(self._tree, COLUMNS, DEFAULT_ORDER, fit=self._fit_default_columns)
        self._columns.changed.connect(self.columns_changed.emit)
        self._columns.apply_default_order()
        header.sectionResized.connect(self._on_section_resized)
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
                    AFTER_LOOP_COLUMN: [COMPLETION_LABELS[action] for action in OFFERED_COMPLETION_ACTIONS],
                    GAP_COLUMN: ms_labels,
                    FADE_IN_COLUMN: ms_labels,
                    FADE_OUT_COLUMN: ms_labels,
                },
                self._tree,
            )
        )
        self._tree.itemChanged.connect(self._on_item_changed)

        self._set_playback_buttons_enabled(0, allow_loop=False)

    # -- column layout (see HeaderColumns) --

    def _on_section_resized(self, *_args) -> None:
        if not self._fitting:
            self._user_sized_columns = True

    def column_menu(self, clicked: int = -1) -> QMenu:
        return self._columns.menu(clicked)

    def columns_short_by(self) -> int:
        """How many pixels wider the list would have to be to show every column shown."""
        header = self._tree.header()
        needed = sum(header.sectionSize(c) for c in range(header.count()) if not header.isSectionHidden(c))
        return max(0, needed - self._tree.viewport().width())

    def row_count(self) -> int:
        return self._tree.topLevelItemCount()

    def column_order(self) -> list[int]:
        return self._columns.order()

    def shown_columns(self) -> list[str]:
        return self._columns.shown()

    def move_column(self, logical: int, step: int) -> None:
        self._columns.move(logical, step)

    def set_column_shown(self, logical: int, shown: bool) -> None:
        self._columns.set_shown(logical, shown)

    def set_column_layout(self, arrangement: list[tuple[int, bool]]) -> None:
        self._columns.set_layout(arrangement)

    def arrange_columns(self, clicked: int = -1) -> None:
        self._columns.arrange(clicked)

    def restore_default_columns(self) -> None:
        self._columns.restore_defaults()

    def _fit_default_columns(self) -> None:
        """The defaults back: the columns fit their contents again."""
        self._user_sized_columns = False
        self._fitting = True
        try:
            for column in range(len(COLUMNS)):
                self._tree.resizeColumnToContents(column)
        finally:
            self._fitting = False

    # -- green while a bookmark plays, yellow once it has played --

    def set_bookmark_playback(self, bookmark_id: UUID | None, state: str | None) -> None:
        self._playback = (bookmark_id, state) if bookmark_id is not None and state else None
        self._apply_playback()

    def playback_state(self, bookmark_id: UUID) -> str | None:
        playback = self._playback
        return playback[1] if playback is not None and playback[0] == bookmark_id else None

    def _apply_playback(self) -> None:
        playback = self._playback
        self._tree.blockSignals(True)  # not an edit (see _apply_flashes)
        try:
            for row in top_level_rows(self._tree):
                state = playback[1] if playback is not None and row.data(0, USER_ROLE) == playback[0] else None
                if row.data(0, PLAYBACK_ROLE) != state:
                    for column in range(len(COLUMNS)):
                        row.setData(column, PLAYBACK_ROLE, state)
        finally:
            self._tree.blockSignals(False)

    # -- orange while the playhead crosses the bookmark's range (a song playing) --

    def set_crossing(self, bookmark_ids: frozenset[UUID]) -> None:
        """The bookmarks whose range the playhead is in while a song plays from the
        playlist (requested on 0.9.0): orange for as long as it is; one it enters is
        scrolled into view (the selection stays as it is)."""
        if bookmark_ids == self._crossing:
            return
        entered = bookmark_ids - self._crossing
        self._crossing = bookmark_ids
        self._apply_crossing()
        for row in top_level_rows(self._tree):
            if row.data(0, USER_ROLE) in entered:
                self._tree.scrollToItem(row, QAbstractItemView.ScrollHint.EnsureVisible)
                break

    def crossing(self) -> frozenset[UUID]:
        return self._crossing

    def _apply_crossing(self) -> None:
        self._tree.blockSignals(True)  # not an edit (see _apply_flashes)
        try:
            for row in top_level_rows(self._tree):
                crossing = row.data(0, USER_ROLE) in self._crossing
                if bool(row.data(0, CROSSING_ROLE)) != crossing:
                    for column in range(len(COLUMNS)):
                        row.setData(column, CROSSING_ROLE, crossing or None)
        finally:
            self._tree.blockSignals(False)

    # -- the orange "saved" flash --

    def flash_bookmark(self, bookmark_id: UUID, duration_ms: int = 2000) -> None:
        """Shows the bookmark's row in orange for a moment: a change was saved. Survives
        the list being rebuilt in the meantime."""
        self._flashing[bookmark_id] = time.monotonic() + duration_ms / 1000
        self._apply_flashes()
        self._flash_timer.start(duration_ms)

    def is_flashing(self, bookmark_id: UUID) -> bool:
        return self._flashing.get(bookmark_id, 0.0) > time.monotonic()

    def _apply_flashes(self) -> None:
        now = time.monotonic()
        # Not an edit: itemChanged would otherwise save the Gap/Fade columns again (and
        # flash again, and again).
        self._tree.blockSignals(True)
        try:
            for row in top_level_rows(self._tree):
                flashing = self._flashing.get(row.data(0, USER_ROLE), 0.0) > now
                if bool(row.data(0, FLASH_ROLE)) != flashing:
                    for column in range(len(COLUMNS)):
                        row.setData(column, FLASH_ROLE, flashing or None)
        finally:
            self._tree.blockSignals(False)

    def _expire_flashes(self) -> None:
        now = time.monotonic()
        self._flashing = {k: v for k, v in self._flashing.items() if v > now}
        self._apply_flashes()
        if self._flashing:
            self._flash_timer.start(max(10, int((min(self._flashing.values()) - now) * 1000)))

    def header_state(self) -> QByteArray:
        """The columns' order and widths (saved with the window layout)."""
        return self._tree.header().saveState()

    def restore_header_state(self, state: QByteArray, *, saved_columns: int = LEGACY_SAVED_COLUMNS) -> bool:
        """`saved_columns`: how many columns the layout was saved with. Columns added since
        (Qt puts them last) go where they belong by default, e.g. After Loop after Loop;
        the user's own order of the others stays."""
        header = self._tree.header()
        if not header.restoreState(state) or header.count() != len(COLUMNS):
            self._columns.apply_default_order()
            return False
        for logical in range(saved_columns, len(COLUMNS)):
            name = COLUMNS[logical]
            before = DEFAULT_ORDER[DEFAULT_ORDER.index(name) - 1] if DEFAULT_ORDER.index(name) > 0 else None
            target = header.visualIndex(COLUMNS.index(before)) + 1 if before is not None else 0
            current = header.visualIndex(logical)
            header.moveSection(current, target if target <= current else target - 1)
        self._user_sized_columns = True  # saved widths are not refitted
        return True

    def column_count(self) -> int:
        return len(COLUMNS)

    def _set_playback_buttons_enabled(self, selected_count: int, *, allow_loop: bool) -> None:
        self._delete_bookmark_button.setEnabled(selected_count > 0)
        self._duplicate_button.setEnabled(selected_count > 0)
        self._extract_button.setEnabled(any(
            bookmark is not None and bookmark.end_us is not None
            for bookmark in (self._bookmarks.get(i) for i in self._selected_bookmark_ids())
        ))
        self._update_move_buttons_enabled()
        ids = self._selected_bookmark_ids()
        target = ids[0] if selected_count == 1 and allow_loop and ids else None
        if target != self._loop_target:
            self._loop_target = target
            self.loop_target_changed.emit(target)

    def _selected_in_list_order(self) -> list[UUID]:
        return [row.data(0, USER_ROLE) for row in top_level_rows(self._tree) if row.isSelected()]

    def request_extract(self) -> None:
        """Extract...: asks for the selected bookmarks to be saved as audio files."""
        ids = self._selected_in_list_order()
        if ids:
            self.extract_requested.emit(ids)

    def request_duplicate(self) -> None:
        """Duplicate: asks for copies of the selected bookmarks."""
        ids = self._selected_in_list_order()
        if ids:
            self.duplicate_requested.emit(ids)

    def selected_bookmark_id(self) -> UUID | None:
        """The one bookmark selected, or None (none or several)."""
        return self._selected_bookmark_id()

    def loop_target(self) -> UUID | None:
        """The selected bookmark the 🔁 would loop, or None."""
        return self._loop_target

    def loop_selected(self) -> None:
        """The 🔁 among the playback buttons: loops the selected bookmark."""
        if self._loop_target is not None:
            self.loop_bookmark_requested.emit(self._loop_target)

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

    def _on_delete_bookmark_clicked(self) -> None:
        bookmark_ids = self._selected_bookmark_ids()
        if bookmark_ids:
            self.delete_bookmark_requested.emit(bookmark_ids)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        # Plays the bookmark (with its loop, if it has one).
        bookmark_id = item.data(0, USER_ROLE)
        if bookmark_id is not None:
            self.play_bookmark_requested.emit(bookmark_id)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Fires when the Loop/Gap/Fade In/Fade Out combo delegate commits a choice.
        Only those four columns are editable (see _ComboColumnDelegate).
        """
        if column not in (LOOP_COLUMN, AFTER_LOOP_COLUMN, GAP_COLUMN, FADE_IN_COLUMN, FADE_OUT_COLUMN):
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
        elif column == AFTER_LOOP_COLUMN:
            action = _completion_from_label(item.text(AFTER_LOOP_COLUMN))
            if action is not None and action is not bookmark.completion_action:
                self.completion_edited.emit(bookmark_id, action)
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
                        ", ".join(bookmark.tags),
                        COMPLETION_LABELS[bookmark.completion_action],
                    ]
                )
                row.setData(0, USER_ROLE, bookmark.id)
                # Without ItemIsEditable, Qt never opens the Loop/Gap/Fade dropdown
                # editors at all -- QTreeWidgetItem isn't editable by default.
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsEditable)
                self._tree.addTopLevelItem(row)
            # Columns fit their contents only when the rows changed, and only until the
            # user sizes one (or a saved layout is restored): their widths stay.
            if refit_columns and not self._user_sized_columns:
                self._fitting = True
                try:
                    for column in range(len(COLUMNS)):
                        self._tree.resizeColumnToContents(column)
                finally:
                    self._fitting = False
                self.columns_changed.emit(False)
            if previously_selected:
                self.select_bookmarks(previously_selected)
            self._tree.verticalScrollBar().setValue(scroll_value)
            self._apply_flashes()
            self._apply_playback()
            self._apply_crossing()
        finally:
            self._restoring = False
        self._update_buttons_for_selection()


    def select_bookmark(self, bookmark_id: UUID) -> None:
        self.select_bookmarks({bookmark_id})

    def select_bookmarks(self, bookmark_ids: set[UUID]) -> None:
        shown = False
        for row in top_level_rows(self._tree):
            selected = row.data(0, USER_ROLE) in bookmark_ids
            row.setSelected(selected)
            if selected and not shown:
                self._tree.scrollToItem(row)
                shown = True

    def _on_selection_changed(self) -> None:
        if self._restoring:
            return  # re-selecting after a refresh is not a user selection
        ids = self._selected_bookmark_ids()
        if len(ids) == 1:
            self.bookmark_selected.emit(ids[0])
        self._update_buttons_for_selection()
        self.selection_changed.emit()

    def _update_buttons_for_selection(self) -> None:
        ids = self._selected_bookmark_ids()
        if len(ids) == 1:
            bookmark = self._bookmarks.get(ids[0])
            self._set_playback_buttons_enabled(1, allow_loop=bookmark is not None and bookmark.end_us is not None)
        else:
            self._set_playback_buttons_enabled(len(ids), allow_loop=False)
