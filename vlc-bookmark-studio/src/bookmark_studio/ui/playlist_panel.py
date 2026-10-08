"""VLC playlist sidebar: filter, bookmark-count column, Follow VLC mode (spec #145-#148);
since 0.9.0 in a Source Playlist tab, with the bookmark list's column menu; since 0.10.0
everything is in the tab -- the playlist's name, the connection, Launch VLC... and Quit,
Follow, the filter and the list (Title and Bookmarks shown by default)."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QSignalBlocker, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bookmark_studio.playback.status import VlcPlaylistItem
from bookmark_studio.ui.header_columns import HeaderColumns
from bookmark_studio.ui.qt_helpers import style_tabs, top_level_rows

COLUMNS = ["Title", "Artist", "Duration", "Bookmarks", "Status"]
# Shown again from the column menu (0.10.0: Title and Bookmarks only, by default).
HIDDEN_BY_DEFAULT = ("Artist", "Duration", "Status")

# The current song's row is tinted: green while it plays, blue while it is only
# loaded (paused/stopped).
_ACTIVELY_PLAYING_COLOR = QColor("#8fd98f")
_CURRENT_NOT_PLAYING_COLOR = QColor("#a9c9f5")
# The song of the bookmark playing (green) or played last (yellow) -- like the bookmark's
# row in the list and its area on the waveform.
_BOOKMARK_SONG_COLORS = {"playing": QColor("#8fd98f"), "done": QColor("#ffe27a")}
_NOT_CURRENT_BRUSH = QBrush()


class PlaylistPanel(QWidget):
    item_selected = Signal(int)  # vlc_id
    item_double_clicked = Signal(int)  # vlc_id -- play in VLC (spec #147)
    follow_vlc_toggled = Signal(bool)
    launch_vlc_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[VlcPlaylistItem] = []
        self._bookmark_counts: dict[int, int] = {}
        self._current_playing_id: int | None = None
        self._is_actively_playing = False
        self._bookmark_song: tuple[int, str] | None = None  # (vlc_id, "playing" | "done")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # The player's playlist in a tab of its own (0.9.0), holding everything (0.10.0):
        # the playlist's name, the connection, Launch VLC... and Quit, Follow, the filter
        # and the list.
        self._tabs = QTabWidget(self)
        self._tabs.setDocumentMode(True)
        style_tabs(self._tabs)
        layout.addWidget(self._tabs, 1)
        page = QWidget(self._tabs)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 6, 0, 0)
        page_layout.setSpacing(6)
        self._tabs.addTab(page, "Source Playlist")

        # The source playlist's name (0.10.0): the .m3u it came from, or the name the app
        # gave a playlist VLC had open ("Unsaved VLC Playlist <date>").
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Playlist", page))
        self._playlist_name = QLineEdit(page)
        self._playlist_name.setReadOnly(True)
        self._playlist_name.setPlaceholderText("No playlist yet")
        self._playlist_name.setToolTip("The source playlist: the .m3u it was opened from, or the name "
                                       "given to a playlist VLC had open")
        name_row.addWidget(self._playlist_name, 1)
        page_layout.addLayout(name_row)

        # Connection status, above the button that makes the connection.
        self._connection_label = QLabel("● Offline", page)
        connection_font = QFont()
        connection_font.setPointSize(10)
        connection_font.setBold(True)
        self._connection_label.setFont(connection_font)
        self._connection_label.setStyleSheet("color: #a33;")
        page_layout.addWidget(self._connection_label)

        # Opens VlcLaunchDialog: attach to a running VLC, launch one with a playlist,
        # or play inside the app.
        self._launch_vlc_button = QPushButton("Launch VLC...", page)
        self._launch_vlc_button.setToolTip(
            "Attach to an already-open VLC instance, or launch a new one with a playlist"
        )
        self._launch_vlc_button.clicked.connect(self.launch_vlc_requested.emit)
        self._quit_button = QPushButton("Quit", page)
        self._quit_button.setToolTip(
            "Save everything, close the VLC this app launched, and quit (Ctrl+Q)"
        )
        self._quit_button.clicked.connect(self.quit_requested.emit)
        session_row = QHBoxLayout()
        session_row.addWidget(self._launch_vlc_button, 1)
        session_row.addWidget(self._quit_button)
        page_layout.addLayout(session_row)

        self._follow_checkbox = QCheckBox("Follow currently playing VLC song", page)
        self._follow_checkbox.setChecked(True)
        self._follow_checkbox.toggled.connect(self.follow_vlc_toggled.emit)
        page_layout.addWidget(self._follow_checkbox)

        # Directly above the tree it filters.
        self._filter_edit = QLineEdit(page)
        self._filter_edit.setPlaceholderText("Filter...")
        self._filter_edit.textChanged.connect(self._apply_filter)
        page_layout.addWidget(self._filter_edit)

        self._tree = QTreeWidget(page)
        self._tree.setColumnCount(len(COLUMNS))
        self._tree.setHeaderLabels(COLUMNS)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        page_layout.addWidget(self._tree)
        # Right-click a column title: which columns show, and in which order -- as in the
        # bookmark list (0.9.0).
        self._columns = HeaderColumns(self._tree, COLUMNS, COLUMNS, hidden=HIDDEN_BY_DEFAULT)
        self._columns.apply_default_order()

    def set_playlist(
        self, items: list[VlcPlaylistItem], bookmark_counts: dict[int, int] | None = None
    ) -> None:
        # Skip the rebuild entirely when nothing actually changed -- called on every
        # ~2s playlist poll regardless, and a full _tree.clear() + repopulate tears
        # down and recreates every row, wiping the current selection and scroll
        # position each time even when the playlist is identical. Same class of bug
        # fixed in set_current_playing() below, just on a longer, less noticeable cycle.
        normalized_counts = bookmark_counts or {}
        if items == self._items and normalized_counts == self._bookmark_counts:
            return
        self._items = items
        self._bookmark_counts = normalized_counts
        self._rebuild()

    def set_current_playing(self, vlc_id: int | None, *, is_playing: bool = False) -> None:
        """Updates only the playing-row highlight on the existing rows. Rebuilding the
        rows here (every status poll) would replace the items between the two clicks
        of a double-click, which Qt then never reports, and would reset the selection
        and scroll position.

        `is_playing` is the player's actual state (green) as opposed to the song
        merely being loaded (blue).
        """
        if vlc_id == self._current_playing_id and is_playing == self._is_actively_playing:
            return
        self._current_playing_id = vlc_id
        self._is_actively_playing = is_playing
        self._paint_rows()

    def set_bookmark_song(self, vlc_id: int | None, state: str | None) -> None:
        """The song of the bookmark that is playing (green) or played last (yellow)."""
        song = (vlc_id, state) if vlc_id is not None and state else None
        if song != self._bookmark_song:
            self._bookmark_song = song
            self._paint_rows()

    def bookmark_song(self) -> tuple[int, str] | None:
        return self._bookmark_song

    def _paint_rows(self) -> None:
        for row in top_level_rows(self._tree):
            vlc_id = row.data(0, 32)
            bookmark_state = self._bookmark_song[1] if self._bookmark_song and self._bookmark_song[0] == vlc_id else None
            _set_row_playing(row, vlc_id == self._current_playing_id, self._is_actively_playing, bookmark_state)

    def select_item(self, vlc_id: int | None) -> None:
        """Highlights the row for `vlc_id` (selecting a bookmark selects its song).
        Setting the current item fires the normal
        itemSelectionChanged -> item_selected signal chain, so Application's existing
        _on_playlist_item_selected handles the actual waveform/follow-state switch;
        this method only needs to move the highlight.
        """
        if vlc_id is None:
            self._tree.clearSelection()
            return
        for row in top_level_rows(self._tree):
            if row.data(0, 32) == vlc_id:
                self._tree.setCurrentItem(row)
                return

    def set_playlist_name(self, name: str | None) -> None:
        self._playlist_name.setText(name or "")
        self._playlist_name.setCursorPosition(0)  # a long name shows its start
        self._playlist_name.setToolTip(name or "")

    def playlist_name(self) -> str:
        return self._playlist_name.text()

    def set_connected(self, connected: bool) -> None:
        if connected:
            self._connection_label.setText("● Connected")
            self._connection_label.setStyleSheet("color: #2a2;")
        else:
            self._connection_label.setText("● Offline")
            self._connection_label.setStyleSheet("color: #a33;")

    def set_follow_vlc(self, enabled: bool, *, notify: bool = True) -> None:
        """Programmatic version of the checkbox -- used when previewing a different,
        not-currently-playing song single-clicks the checkbox off (see
        Application._on_playlist_item_selected) so live playback progression doesn't
        yank the waveform view away from what the user just chose to look at.

        notify=False changes the box without emitting follow_vlc_toggled: for callers
        that are about to switch the displayed song themselves, where the toggle
        handler's own "snap back to the playing song" would load the wrong song first.
        """
        blocker = QSignalBlocker(self._follow_checkbox) if not notify else None
        try:
            self._follow_checkbox.setChecked(enabled)
        finally:
            if blocker is not None:
                blocker.unblock()

    def follow_vlc_enabled(self) -> bool:
        return self._follow_checkbox.isChecked()

    # -- columns (see HeaderColumns) --

    def column_menu(self, clicked: int = -1):  # noqa: ANN201 - QMenu
        return self._columns.menu(clicked)

    def shown_columns(self) -> list[str]:
        return self._columns.shown()

    def set_column_shown(self, logical: int, shown: bool) -> None:
        self._columns.set_shown(logical, shown)

    def move_column(self, logical: int, step: int) -> None:
        self._columns.move(logical, step)

    def header_state(self) -> QByteArray:
        """The columns' order, widths and which show (saved with the window layout)."""
        return self._tree.header().saveState()

    def restore_header_state(self, state: QByteArray) -> bool:
        header = self._tree.header()
        if not header.restoreState(state) or header.count() != len(COLUMNS):
            self._columns.apply_default_order()
            return False
        return True

    def _rebuild(self) -> None:
        self._tree.clear()
        for item in self._items:
            row = QTreeWidgetItem(
                [
                    item.name,
                    "",
                    _format_duration(item.duration_s),
                    str(self._bookmark_counts.get(item.vlc_id, 0)),
                    "",
                ]
            )
            row.setData(0, 32, item.vlc_id)  # Qt.ItemDataRole.UserRole == 32
            bookmark_state = (
                self._bookmark_song[1] if self._bookmark_song and self._bookmark_song[0] == item.vlc_id else None
            )
            _set_row_playing(row, item.vlc_id == self._current_playing_id, self._is_actively_playing, bookmark_state)
            self._tree.addTopLevelItem(row)
        # Fit the columns to their contents on each rebuild (still resizable by hand).
        for column in range(len(COLUMNS)):
            self._tree.resizeColumnToContents(column)
        self._apply_filter(self._filter_edit.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in top_level_rows(self._tree):
            visible = not needle or needle in row.text(0).lower()  # column 0 == Title
            row.setHidden(not visible)

    def _on_selection_changed(self) -> None:
        selected = self._tree.selectedItems()
        if selected:
            self.item_selected.emit(selected[0].data(0, 32))

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self.item_double_clicked.emit(item.data(0, 32))


def _set_row_playing(row: QTreeWidgetItem, is_current: bool, is_playing: bool,
                     bookmark_state: str | None = None) -> None:
    if bookmark_state in _BOOKMARK_SONG_COLORS:
        brush = QBrush(_BOOKMARK_SONG_COLORS[bookmark_state])
    elif is_current:
        brush = QBrush(_ACTIVELY_PLAYING_COLOR if is_playing else _CURRENT_NOT_PLAYING_COLOR)
    else:
        brush = _NOT_CURRENT_BRUSH
    for column in range(len(COLUMNS)):
        row.setBackground(column, brush)


def _format_duration(duration_s: float | None) -> str:
    if duration_s is None or duration_s <= 0:  # VLC reports -1 for "not parsed yet"
        return ""
    total = int(duration_s)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
