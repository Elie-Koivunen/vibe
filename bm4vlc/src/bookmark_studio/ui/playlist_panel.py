"""VLC playlist sidebar: filter, bookmark-count column, Follow VLC mode (spec #145-#148)."""
from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from bookmark_studio.playback.status import VlcPlaylistItem

COLUMNS = ["Title", "Artist", "Duration", "Bookmarks", "Status"]

# Direct user request: "instead of having a playing column, reflect in traffic
# color by highlighting the song that is being played e.g. in green" -- replaces
# the old "▶" marker column with a full-row background tint instead.
_PLAYING_ROW_COLOR = QColor("#8fd98f")
_NOT_PLAYING_BRUSH = QBrush()


class PlaylistPanel(QWidget):
    item_selected = Signal(int)  # vlc_id
    item_double_clicked = Signal(int)  # vlc_id -- play in VLC (spec #147)
    follow_vlc_toggled = Signal(bool)
    launch_vlc_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[VlcPlaylistItem] = []
        self._bookmark_counts: dict[int, int] = {}
        self._current_playing_id: int | None = None

        layout = QVBoxLayout(self)
        # Direct user request: "beautify the layout".
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Direct user request: "add button to launch vlc and a browse button to
        # select desired playlist" / "option to select an open vlc instance, a drop
        # box ... alternatively the user would launch a new instance with a browse
        # button to find the playlist and launch" -- one button opens VlcLaunchDialog
        # (app/application.py's prompt_vlc_launch_dialog), which contains both that
        # dropdown of open instances and the browse-for-playlist flow.
        self._launch_vlc_button = QPushButton("Launch VLC...", self)
        self._launch_vlc_button.setToolTip(
            "Attach to an already-open VLC instance, or launch a new one with a playlist"
        )
        self._launch_vlc_button.clicked.connect(self.launch_vlc_requested.emit)
        layout.addWidget(self._launch_vlc_button)

        # Direct follow-up requests: "move the connection status to above the launch
        # vlc button and enlarge the text", then "move the button above and make the
        # connected/disconnected text a bit smaller" -- previously a small label
        # buried in the transport bar, far from the button that actually establishes
        # the connection it's reporting on; now sits right under that button instead.
        self._connection_label = QLabel("● Offline", self)
        connection_font = QFont()
        connection_font.setPointSize(10)
        connection_font.setBold(True)
        self._connection_label.setFont(connection_font)
        self._connection_label.setStyleSheet("color: #a33;")
        layout.addWidget(self._connection_label)

        self._filter_edit = QLineEdit(self)
        self._filter_edit.setPlaceholderText("Filter...")
        self._filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter_edit)

        self._follow_checkbox = QCheckBox("Follow currently playing VLC song", self)
        self._follow_checkbox.setChecked(True)
        self._follow_checkbox.toggled.connect(self.follow_vlc_toggled.emit)
        layout.addWidget(self._follow_checkbox)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(len(COLUMNS))
        self._tree.setHeaderLabels(COLUMNS)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self._tree)

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

    def set_current_playing(self, vlc_id: int | None) -> None:
        """Updates just the playing-row highlight on existing rows -- direct fix for
        "i still cant automatically play a song by double clicking the tittle": this
        used to call _rebuild(), which does _tree.clear() + recreates every
        QTreeWidgetItem from scratch. Called on every ~400ms status poll, that
        destroyed and rebuilt the whole row list mid-gesture far more often than not
        during a real double-click (whose two clicks need to land on the SAME item
        object within Qt's double-click interval), silently breaking double-click
        detection -- confirmed by the fact goto_item()'s VLC command itself was
        already verified correct. It also wiped the current selection highlight and
        scroll position on every tick, a real bug in its own right even ignoring
        double-click. No rebuild is needed at all: only the highlighted row changes.
        """
        if vlc_id == self._current_playing_id:
            return
        self._current_playing_id = vlc_id
        for i in range(self._tree.topLevelItemCount()):
            row = self._tree.topLevelItem(i)
            _set_row_playing(row, row.data(0, 32) == vlc_id)

    def select_item(self, vlc_id: int | None) -> None:
        """Highlights the row for `vlc_id` -- direct user request: selecting a
        bookmark should "automatically select the song from the playlist above" too,
        not just switch the waveform. Setting the current item fires the normal
        itemSelectionChanged -> item_selected signal chain, so Application's existing
        _on_playlist_item_selected handles the actual waveform/follow-state switch;
        this method only needs to move the highlight.
        """
        if vlc_id is None:
            self._tree.clearSelection()
            return
        for i in range(self._tree.topLevelItemCount()):
            row = self._tree.topLevelItem(i)
            if row.data(0, 32) == vlc_id:
                self._tree.setCurrentItem(row)
                return

    def set_connected(self, connected: bool) -> None:
        if connected:
            self._connection_label.setText("● Connected")
            self._connection_label.setStyleSheet("color: #2a2;")
        else:
            self._connection_label.setText("● Offline")
            self._connection_label.setStyleSheet("color: #a33;")

    def set_follow_vlc(self, enabled: bool) -> None:
        """Programmatic version of the checkbox -- used when previewing a different,
        not-currently-playing song single-clicks the checkbox off (see
        Application._on_playlist_item_selected) so live playback progression doesn't
        yank the waveform view away from what the user just chose to look at.
        """
        self._follow_checkbox.setChecked(enabled)

    def follow_vlc_enabled(self) -> bool:
        return self._follow_checkbox.isChecked()

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
            row.setData(0, 32, item.vlc_id)  # Qt.UserRole == 32
            _set_row_playing(row, item.vlc_id == self._current_playing_id)
            self._tree.addTopLevelItem(row)
        # Direct user request: "have the columns resize automatically to the length
        # of the strings" -- stays manually resizable after this, just re-fit to the
        # current content on every rebuild instead of defaulting to truncated text.
        for column in range(len(COLUMNS)):
            self._tree.resizeColumnToContents(column)
        self._apply_filter(self._filter_edit.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._tree.topLevelItemCount()):
            row = self._tree.topLevelItem(i)
            visible = not needle or needle in row.text(0).lower()  # column 0 == Title
            row.setHidden(not visible)

    def _on_selection_changed(self) -> None:
        selected = self._tree.selectedItems()
        if selected:
            self.item_selected.emit(selected[0].data(0, 32))

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self.item_double_clicked.emit(item.data(0, 32))


def _set_row_playing(row: QTreeWidgetItem, playing: bool) -> None:
    brush = QBrush(_PLAYING_ROW_COLOR) if playing else _NOT_PLAYING_BRUSH
    for column in range(len(COLUMNS)):
        row.setBackground(column, brush)


def _format_duration(duration_s: float | None) -> str:
    if duration_s is None:
        return ""
    total = int(duration_s)
    minutes, seconds = divmod(total, 60)
    return f"{minutes}:{seconds:02d}"
