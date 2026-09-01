"""MainWindow: menu bar, QSplitter panel layout, active-context breadcrumb (spec #7-#8)."""
from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QUndoStack
from PySide6.QtWidgets import QLabel, QMainWindow, QSplitter, QVBoxLayout, QWidget

from bookmark_studio.app.commands import (
    ChangeLoopCommand,
    CreateBookmarkCommand,
    MoveBookmarkCommand,
    RenameBookmarkCommand,
    ResizeBookmarkCommand,
)
from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.ui.bookmark_panel import BookmarkPanel
from bookmark_studio.ui.inspector import BookmarkInspector
from bookmark_studio.ui.playlist_panel import PlaylistPanel
from bookmark_studio.ui.transport import TransportBar
from bookmark_studio.ui.waveform.scene import WaveformScene
from bookmark_studio.ui.waveform.view import WaveformView


class MainWindow(QMainWindow):
    def __init__(
        self,
        bookmark_repository: BookmarkRepository,
        undo_stack: QUndoStack | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("VLC Bookmark Studio")
        self.setMinimumSize(900, 600)  # spec #132

        self._bookmark_repository = bookmark_repository
        self._undo_stack = undo_stack or QUndoStack(self)
        self._current_playlist_id: UUID | None = None
        self._current_media_id: UUID | None = None

        self._breadcrumb = QLabel("No playlist › No track › 0 bookmarks", self)

        self._playlist_panel = PlaylistPanel(self)
        self._waveform_scene = WaveformScene()
        self._waveform_view = WaveformView(self._waveform_scene)
        self._bookmark_panel = BookmarkPanel(self)
        self._inspector = BookmarkInspector(self)
        self._transport = TransportBar(self)

        self._build_menu_bar()
        self._build_layout()
        self._wire_signals()

    # -- layout --

    def _build_layout(self) -> None:
        top_splitter = QSplitter(Qt.Horizontal, self)
        top_splitter.addWidget(self._playlist_panel)
        top_splitter.addWidget(self._waveform_view)
        top_splitter.setStretchFactor(1, 1)

        bottom_splitter = QSplitter(Qt.Horizontal, self)
        bottom_splitter.addWidget(self._bookmark_panel)
        bottom_splitter.addWidget(self._inspector)

        main_splitter = QSplitter(Qt.Vertical, self)
        main_splitter.addWidget(top_splitter)
        main_splitter.addWidget(bottom_splitter)
        main_splitter.setStretchFactor(0, 2)
        main_splitter.setStretchFactor(1, 1)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addWidget(self._breadcrumb)
        layout.addWidget(main_splitter)
        layout.addWidget(self._transport)
        self.setCentralWidget(central)

    def _build_menu_bar(self) -> None:
        menu_bar = self.menuBar()
        for title in ("File", "Edit", "View", "Bookmark", "Playback", "Playlist", "Tools", "Help"):
            menu_bar.addMenu(title)

        # Wire Undo/Redo into the "Edit" menu added above (File=0, Edit=1).
        edit_menu = menu_bar.actions()[1].menu()
        undo_action = self._undo_stack.createUndoAction(self, "Undo")
        undo_action.setShortcut("Ctrl+Z")
        redo_action = self._undo_stack.createRedoAction(self, "Redo")
        redo_action.setShortcut("Ctrl+Y")
        edit_menu.addAction(undo_action)
        edit_menu.addAction(redo_action)

    # -- wiring --

    def _wire_signals(self) -> None:
        self._waveform_scene.seek_requested.connect(self._on_seek_requested)
        self._waveform_scene.point_bookmark_requested.connect(self._on_point_bookmark_requested)
        self._waveform_scene.bookmark_activated.connect(self._on_bookmark_activated)
        self._waveform_scene.bookmark_move_finished.connect(self._on_bookmark_move_finished)
        self._waveform_scene.bookmark_resize_finished.connect(self._on_bookmark_resize_finished)

        self._bookmark_panel.bookmark_selected.connect(self._on_bookmark_activated)

        self._inspector.name_committed.connect(self._on_name_committed)
        self._inspector.loop_settings_committed.connect(self._on_loop_settings_committed)

    # -- context --

    def set_context(self, *, playlist_name: str, track_name: str, playlist_id: UUID | None,
                     media_id: UUID | None, bookmark_count: int, duration_us: int = 0) -> None:
        self._current_playlist_id = playlist_id
        self._current_media_id = media_id
        self._breadcrumb.setText(f"{playlist_name} › {track_name} › {bookmark_count} bookmarks")
        self._waveform_scene.set_duration_us(duration_us)

    def load_bookmarks(self, bookmarks: list[Bookmark]) -> None:
        self._waveform_scene.set_bookmarks(bookmarks)
        self._bookmark_panel.set_bookmarks(bookmarks)

    # -- handlers --

    def _on_seek_requested(self, time_us: int) -> None:
        self._waveform_scene.set_playhead_time_us(time_us)

    def _on_point_bookmark_requested(self, time_us: int) -> None:
        if self._current_media_id is None:
            return
        from uuid import uuid4

        bookmark = Bookmark(
            id=uuid4(),
            playlist_id=self._current_playlist_id,
            media_id=self._current_media_id,
            scope=BookmarkScope.PLAYLIST_MEDIA if self._current_playlist_id else BookmarkScope.GLOBAL_MEDIA,
            lane_id=None,
            bookmark_type=BookmarkType.POINT,
            name="New bookmark",
            start_us=time_us,
            end_us=None,
            loop_enabled=False,
            repeat_count=None,
            loop_gap_ms=0,
            completion_action=CompletionAction.CONTINUE,
        )
        self._undo_stack.push(CreateBookmarkCommand(self._bookmark_repository, bookmark))
        self._refresh_bookmarks()

    def _on_bookmark_activated(self, bookmark_id: UUID) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is not None:
            self._inspector.load_bookmark(bookmark)
            self._bookmark_panel.select_bookmark(bookmark_id)

    def _on_bookmark_move_finished(self, bookmark_id: UUID, start_us: int, end_us: int) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        self._undo_stack.push(
            MoveBookmarkCommand(self._bookmark_repository, bookmark_id, bookmark.start_us, bookmark.end_us, start_us, end_us)
        )
        self._refresh_bookmarks()

    def _on_bookmark_resize_finished(self, bookmark_id: UUID, handle: str, value_us: int) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        old_value = bookmark.start_us if handle == "start" else bookmark.end_us
        self._undo_stack.push(
            ResizeBookmarkCommand(self._bookmark_repository, bookmark_id, handle, old_value, value_us)
        )
        self._refresh_bookmarks()

    def _on_name_committed(self, new_name: str) -> None:
        bookmark = self._current_inspected_bookmark()
        if bookmark is None:
            return
        self._undo_stack.push(RenameBookmarkCommand(self._bookmark_repository, bookmark.id, bookmark.name, new_name))
        self._refresh_bookmarks()

    def _on_loop_settings_committed(
        self, enabled: bool, repeat_count: int | None, gap_ms: int, action: CompletionAction
    ) -> None:
        bookmark = self._current_inspected_bookmark()
        if bookmark is None:
            return
        self._undo_stack.push(
            ChangeLoopCommand(
                self._bookmark_repository, bookmark.id,
                old=(bookmark.loop_enabled, bookmark.repeat_count, bookmark.loop_gap_ms, bookmark.completion_action),
                new=(enabled, repeat_count, gap_ms, action),
            )
        )
        self._refresh_bookmarks()

    def _current_inspected_bookmark(self) -> Bookmark | None:
        return self._inspector.current_bookmark()

    def _refresh_bookmarks(self) -> None:
        if self._current_playlist_id is None and self._current_media_id is None:
            return
        if self._current_media_id is None:
            return
        bookmarks = self._bookmark_repository.list_for_playlist_media(
            self._current_playlist_id, self._current_media_id
        ) if self._current_playlist_id else self._bookmark_repository.list_global_for_media(self._current_media_id)
        self.load_bookmarks(bookmarks)
