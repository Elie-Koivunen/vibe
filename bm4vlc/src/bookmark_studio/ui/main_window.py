"""MainWindow: menu bar, QSplitter panel layout, active-context breadcrumb (spec #7-#8)."""
from __future__ import annotations

from uuid import UUID, uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QUndoStack
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget,
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
    # commands, instead of reaching into private widgets. loop_selection_requested
    # carries (start_us, end_us). object, not int: int would marshal through a
    # 32-bit C++ int and silently wrap past ~35.8 minutes (2^31 microseconds) --
    # same bug class fixed for TransportBar/BookmarkInspector's timecode signals.
    # Direct follow-up request: "remove the play selection button as it is" -- the
    # separate non-looping Play Selection button/signal is gone; the one remaining
    # button (labeled "Play", per "rename the loop selection as 'play'") still loops
    # under the hood, same as every bookmark already defaults to loop-enabled.
    loop_selection_requested = Signal(object, object)
    launch_vlc_requested = Signal()
    play_bookmark_requested = Signal(object)  # UUID
    loop_bookmark_requested = Signal(object)  # UUID
    bookmark_reorder_requested = Signal(list)  # ordered list of bookmark UUIDs
    bookmark_song_display_requested = Signal(object)  # UUID -- just selected, not played
    bookmarks_changed = Signal()

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
        self._breadcrumb.setStyleSheet("padding: 4px 8px; font-weight: 600;")

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
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        self._selection_label = QLabel("No selection", self)
        layout.addWidget(self._selection_label)

        # The editable Start/End fields that used to sit here were removed per
        # direct follow-up request -- "these do not serve a purpose" -- once the
        # bookmark list's own Start/End columns and the Inspector's fields already
        # cover editing a saved bookmark's range; the pair here only ever edited an
        # in-progress drag-selection, which is more naturally adjusted by dragging
        # the selection itself on the waveform.
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

        # Direct follow-up request: "remove the play selection button as it is.
        # rename the loop selection as 'play'" -- consolidates the two into one
        # button; it still loops the selection under the hood (matching every
        # bookmark's own loop-enabled-by-default), just no longer needs a separate
        # non-looping "Play Selection" alongside it.
        self._loop_selection_button = QPushButton("Play", self)
        self._loop_selection_button.clicked.connect(self._on_loop_selection_clicked)
        layout.addWidget(self._loop_selection_button)

        self._clear_selection_button = QPushButton("Clear", self)
        self._clear_selection_button.clicked.connect(lambda: self._waveform_scene.clear_selection())
        layout.addWidget(self._clear_selection_button)

        self._set_selection_buttons_enabled(False)

    def _set_selection_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self._bookmark_selection_button, self._loop_selection_button, self._clear_selection_button,
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
        layout.setSpacing(6)
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
        self._bookmark_panel.delete_bookmark_requested.connect(self._on_delete_bookmark_requested)
        self._bookmark_panel.reorder_requested.connect(self.bookmark_reorder_requested.emit)
        self._bookmark_panel.loop_edited.connect(self._on_bookmark_panel_loop_edited)
        self._bookmark_panel.gap_edited.connect(self._on_bookmark_panel_gap_edited)
        self._bookmark_panel.fade_in_edited.connect(self._on_bookmark_panel_fade_in_edited)
        self._bookmark_panel.fade_out_edited.connect(self._on_bookmark_panel_fade_out_edited)

        self._inspector.name_committed.connect(self._on_name_committed)
        self._inspector.loop_settings_committed.connect(self._on_loop_settings_committed)
        self._inspector.start_committed.connect(self._on_inspector_start_committed)
        self._inspector.end_committed.connect(self._on_inspector_end_committed)

        self._playlist_panel.launch_vlc_requested.connect(self.launch_vlc_requested.emit)

    # -- context --

    def set_context(self, *, playlist_name: str, track_name: str, playlist_id: UUID | None,
                     media_id: UUID | None, bookmark_count: int, duration_us: int = 0) -> None:
        self._current_playlist_id = playlist_id
        self._current_media_id = media_id
        self._breadcrumb.setText(f"{playlist_name} › {track_name} › {bookmark_count} bookmarks")
        self._waveform_scene.set_duration_us(duration_us)

    def load_bookmarks(self, bookmarks: list[Bookmark]) -> None:
        """Waveform-scoped bookmarks for the one song currently displayed. The
        bookmark LIST panel is fed separately, from load_all_bookmarks -- direct user
        request: "the bookmarks should all be listed for all songs", not just this one.
        """
        self._waveform_scene.set_bookmarks(bookmarks)

    def load_all_bookmarks(self, bookmarks: list[Bookmark], song_names: dict[UUID, str]) -> None:
        self._bookmark_panel.set_bookmarks(bookmarks, song_names)

    def set_playhead_time_us(self, time_us: int) -> None:
        self._waveform_scene.set_playhead_time_us(time_us)

    def set_connected(self, connected: bool) -> None:
        """Drives both the connection indicator (PlaylistPanel, above Launch VLC --
        direct follow-up request to move it there) and the transport buttons'
        enabled state (TransportBar) together, so callers have one place to report
        connection changes instead of reaching into two widgets.
        """
        self._transport.set_transport_enabled(connected)
        self._playlist_panel.set_connected(connected)

    # -- handlers: selection bar --

    def _on_selection_changed(self, selection: object) -> None:
        from bookmark_studio.domain.selection import Selection
        from bookmark_studio.ui.transport import format_timecode

        if isinstance(selection, Selection):
            self._selection_label.setText(f"Selection ({format_timecode(selection.duration_us)})")
            self._set_selection_buttons_enabled(True)
        else:
            self._selection_label.setText("No selection")
            self._set_selection_buttons_enabled(False)

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
            # Direct user request: "per default, bookmark loop should be enabled
            # (infinite)". repeat_count=None already means "forever" throughout this
            # codebase (see e.g. inspector.py's Repeat spinbox special value).
            loop_enabled=True,
            repeat_count=None,
            loop_gap_ms=0,
            completion_action=CompletionAction.CONTINUE,
        )
        self._create_bookmark_and_focus_name(bookmark)
        self._waveform_scene.clear_selection()

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
        self._load_bookmark_into_inspector(bookmark)
        self._bookmark_panel.select_bookmark(bookmark.id)
        self._inspector._name_edit.setFocus()
        self._inspector._name_edit.selectAll()

    def _on_bookmark_activated(self, bookmark_id: UUID) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is not None:
            self._load_bookmark_into_inspector(bookmark)
            self._bookmark_panel.select_bookmark(bookmark_id)
            # Direct user request: "when i select a bookmarking, i want it to
            # automatically select the song from the playlist above and display its
            # waveform along with the bookmarks" -- Application resolves which live
            # playlist row/song this bookmark belongs to (this window has no playlist
            # snapshot of its own to do that lookup with).
            self.bookmark_song_display_requested.emit(bookmark_id)

    def _on_bookmark_move_finished(self, bookmark_id: UUID, start_us: int, end_us: int) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        self._undo_stack.push(
            MoveBookmarkCommand(self._bookmark_repository, bookmark_id, bookmark.start_us, bookmark.end_us, start_us, end_us)
        )
        self._refresh_bookmarks()
        self._refresh_inspector_if_current(bookmark_id)

    def _on_bookmark_resize_finished(self, bookmark_id: UUID, handle: str, value_us: int) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        old_value = bookmark.start_us if handle == "start" else bookmark.end_us
        self._undo_stack.push(
            ResizeBookmarkCommand(self._bookmark_repository, bookmark_id, handle, old_value, value_us)
        )
        self._refresh_bookmarks()
        self._refresh_inspector_if_current(bookmark_id)

    def _refresh_inspector_if_current(self, bookmark_id: UUID) -> None:
        """Direct user request: "if the bookmark is adjusted, it should automatically
        update the bookmark values" -- dragging/resizing a bookmark region on the
        waveform already persisted correctly, but the Inspector's Start/End fields
        (if that same bookmark happened to be loaded there) stayed stuck showing the
        pre-drag values until the user clicked away and back.
        """
        current = self._current_inspected_bookmark()
        if current is None or current.id != bookmark_id:
            return
        updated = self._bookmark_repository.get(bookmark_id)
        if updated is not None:
            self._load_bookmark_into_inspector(updated)

    def _on_name_committed(self, new_name: str) -> None:
        bookmark = self._current_inspected_bookmark()
        if bookmark is None:
            return
        self._undo_stack.push(RenameBookmarkCommand(self._bookmark_repository, bookmark.id, bookmark.name, new_name))
        self._refresh_bookmarks()

    def _on_loop_settings_committed(
        self, enabled: bool, repeat_count: int | None, gap_ms: int, action: CompletionAction,
        fade_in_ms: int, fade_out_ms: int,
    ) -> None:
        bookmark = self._current_inspected_bookmark()
        if bookmark is None:
            return
        self._undo_stack.push(
            ChangeLoopCommand(
                self._bookmark_repository, bookmark.id,
                old=(
                    bookmark.loop_enabled, bookmark.repeat_count, bookmark.loop_gap_ms,
                    bookmark.completion_action, bookmark.fade_in_ms, bookmark.fade_out_ms,
                ),
                new=(enabled, repeat_count, gap_ms, action, fade_in_ms, fade_out_ms),
            )
        )
        self._refresh_bookmarks()

    # Direct follow-up request: "the columns for loop, fade in/out etc, they should
    # also be directly editable, e.g. clicking would give a drop menu options" --
    # these three mirror _on_loop_settings_committed's ChangeLoopCommand usage, but
    # target whichever bookmark row was edited in the list (not necessarily the one
    # currently loaded in the Inspector) and only touch the one field that changed.

    def _on_bookmark_panel_loop_edited(self, bookmark_id: UUID, loop_enabled: bool, repeat_count: int | None) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        self._push_bookmark_loop_change(
            bookmark, loop_enabled=loop_enabled, repeat_count=repeat_count,
            gap_ms=bookmark.loop_gap_ms, completion_action=bookmark.completion_action,
            fade_in_ms=bookmark.fade_in_ms, fade_out_ms=bookmark.fade_out_ms,
        )

    def _on_bookmark_panel_gap_edited(self, bookmark_id: UUID, gap_ms: int) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        self._push_bookmark_loop_change(
            bookmark, loop_enabled=bookmark.loop_enabled, repeat_count=bookmark.repeat_count,
            gap_ms=gap_ms, completion_action=bookmark.completion_action,
            fade_in_ms=bookmark.fade_in_ms, fade_out_ms=bookmark.fade_out_ms,
        )

    def _on_bookmark_panel_fade_in_edited(self, bookmark_id: UUID, fade_in_ms: int) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        self._push_bookmark_loop_change(
            bookmark, loop_enabled=bookmark.loop_enabled, repeat_count=bookmark.repeat_count,
            gap_ms=bookmark.loop_gap_ms, completion_action=bookmark.completion_action,
            fade_in_ms=fade_in_ms, fade_out_ms=bookmark.fade_out_ms,
        )

    def _on_bookmark_panel_fade_out_edited(self, bookmark_id: UUID, fade_out_ms: int) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        self._push_bookmark_loop_change(
            bookmark, loop_enabled=bookmark.loop_enabled, repeat_count=bookmark.repeat_count,
            gap_ms=bookmark.loop_gap_ms, completion_action=bookmark.completion_action,
            fade_in_ms=bookmark.fade_in_ms, fade_out_ms=fade_out_ms,
        )

    def _push_bookmark_loop_change(
        self, bookmark: Bookmark, *, loop_enabled: bool, repeat_count: int | None, gap_ms: int,
        completion_action: CompletionAction, fade_in_ms: int, fade_out_ms: int,
    ) -> None:
        self._undo_stack.push(
            ChangeLoopCommand(
                self._bookmark_repository, bookmark.id,
                old=(
                    bookmark.loop_enabled, bookmark.repeat_count, bookmark.loop_gap_ms,
                    bookmark.completion_action, bookmark.fade_in_ms, bookmark.fade_out_ms,
                ),
                new=(loop_enabled, repeat_count, gap_ms, completion_action, fade_in_ms, fade_out_ms),
            )
        )
        self._refresh_bookmarks()
        self._refresh_inspector_if_current(bookmark.id)

    def _on_inspector_start_committed(self, start_us: int) -> None:
        """Direct user request: "the begin/end time fields should be manually
        editable for refined adjustment" -- the Inspector's Start/End QLineEdits
        already existed and emitted these signals, but nothing was connected to
        them, so typing a new value and pressing Enter silently did nothing.
        """
        bookmark = self._current_inspected_bookmark()
        if bookmark is None:
            return
        if start_us < 0 or (bookmark.end_us is not None and start_us >= bookmark.end_us):
            self._load_bookmark_into_inspector(bookmark)  # reject and revert the field
            return
        self._undo_stack.push(
            ResizeBookmarkCommand(self._bookmark_repository, bookmark.id, "start", bookmark.start_us, start_us)
        )
        self._refresh_bookmarks()
        self._refresh_inspector_if_current(bookmark.id)

    def _on_inspector_end_committed(self, end_us: int) -> None:
        bookmark = self._current_inspected_bookmark()
        if bookmark is None or bookmark.end_us is None:
            return  # a point bookmark has no end to edit; the field is disabled anyway
        if end_us <= bookmark.start_us:
            self._load_bookmark_into_inspector(bookmark)  # reject and revert the field
            return
        self._undo_stack.push(
            ResizeBookmarkCommand(self._bookmark_repository, bookmark.id, "end", bookmark.end_us, end_us)
        )
        self._refresh_bookmarks()
        self._refresh_inspector_if_current(bookmark.id)

    def _on_rename_shortcut(self) -> None:
        if self._current_inspected_bookmark() is not None:
            self._inspector._name_edit.setFocus()
            self._inspector._name_edit.selectAll()

    def _on_delete_shortcut(self) -> None:
        bookmark = self._current_inspected_bookmark()
        if bookmark is None:
            return
        self._undo_stack.push(DeleteBookmarkCommand(self._bookmark_repository, bookmark))
        self._clear_inspector()
        self._refresh_bookmarks()

    def _on_delete_bookmark_requested(self, bookmark_ids: list) -> None:
        """"there is no button to select and delete a bookmark" (later: "i should be
        able to multiple select and delete or move") -- deletion already worked via
        the Delete key/Bookmark menu once loaded into the Inspector, but with no
        visible button, and no way to act on more than one row, it read as missing.
        Deletes whatever rows are selected in the bookmark LIST directly, independent
        of Inspector state. One undo-stack push per bookmark, so each is individually
        undoable/redoable, same granularity as every other bookmark command here.
        """
        inspected = self._current_inspected_bookmark()
        for bookmark_id in bookmark_ids:
            bookmark = self._bookmark_repository.get(bookmark_id)
            if bookmark is None:
                continue
            self._undo_stack.push(DeleteBookmarkCommand(self._bookmark_repository, bookmark))
            if inspected is not None and inspected.id == bookmark_id:
                self._clear_inspector()
        self._refresh_bookmarks()

    def _current_inspected_bookmark(self) -> Bookmark | None:
        return self._inspector.current_bookmark()

    def _load_bookmark_into_inspector(self, bookmark: Bookmark) -> None:
        """Single funnel for every "show this bookmark for editing" path."""
        self._inspector.load_bookmark(bookmark)

    def _clear_inspector(self) -> None:
        self._inspector.clear()

    def _refresh_bookmarks(self) -> None:
        if self._current_playlist_id is None and self._current_media_id is None:
            return
        if self._current_media_id is None:
            return
        bookmarks = self._bookmark_repository.list_for_playlist_media(
            self._current_playlist_id, self._current_media_id
        ) if self._current_playlist_id else self._bookmark_repository.list_global_for_media(self._current_media_id)
        self.load_bookmarks(bookmarks)
        # Every bookmark mutation (create/rename/delete/drag-move/drag-resize/loop
        # settings) funnels through this method -- one signal here lets Application
        # refresh the cross-song bookmark panel immediately instead of the edit only
        # showing up there after the next ~2s playlist poll happens to catch up.
        self.bookmarks_changed.emit()

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
