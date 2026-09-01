"""VLC playlist sidebar: filter, bookmark-count column, Follow VLC mode (spec #145-#148)."""
from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from bookmark_studio.playback.status import VlcPlaylistItem

COLUMNS = ["Playing", "Title", "Artist", "Duration", "Bookmarks", "Status"]


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
        self._items = items
        self._bookmark_counts = bookmark_counts or {}
        self._rebuild()

    def set_current_playing(self, vlc_id: int | None) -> None:
        self._current_playing_id = vlc_id
        self._rebuild()

    def follow_vlc_enabled(self) -> bool:
        return self._follow_checkbox.isChecked()

    def _rebuild(self) -> None:
        self._tree.clear()
        for item in self._items:
            row = QTreeWidgetItem(
                [
                    "▶" if item.vlc_id == self._current_playing_id else "",
                    item.name,
                    "",
                    _format_duration(item.duration_s),
                    str(self._bookmark_counts.get(item.vlc_id, 0)),
                    "",
                ]
            )
            row.setData(0, 32, item.vlc_id)  # Qt.UserRole == 32
            self._tree.addTopLevelItem(row)
        self._apply_filter(self._filter_edit.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._tree.topLevelItemCount()):
            row = self._tree.topLevelItem(i)
            visible = not needle or needle in row.text(1).lower()
            row.setHidden(not visible)

    def _on_selection_changed(self) -> None:
        selected = self._tree.selectedItems()
        if selected:
            self.item_selected.emit(selected[0].data(0, 32))

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self.item_double_clicked.emit(item.data(0, 32))


def _format_duration(duration_s: float | None) -> str:
    if duration_s is None:
        return ""
    total = int(duration_s)
    minutes, seconds = divmod(total, 60)
    return f"{minutes}:{seconds:02d}"
