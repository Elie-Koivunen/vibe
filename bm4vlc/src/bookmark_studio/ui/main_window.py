"""MainWindow: menu bar, QSplitter panel layout, active-context breadcrumb (spec #7-#8)."""
from __future__ import annotations

from uuid import UUID, uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QUndoStack
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from bookmark_studio.app.commands import (
    ChangeLoopCommand,
    CreateBookmarkCommand,
    DeleteBookmarkCommand,
    MoveBookmarkCommand,
    RenameBookmarkCommand,
    ResizeBookmarkCommand,
)
from bookmark_studio.domain.bookmark import Bookmark, default_bookmark_name
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.ui.bookmark_panel import BookmarkPanel
from bookmark_studio.ui.inspector import BookmarkInspector
from bookmark_studio.ui.playlist_panel import PlaylistPanel
from bookmark_studio.ui.transport import TransportBar
from bookmark_studio.ui.waveform.scene import WaveformScene
from bookmark_studio.ui.waveform.view import WaveformView


class MainWindow(QMainWindow):
    # Re-exposes transport/waveform playback intents at the MainWindow level so a
    # composition root (e.g. app/application.py) has one place to wire real VLC
    # commands, instead of reaching into private widgets. play_selection_requested and
    # loop_selection_requested carry (start_us, end_us).
    play_selection_requested = Signal(int, int)
    loop_selection_requested = Signal(int, int)
    launch_vlc_requested = Signal()
    play_bookmark_requested = Signal(object)  # UUID
    loop_bookmark_requested = Signal(object)  # UUID

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

        self._build_selection_bar()
        self._build_menu_bar()
        self._build_layout()
        self._build_shortcuts()
        self._wire_signals()

    # -- layout --

    def _build_selection_bar(self) -> None:
        """Visible actions for a painted selection (spec #37: 'floating actions appear:
        Bookmark / Play / Loop / Clear'). Not a floating popup -- a persistent, always
        visible row that enables when a selection exists -- simpler and, per direct
        user feedback ('no buttons to save highlighted areas'), the actual bug was that
        no such control existed at all, floating or otherwise.
        """
        self._selection_bar = QWidget(self)
        layout = QHBoxLayout(self._selection_bar)
        layout.setContentsMargins(4, 2, 4, 2)

        self._selection_label = QLabel("No selection", self)
        layout.addWidget(self._selection_label)

        # Direct user request: "add additional fields to display start/end time based
        # on how the bookmark is highlighted" -- editable, so a drag-selection can be
        # fine-tuned numerically before turning it into a bookmark, not just read.
        self._selection_start_edit = QLineEdit(self)
        self._selection_start_edit.setPlaceholderText("Start")
        self._selection_start_edit.setMaximumWidth(110)
        self._selection_start_edit.setEnabled(False)
        self._selection_start_edit.editingFinished.connect(self._on_selection_start_edited)
        layout.addWidget(self._selection_start_edit)

        layout.addWidget(QLabel("→", self))

        self._selection_end_edit = QLineEdit(self)
        self._selection_end_edit.setPlaceholderText("End")
        self._selection_end_edit.setMaximumWidth(110)
        self._selection_end_edit.setEnabled(False)
        self._selection_end_edit.editingFinished.connect(self._on_selection_end_edited)
        layout.addWidget(self._selection_end_edit)

        layout.addStretch(1)

        # Explicit, always-visible zoom controls (spec #84's Ctrl+wheel/Ctrl+0 still
        # work) -- added directly next to the waveform because "unable to zoom
        # in/out" was reported live: a hidden modifier-key gesture with no on-screen
        # affordance at all reads as a broken feature, not an undiscovered one.
        zoom_out_button = QPushButton("Zoom −", self)
        zoom_out_button.setToolTip("Zoom out (Ctrl+-, or scroll wheel down)")
        zoom_out_button.clicked.connect(lambda: self._waveform_view.zoom(0.8))
        layout.addWidget(zoom_out_button)

        zoom_in_button = QPushButton("Zoom +", self)
        zoom_in_button.setToolTip("Zoom in (Ctrl++, or scroll wheel up)")
        zoom_in_button.clicked.connect(lambda: self._waveform_view.zoom(1.25))
        layout.addWidget(zoom_in_button)

        zoom_fit_button = QPushButton("Fit", self)
        zoom_fit_button.setToolTip("Fit entire track to the window (Ctrl+0)")
        zoom_fit_button.clicked.connect(self._waveform_view.fit_entire_media)
        layout.addWidget(zoom_fit_button)

        # Direct user request: "a button to explicitly bookmark". Always enabled --
        # unlike "Bookmark Selection" below, this needs no drag-selection first (which
        # was itself broken by the handle_empty_drag boundary bug -- see scene.py), so
        # it's the one guaranteed-simple way to drop a bookmark. Context-aware: reported
        # live as "the bookmarking seems to have changed" after a user had a region
        # highlighted, clicked this, and got a point bookmark at the playhead instead
        # of a segment from their selection -- always ignoring an active selection was
        # the confusing part, not a regression. Now it behaves like Bookmark Selection
        # whenever one exists, and only falls back to a playhead point when it doesn't.
        self._bookmark_now_button = QPushButton("Bookmark Now", self)
        self._bookmark_now_button.setToolTip(
            "Bookmark the current selection, or the playhead position if nothing is selected"
        )
        self._bookmark_now_button.clicked.connect(self._on_bookmark_now_clicked)
        layout.addWidget(self._bookmark_now_button)

        self._bookmark_selection_button = QPushButton("Bookmark Selection (Ctrl+B)", self)
        self._bookmark_selection_button.clicked.connect(self._on_bookmark_selection_clicked)
        layout.addWidget(self._bookmark_selection_button)

        self._play_selection_button = QPushButton("Play Selection", self)
        self._play_selection_button.clicked.connect(self._on_play_selection_clicked)
        layout.addWidget(self._play_selection_button)

        self._loop_selection_button = QPushButton("Loop Selection", self)
        self._loop_selection_button.clicked.connect(self._on_loop_selection_clicked)
        layout.addWidget(self._loop_selection_button)

        self._clear_selection_button = QPushButton("Clear", self)
        self._clear_selection_button.clicked.connect(lambda: self._waveform_scene.clear_selection())
        layout.addWidget(self._clear_selection_button)

        self._set_selection_buttons_enabled(False)

    def _set_selection_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self._bookmark_selection_button, self._play_selection_button,
            self._loop_selection_button, self._clear_selection_button,
        ):
            button.setEnabled(enabled)

    def _build_layout(self) -> None:
        top_splitter = QSplitter(Qt.Horizontal, self)
        top_splitter.addWidget(self._playlist_panel)
        top_splitter.addWidget(self._waveform_view)
        top_splitter.setStretchFactor(1, 1)

        bottom_splitter = QSplitter(Qt.Horizontal, self)
        bottom_splitter.addWidget(self._bookmark_panel)
        bottom_splitter.addWidget(self._inspector)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addWidget(self._breadcrumb)
        layout.addWidget(self._selection_bar)
        layout.addWidget(top_splitter, 2)
        # Direct user request: "i want the playback buttons to be above, not under" --
        # previously sat at the very bottom of the window, past the bookmark list and
        # inspector; moved directly under the waveform it controls instead.
        layout.addWidget(self._transport)
        layout.addWidget(bottom_splitter, 1)
        self.setCentralWidget(central)

    def _build_menu_bar(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("File")
        launch_vlc_action = file_menu.addAction("Launch VLC...")
        launch_vlc_action.triggered.connect(self.launch_vlc_requested.emit)
        file_menu.addSeparator()
        export_action = file_menu.addAction("Export Project...")
        export_action.triggered.connect(self._on_export_project)
        import_action = file_menu.addAction("Import Project...")
        import_action.triggered.connect(self._on_import_project)
        file_menu.addSeparator()
        exit_action = file_menu.addAction("Exit")
        exit_action.triggered.connect(self.close)

        edit_menu = menu_bar.addMenu("Edit")
        undo_action = self._undo_stack.createUndoAction(self, "Undo")
        undo_action.setShortcut("Ctrl+Z")
        redo_action = self._undo_stack.createRedoAction(self, "Redo")
        redo_action.setShortcut("Ctrl+Y")
        edit_menu.addAction(undo_action)
        edit_menu.addAction(redo_action)

        view_menu = menu_bar.addMenu("View")
        zoom_in_action = view_menu.addAction("Zoom In")
        zoom_in_action.setShortcut("Ctrl++")
        zoom_in_action.triggered.connect(lambda: self._waveform_view.zoom(1.25))
        zoom_out_action = view_menu.addAction("Zoom Out")
        zoom_out_action.setShortcut("Ctrl+-")
        zoom_out_action.triggered.connect(lambda: self._waveform_view.zoom(0.8))
        fit_action = view_menu.addAction("Fit Entire Media")
        fit_action.setShortcut("Ctrl+0")
        fit_action.triggered.connect(self._waveform_view.fit_entire_media)

        bookmark_menu = menu_bar.addMenu("Bookmark")
        bookmark_selection_action = bookmark_menu.addAction("Bookmark Selection")
        bookmark_selection_action.setShortcut("Ctrl+B")
        bookmark_selection_action.triggered.connect(self._on_bookmark_selection_clicked)
        point_action = bookmark_menu.addAction("Point Bookmark at Playhead")
        point_action.setShortcut("Ctrl+Shift+B")
        point_action.triggered.connect(lambda: self._on_point_bookmark_requested(self._playhead_time_us()))
        rename_action = bookmark_menu.addAction("Rename Selected Bookmark")
        rename_action.setShortcut("F2")
        rename_action.triggered.connect(self._on_rename_shortcut)
        delete_action = bookmark_menu.addAction("Delete Selected Bookmark")
        delete_action.setShortcut("Delete")
        delete_action.triggered.connect(self._on_delete_shortcut)

        playback_menu = menu_bar.addMenu("Playback")
        play_pause_action = playback_menu.addAction("Play/Pause")
        play_pause_action.setShortcut("Space")
        play_pause_action.triggered.connect(self._transport.play_pause_clicked.emit)
        stop_action = playback_menu.addAction("Stop")
        stop_action.triggered.connect(self._transport.stop_clicked.emit)
        seek_back_action = playback_menu.addAction("Seek -5s")
        seek_back_action.setShortcut("Left")
        seek_back_action.triggered.connect(self._transport.seek_back_clicked.emit)
        seek_forward_action = playback_menu.addAction("Seek +5s")
        seek_forward_action.setShortcut("Right")
        seek_forward_action.triggered.connect(self._transport.seek_forward_clicked.emit)

        playlist_menu = menu_bar.addMenu("Playlist")
        refresh_action = playlist_menu.addAction("Refresh")
        refresh_action.setShortcut("F5")
        # Application (if wired) connects to refresh_requested; harmless no-op otherwise.
        self.playlist_refresh_requested = refresh_action.triggered

        tools_menu = menu_bar.addMenu("Tools")
        diagnostics_action = tools_menu.addAction("Diagnostics...")
        diagnostics_action.triggered.connect(self._on_show_diagnostics)

        help_menu = menu_bar.addMenu("Help")
        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self._on_show_about)

    def _build_shortcuts(self) -> None:
        QShortcut(QKeySequence("["), self, activated=self._on_mark_selection_start)
        QShortcut(QKeySequence("]"), self, activated=self._on_mark_selection_end)

    # -- wiring --

    def _wire_signals(self) -> None:
        self._waveform_scene.seek_requested.connect(self._on_seek_requested)
        self._waveform_scene.point_bookmark_requested.connect(self._on_point_bookmark_requested)
        self._waveform_scene.bookmark_activated.connect(self._on_bookmark_activated)
        self._waveform_scene.bookmark_move_finished.connect(self._on_bookmark_move_finished)
        self._waveform_scene.bookmark_resize_finished.connect(self._on_bookmark_resize_finished)
        self._waveform_scene.selection_changed.connect(self._on_selection_changed)

        self._bookmark_panel.bookmark_selected.connect(self._on_bookmark_activated)
        self._bookmark_panel.export_requested.connect(self._on_export_project)
        self._bookmark_panel.play_bookmark_requested.connect(self.play_bookmark_requested.emit)
        self._bookmark_panel.loop_bookmark_requested.connect(self.loop_bookmark_requested.emit)

        self._inspector.name_committed.connect(self._on_name_committed)
        self._inspector.loop_settings_committed.connect(self._on_loop_settings_committed)

        self._playlist_panel.launch_vlc_requested.connect(self.launch_vlc_requested.emit)

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

    def set_playhead_time_us(self, time_us: int) -> None:
        self._waveform_scene.set_playhead_time_us(time_us)

    # -- handlers: selection bar --

    def _on_selection_changed(self, selection: object) -> None:
        from bookmark_studio.domain.selection import Selection
        from bookmark_studio.ui.transport import format_timecode

        if isinstance(selection, Selection):
            # Start/end now live in the dedicated fields next to this label (below) --
            # repeating them here as well was redundant and crowded the toolbar row.
            self._selection_label.setText(f"Selection ({format_timecode(selection.duration_us)})")
            # Only resync from the model when the field doesn't already show this
            # value -- editingFinished() already applied the typed value locally, and
            # overwriting mid-edit-cycle would just echo back exactly what the user
            # typed, but doing it unconditionally would also clobber the cursor/
            # selection state in the field for no reason.
            start_text = format_timecode(selection.start_us)
            if self._selection_start_edit.text() != start_text:
                self._selection_start_edit.setText(start_text)
            end_text = format_timecode(selection.end_us)
            if self._selection_end_edit.text() != end_text:
                self._selection_end_edit.setText(end_text)
            self._selection_start_edit.setEnabled(True)
            self._selection_end_edit.setEnabled(True)
            self._set_selection_buttons_enabled(True)
        else:
            self._selection_label.setText("No selection")
            self._selection_start_edit.clear()
            self._selection_end_edit.clear()
            self._selection_start_edit.setEnabled(False)
            self._selection_end_edit.setEnabled(False)
            self._set_selection_buttons_enabled(False)

    def _on_selection_start_edited(self) -> None:
        from bookmark_studio.domain.selection import Selection
        from bookmark_studio.ui.transport import format_timecode, parse_timecode

        selection = self._waveform_scene.selection()
        if selection is None:
            return
        try:
            new_start_us = parse_timecode(self._selection_start_edit.text())
        except ValueError:
            self._selection_start_edit.setText(format_timecode(selection.start_us))
            return
        if new_start_us < 0 or new_start_us >= selection.end_us:
            self._selection_start_edit.setText(format_timecode(selection.start_us))
            return
        self._waveform_scene.set_selection(Selection(start_us=new_start_us, end_us=selection.end_us))

    def _on_selection_end_edited(self) -> None:
        from bookmark_studio.domain.selection import Selection
        from bookmark_studio.ui.transport import format_timecode, parse_timecode

        selection = self._waveform_scene.selection()
        if selection is None:
            return
        try:
            new_end_us = parse_timecode(self._selection_end_edit.text())
        except ValueError:
            self._selection_end_edit.setText(format_timecode(selection.end_us))
            return
        if new_end_us <= selection.start_us:
            self._selection_end_edit.setText(format_timecode(selection.end_us))
            return
        self._waveform_scene.set_selection(Selection(start_us=selection.start_us, end_us=new_end_us))

    def _on_bookmark_now_clicked(self) -> None:
        if self._waveform_scene.selection() is not None:
            self._on_bookmark_selection_clicked()
        else:
            self._on_point_bookmark_requested(self._playhead_time_us())

    def _on_bookmark_selection_clicked(self) -> None:
        selection = self._waveform_scene.selection()
        if selection is None or self._current_media_id is None:
            return
        bookmark = Bookmark(
            id=uuid4(),
            playlist_id=self._current_playlist_id,
            media_id=self._current_media_id,
            scope=BookmarkScope.PLAYLIST_MEDIA if self._current_playlist_id else BookmarkScope.GLOBAL_MEDIA,
            lane_id=None,
            bookmark_type=BookmarkType.SEGMENT,
            name=default_bookmark_name(),
            start_us=selection.start_us,
            end_us=selection.end_us,
            loop_enabled=False,
            repeat_count=None,
            loop_gap_ms=0,
            completion_action=CompletionAction.CONTINUE,
        )
        self._create_bookmark_and_focus_name(bookmark)
        self._waveform_scene.clear_selection()

    def _on_play_selection_clicked(self) -> None:
        selection = self._waveform_scene.selection()
        if selection is not None:
            self.play_selection_requested.emit(selection.start_us, selection.end_us)

    def _on_loop_selection_clicked(self) -> None:
        selection = self._waveform_scene.selection()
        if selection is not None:
            self.loop_selection_requested.emit(selection.start_us, selection.end_us)

    def _on_mark_selection_start(self) -> None:
        from bookmark_studio.domain.selection import Selection

        time_us = self._playhead_time_us()
        current = self._waveform_scene.selection()
        end_us = current.end_us if current and current.end_us > time_us else time_us + 1
        self._waveform_scene.set_selection(Selection(start_us=time_us, end_us=end_us))

    def _on_mark_selection_end(self) -> None:
        from bookmark_studio.domain.selection import Selection

        time_us = self._playhead_time_us()
        current = self._waveform_scene.selection()
        start_us = current.start_us if current and current.start_us < time_us else max(0, time_us - 1)
        self._waveform_scene.set_selection(Selection(start_us=start_us, end_us=time_us))

    def _playhead_time_us(self) -> int:
        return self._waveform_scene.playhead_time_us()

    # -- handlers: bookmark lifecycle --

    def _on_point_bookmark_requested(self, time_us: int) -> None:
        if self._current_media_id is None:
            return
        bookmark = Bookmark(
            id=uuid4(),
            playlist_id=self._current_playlist_id,
            media_id=self._current_media_id,
            scope=BookmarkScope.PLAYLIST_MEDIA if self._current_playlist_id else BookmarkScope.GLOBAL_MEDIA,
            lane_id=None,
            bookmark_type=BookmarkType.POINT,
            name=default_bookmark_name(),
            start_us=time_us,
            end_us=None,
            loop_enabled=False,
            repeat_count=None,
            loop_gap_ms=0,
            completion_action=CompletionAction.CONTINUE,
        )
        self._create_bookmark_and_focus_name(bookmark)

    def _create_bookmark_and_focus_name(self, bookmark: Bookmark) -> None:
        self._undo_stack.push(CreateBookmarkCommand(self._bookmark_repository, bookmark))
        self._refresh_bookmarks()
        # spec #46: inline name editor appears immediately after creation, no modal.
        self._inspector.load_bookmark(bookmark)
        self._bookmark_panel.select_bookmark(bookmark.id)
        self._inspector._name_edit.setFocus()
        self._inspector._name_edit.selectAll()

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

    def _on_rename_shortcut(self) -> None:
        if self._current_inspected_bookmark() is not None:
            self._inspector._name_edit.setFocus()
            self._inspector._name_edit.selectAll()

    def _on_delete_shortcut(self) -> None:
        bookmark = self._current_inspected_bookmark()
        if bookmark is None:
            return
        self._undo_stack.push(DeleteBookmarkCommand(self._bookmark_repository, bookmark))
        self._inspector.clear()
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

    def _on_seek_requested(self, time_us: int) -> None:
        self._waveform_scene.set_playhead_time_us(time_us)

    # -- menu action bodies --

    def _on_export_project(self) -> None:
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        from bookmark_studio.persistence.lane_repository import LaneRepository
        from bookmark_studio.persistence.media_repository import MediaRepository
        from bookmark_studio.persistence.playlist_repository import PlaylistRepository
        from bookmark_studio.project.export_service import ProjectData, export_project

        path_str, _filter = QFileDialog.getSaveFileName(self, "Export Project", "", "Bookmark Studio Project (*.vlcbmk)")
        if not path_str:
            return
        conn = self._bookmark_repository.connection
        playlists = [r.playlist for r in PlaylistRepository(conn).list_recent(limit=10_000)]
        media = []
        bookmarks = []
        if self._current_media_id is not None:
            media_record = MediaRepository(conn).get(self._current_media_id)
            if media_record:
                media.append(media_record)
            bookmarks = self._bookmark_repository.list_for_playlist_media(
                self._current_playlist_id, self._current_media_id
            ) if self._current_playlist_id else self._bookmark_repository.list_global_for_media(self._current_media_id)
        lanes = LaneRepository(conn).list_for_playlist(self._current_playlist_id) if self._current_playlist_id else []
        export_project(Path(path_str), ProjectData(playlists=playlists, media=media, bookmarks=bookmarks, lanes=lanes))
        QMessageBox.information(self, "Export Project", f"Exported to {path_str}")

    def _on_import_project(self) -> None:
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        from bookmark_studio.project.import_service import import_project

        path_str, _filter = QFileDialog.getOpenFileName(self, "Import Project", "", "Bookmark Studio Project (*.vlcbmk)")
        if not path_str:
            return
        try:
            plan = import_project(self._bookmark_repository.connection, Path(path_str))
        except Exception as exc:  # noqa: BLE001 - shown to the user, not a crash
            QMessageBox.critical(self, "Import Project", f"Import failed: {exc}")
            return
        QMessageBox.information(self, "Import Project", f"Imported {len(plan.bookmarks)} bookmarks.")
        self._refresh_bookmarks()

    def _on_show_diagnostics(self) -> None:
        lines = [
            f"Current playlist: {self._current_playlist_id or '(none)'}",
            f"Current media: {self._current_media_id or '(none)'}",
            f"Bookmarks loaded: {self._bookmark_panel._tree.topLevelItemCount()}",
        ]
        QMessageBox.information(self, "Diagnostics", "\n".join(lines))

    def _on_show_about(self) -> None:
        QMessageBox.about(
            self, "About VLC Bookmark Studio",
            "VLC Bookmark Studio\n\nA playlist-aware visual bookmarking and looping "
            "tool for VLC Media Player.",
        )
