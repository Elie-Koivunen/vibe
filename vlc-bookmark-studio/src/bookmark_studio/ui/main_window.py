"""MainWindow: menu bar, QSplitter panel layout, active-context breadcrumb (spec #7-#8)."""
from __future__ import annotations

from uuid import UUID, uuid4

from PySide6.QtCore import QByteArray, QEvent, QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QUndoStack
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from bookmark_studio.app.commands import (
    ChangeLoopCommand,
    CreateBookmarkCommand,
    DeleteBookmarkCommand,
    EditBookmarkFieldsCommand,
    MoveBookmarkCommand,
    RenameBookmarkCommand,
    ResizeBookmarkCommand,
)
from bookmark_studio.domain.bookmark import Bookmark, default_bookmark_name
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.domain.equalizer import EqualizerSettings
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.tag_repository import TagRepository
from bookmark_studio.ui.bookmark_panel import BookmarkPanel
from bookmark_studio.ui.branding import APP_NAME, app_icon, logo_pixmap
from bookmark_studio.ui.deck_fader import VolumeStrip, level_to_percent
from bookmark_studio.ui.dialogs.tag_catalog_dialog import TagCatalogDialog
from bookmark_studio.ui.inspector import BookmarkInspector
from bookmark_studio.ui.playlist_panel import PlaylistPanel
from bookmark_studio.ui.transport import TransportBar
from bookmark_studio.ui.volume_eq_panel import VolumeEqPanel
from bookmark_studio.ui.waveform.scene import WaveformScene
from bookmark_studio.ui.waveform.view import WaveformView


def _scrolling(widget: QWidget, *, vertical_only: bool = False) -> QScrollArea:
    """`widget` in a frameless scroll area, so a small window scrolls it instead of
    growing to fit it."""
    area = QScrollArea()
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidgetResizable(True)
    area.setWidget(widget)
    if vertical_only:
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Room for a scroll bar, so one appearing never squeezes the buttons.
        area.setFixedWidth(widget.sizeHint().width() + area.verticalScrollBar().sizeHint().width())
    return area


class MainWindow(QMainWindow):
    """The main window. Its public signals and methods are the whole interface the
    composition root (app/application.py) uses; the widgets themselves stay private.

    Timecodes travel as `object`, not `int`: PySide6 marshals `int` through a 32-bit C++
    int, which wraps past ~35.8 minutes of microseconds.
    """

    # Bookmark intents
    loop_selection_requested = Signal(object, object)  # start_us, end_us ("Play" on a selection)
    launch_vlc_requested = Signal()
    play_bookmark_requested = Signal(object)  # UUID
    loop_bookmark_requested = Signal(object)  # UUID
    bookmark_reorder_requested = Signal(list)  # ordered list of bookmark UUIDs
    bookmark_song_display_requested = Signal(object)  # UUID -- just selected, not played
    bookmarks_changed = Signal()
    project_imported = Signal()  # a .vlcbmk was merged in; playlist recognition may change
    sync_requested = Signal()  # File > Sync Now
    playlist_refresh_requested = Signal()  # Playlist > Refresh
    volume_requested = Signal(int)  # the volume fader moved (0-512, 256 = 100 %)
    equalizer_requested = Signal(object)  # EqualizerSettings, from the Volume & EQ tab
    volume_max_requested = Signal()  # Max: up to 100 % over its ramp time
    volume_mute_requested = Signal()  # Mute: down to silence over its ramp time
    volume_reset_requested = Signal()  # Reset: back to the volume from before, at once
    volume_ramp_times_changed = Signal(int, int)  # Max ms, Mute ms
    quit_requested = Signal()  # Quit button / File > Quit / Ctrl+Q

    # Player controls
    play_pause_requested = Signal()
    stop_requested = Signal()
    seek_relative_requested = Signal(object)  # delta_us
    previous_track_requested = Signal()
    next_track_requested = Signal()
    previous_bookmark_requested = Signal()
    next_bookmark_requested = Signal()
    seek_requested = Signal(object)  # time_us within the song on screen

    # Waveform / playlist
    waveform_selection_changed = Signal(object)  # Selection | None
    playlist_item_selected = Signal(int)  # vlc_id -- previewed, not played
    playlist_item_double_clicked = Signal(int)  # vlc_id -- play it
    follow_player_toggled = Signal(bool)

    def __init__(
        self,
        bookmark_repository: BookmarkRepository,
        undo_stack: QUndoStack | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(900, 600)  # spec #132

        self._bookmark_repository = bookmark_repository
        self._undo_stack = undo_stack or QUndoStack(self)
        self._current_playlist_id: UUID | None = None
        self._current_media_id: UUID | None = None
        # Undo/redo changed the database but nothing refreshed the waveform, the list
        # or the Inspector afterwards. indexChanged now does -- except during our own
        # push(), whose callers refresh precisely themselves.
        self._pushing = False
        self._undo_stack.indexChanged.connect(self._on_undo_index_changed)

        self._logo = QLabel(self)
        self._logo.setPixmap(logo_pixmap(28))
        self._logo.setToolTip(APP_NAME)
        self._context_names = ("No playlist", "No track")
        self._breadcrumb = QLabel("No playlist › No track › 0 bookmarks", self)
        self._breadcrumb.setStyleSheet("padding: 4px 8px; font-weight: 600;")
        self._breadcrumb.setMinimumWidth(1)  # a long playlist name is cut off, never widens the window

        self._playlist_panel = PlaylistPanel(self)
        self._waveform_scene = WaveformScene()
        self._waveform_view = WaveformView(self._waveform_scene)
        self._bookmark_panel = BookmarkPanel(self)
        self._inspector = BookmarkInspector(self)
        self._transport = TransportBar(self)
        self._volume_eq = VolumeEqPanel(self)
        self._volume: VolumeStrip = self._volume_eq.volume_strip

        self._build_selection_bar()
        self._build_tool_column()
        self._build_menu_bar()
        self._build_layout()
        self._build_shortcuts()
        self._wire_signals()
        self.refresh_tag_catalog()
        self._fit_minimum_size()

    def _fit_minimum_size(self) -> None:
        """Never smaller than the panels need (wider fonts, e.g. Linux's, need more than
        900 px): below that the playback buttons were squeezed on top of each other.
        Polished first -- the style's metrics add to the widgets' minimums."""
        self.ensurePolished()
        minimum = self.minimumSizeHint().expandedTo(QSize(900, 600))
        if minimum != self.minimumSize():
            self.setMinimumSize(minimum)

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        # The panels' needs change after construction (style polish, texts): keep up.
        if event.type() == QEvent.Type.LayoutRequest:
            self._fit_minimum_size()
        return handled

    # -- layout --

    def _build_selection_bar(self) -> None:
        """The selection readout, at the top of the tool column beside the waveform: where
        a drag-selection starts and ends and how long it is. Read-only -- it follows the
        selection while it is dragged or resized (see SelectionItem's handles); saved
        bookmarks are edited in the list and the Bookmark tab."""
        self._selection_bar = QFrame(self)
        self._selection_bar.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self._selection_bar)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(1)
        self._selection_bar.setToolTip(
            "Drag on the waveform to select; [ and ] set the start and end at the playhead"
        )

        self._selection_label = QLabel("No selection", self)
        self._selection_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._selection_label)
        start_row = QHBoxLayout()
        start_row.setSpacing(4)
        self._selection_start_label = QLabel("--:--:--.---", self)
        start_row.addWidget(self._selection_start_label)
        start_row.addWidget(QLabel("→", self))
        start_row.addStretch(1)
        layout.addLayout(start_row)
        self._selection_end_label = QLabel("--:--:--.---", self)
        layout.addWidget(self._selection_end_label)
        self._selection_duration_label = QLabel("", self)
        layout.addWidget(self._selection_duration_label)

    def _build_tool_column(self) -> None:
        """View, bookmark and selection buttons in a column beside the waveform, grouped
        (spec #37's selection actions: a persistent control that enables when a
        selection exists, not a floating popup)."""
        self._tool_column = QWidget(self)
        layout = QVBoxLayout(self._tool_column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        def heading(text: str) -> None:
            label = QLabel(text, self._tool_column)
            label.setStyleSheet("font-size: 8pt; font-weight: 700; letter-spacing: 1px;")
            layout.addSpacing(4)
            layout.addWidget(label)

        def button(text: str, tooltip: str, slot) -> QPushButton:  # noqa: ANN001
            widget = QPushButton(text, self._tool_column)
            widget.setToolTip(tooltip)
            widget.clicked.connect(slot)
            return widget

        layout.addWidget(self._selection_bar)

        # Visible zoom buttons: Ctrl+wheel/Ctrl+0 (spec #84) alone are too easy to miss.
        heading("VIEW")
        zoom_row = QHBoxLayout()
        zoom_row.setSpacing(4)
        self._zoom_out_button = button("Zoom −", "Zoom out (Ctrl+-, or Ctrl+wheel)",
                                       lambda: self._waveform_view.zoom(0.8))
        self._zoom_in_button = button("Zoom +", "Zoom in (Ctrl++, or Ctrl+wheel)",
                                      lambda: self._waveform_view.zoom(1.25))
        zoom_row.addWidget(self._zoom_out_button)
        zoom_row.addWidget(self._zoom_in_button)
        layout.addLayout(zoom_row)
        self._zoom_fit_button = button("Fit", "Show the whole song (Ctrl+0)", self._waveform_view.fit_entire_media)
        layout.addWidget(self._zoom_fit_button)

        # Bookmark now is always enabled: it bookmarks the drag-selection when there is
        # one (like Bookmark selection), otherwise the playhead position.
        heading("BOOKMARK")
        self._bookmark_now_button = button(
            "Bookmark now", "Bookmark the selection, or the playhead position if nothing is selected",
            self._on_bookmark_now_clicked,
        )
        layout.addWidget(self._bookmark_now_button)
        self._bookmark_selection_button = button(
            "Bookmark selection", "Bookmark the selection (Ctrl+B)", self._on_bookmark_selection_clicked,
        )
        layout.addWidget(self._bookmark_selection_button)

        # "Play selection" loops the selection, like a new bookmark (loop on by default).
        heading("SELECTION")
        self._loop_selection_button = button("Play selection", "Loop the selection", self._on_loop_selection_clicked)
        layout.addWidget(self._loop_selection_button)
        self._clear_selection_button = button(
            "Clear selection", "Remove the selection", lambda: self._waveform_scene.clear_selection(),
        )
        layout.addWidget(self._clear_selection_button)
        layout.addStretch(1)

        self._set_selection_buttons_enabled(False)

    def _set_selection_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self._bookmark_selection_button, self._loop_selection_button, self._clear_selection_button,
        ):
            button.setEnabled(enabled)

    def _build_layout(self) -> None:
        # The deck: the transport above the waveform, the tool column (selection readout,
        # view, bookmark and selection buttons) beside it.
        deck = QWidget(self)
        deck_layout = QVBoxLayout(deck)
        deck_layout.setContentsMargins(0, 0, 0, 0)
        deck_layout.setSpacing(4)
        deck_layout.addWidget(self._transport)
        waveform_row = QHBoxLayout()
        waveform_row.setSpacing(6)
        waveform_row.addWidget(self._waveform_view, 1)
        waveform_row.addWidget(_scrolling(self._tool_column, vertical_only=True))
        deck_layout.addLayout(waveform_row, 1)

        top_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        top_splitter.addWidget(self._playlist_panel)
        top_splitter.addWidget(deck)
        top_splitter.setStretchFactor(1, 1)

        # Beside the bookmark list: the selected bookmark's settings, and the player's
        # volume and equalizer. Scroll areas keep a short window usable.
        self._side_tabs = QTabWidget(self)
        self._side_tabs.setDocumentMode(True)
        self._side_tabs.addTab(_scrolling(self._inspector), "Bookmark")
        self._side_tabs.addTab(_scrolling(self._volume_eq), "Volume && EQ")
        self._side_tabs.setTabToolTip(0, "The selected bookmark's settings")
        self._side_tabs.setTabToolTip(1, "The player's volume and equalizer")

        bottom_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        bottom_splitter.addWidget(self._bookmark_panel)
        bottom_splitter.addWidget(self._side_tabs)
        bottom_splitter.setStretchFactor(0, 1)
        bottom_splitter.setStretchFactor(1, 1)
        self._splitters = {"top": top_splitter, "bottom": bottom_splitter}

        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 0, 0)
        header.addWidget(self._logo)
        header.addWidget(self._breadcrumb, 1)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(top_splitter, 2)
        layout.addWidget(bottom_splitter, 1)
        self.setCentralWidget(central)

    def show_side_tab(self, name: str) -> None:
        """"bookmark" or "volume": brings that tab of the side panel to the front."""
        self._side_tabs.setCurrentIndex(1 if name == "volume" else 0)

    def _build_menu_bar(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("File")
        launch_vlc_action = file_menu.addAction("Launch VLC / Open Media...")
        launch_vlc_action.setShortcut("Ctrl+O")
        launch_vlc_action.triggered.connect(lambda *_: self.launch_vlc_requested.emit())
        file_menu.addSeparator()
        export_action = file_menu.addAction("Export Project...")
        export_action.triggered.connect(self._on_export_project)
        import_action = file_menu.addAction("Import Project...")
        import_action.triggered.connect(self._on_import_project)
        sync_action = file_menu.addAction("Sync Now")
        sync_action.setToolTip("Exchange bookmarks with the sync folder (see --sync-dir)")
        sync_action.triggered.connect(lambda *_: self.sync_requested.emit())
        file_menu.addSeparator()
        quit_action = file_menu.addAction("Quit")
        quit_action.setShortcut("Ctrl+Q")
        quit_action.setToolTip("Save everything, close the VLC this app launched, and quit")
        quit_action.triggered.connect(lambda *_: self.quit_requested.emit())

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
        seek_back_action.triggered.connect(lambda *_: self.seek_relative_requested.emit(-5_000_000))
        seek_forward_action = playback_menu.addAction("Seek +5s")
        seek_forward_action.setShortcut("Right")
        seek_forward_action.triggered.connect(lambda *_: self.seek_relative_requested.emit(5_000_000))

        playlist_menu = menu_bar.addMenu("Playlist")
        refresh_action = playlist_menu.addAction("Refresh")
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(lambda *_: self.playlist_refresh_requested.emit())

        tools_menu = menu_bar.addMenu("Tools")
        diagnostics_action = tools_menu.addAction("Diagnostics...")
        diagnostics_action.triggered.connect(self._on_show_diagnostics)

        help_menu = menu_bar.addMenu("Help")
        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self._on_show_about)

    def _build_shortcuts(self) -> None:
        QShortcut(QKeySequence("["), self).activated.connect(self._on_mark_selection_start)
        QShortcut(QKeySequence("]"), self).activated.connect(self._on_mark_selection_end)

    # -- wiring --

    def _wire_signals(self) -> None:
        self._waveform_scene.seek_requested.connect(self._on_seek_requested)
        self._waveform_scene.point_bookmark_requested.connect(self._on_point_bookmark_requested)
        self._waveform_scene.bookmark_activated.connect(self._on_bookmark_activated)
        self._waveform_scene.bookmark_move_finished.connect(self._on_bookmark_move_finished)
        self._waveform_scene.bookmark_resize_finished.connect(self._on_bookmark_resize_finished)
        self._waveform_scene.selection_changed.connect(self._on_selection_changed)
        self._waveform_scene.selection_preview_changed.connect(self._on_selection_preview_changed)

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
        # These two were never connected, so Tags/Notes typed in the Inspector were
        # silently discarded.
        self._inspector.tags_committed.connect(self._on_tags_committed)
        self._inspector.notes_committed.connect(self._on_notes_committed)
        self._inspector.edit_tags_requested.connect(self.edit_tags)

        self._playlist_panel.launch_vlc_requested.connect(self.launch_vlc_requested.emit)
        self._playlist_panel.quit_requested.connect(self.quit_requested.emit)
        self._volume.volume_changed.connect(self.volume_requested.emit)
        self._volume.volume_changed.connect(lambda level: self._transport.set_volume_percent(level_to_percent(level)))
        self._volume_eq.equalizer_changed.connect(self.equalizer_requested.emit)
        self._volume_eq.max_requested.connect(self.volume_max_requested.emit)
        self._volume_eq.mute_requested.connect(self.volume_mute_requested.emit)
        self._volume_eq.reset_requested.connect(self.volume_reset_requested.emit)
        self._volume_eq.ramp_times_changed.connect(self.volume_ramp_times_changed.emit)
        self._transport.volume_clicked.connect(self._on_volume_readout_clicked)
        self._playlist_panel.item_selected.connect(self.playlist_item_selected.emit)
        self._playlist_panel.item_double_clicked.connect(self.playlist_item_double_clicked.emit)
        self._playlist_panel.follow_vlc_toggled.connect(self.follow_player_toggled.emit)

        self._waveform_scene.seek_requested.connect(self.seek_requested.emit)
        self._waveform_scene.selection_changed.connect(self.waveform_selection_changed.emit)

        transport = self._transport
        transport.play_pause_clicked.connect(self.play_pause_requested.emit)
        transport.stop_clicked.connect(self.stop_requested.emit)
        transport.previous_track_clicked.connect(self.previous_track_requested.emit)
        transport.next_track_clicked.connect(self.next_track_requested.emit)
        transport.previous_bookmark_clicked.connect(self.previous_bookmark_requested.emit)
        transport.next_bookmark_clicked.connect(self.next_bookmark_requested.emit)

    # -- player display (called by the composition root) --

    def show_volume(self, level: int) -> None:
        """The player's volume (0-512) on the fader and the transport's readout; the
        fader ignores it while the user drags it."""
        self._volume.set_level(level)
        self._transport.set_volume_percent(level_to_percent(self._volume.level()))

    def volume_level(self) -> int:
        return self._volume.level()

    def _on_volume_readout_clicked(self) -> None:
        self.show_side_tab("volume")
        self._volume_eq.focus_volume()

    def show_equalizer(self, settings: EqualizerSettings) -> None:
        """Saved equalizer settings on the Volume & EQ tab (no equalizer_requested)."""
        self._volume_eq.set_equalizer(settings)

    def equalizer_settings(self) -> EqualizerSettings:
        return self._volume_eq.equalizer()

    def set_equalizer_support(self, supported: bool, band_hz: tuple[float, ...], note: str = "") -> None:
        """What the connected player can do: its band frequencies, or no equalizer."""
        self._volume_eq.set_band_frequencies(band_hz)
        self._volume_eq.set_equalizer_supported(supported, note)

    def show_volume_ramp_times(self, max_ms: int, mute_ms: int) -> None:
        """Saved Max/Mute ramp times (no volume_ramp_times_changed)."""
        self._volume_eq.set_ramp_times(max_ms, mute_ms)

    def volume_ramp_times(self) -> tuple[int, int]:
        return self._volume_eq.ramp_times()

    def set_volume_reset_level(self, level: int | None) -> None:
        """What Reset would restore (0-512), or None to disable it."""
        self._volume_eq.set_reset_level(level)

    # -- tags --

    def refresh_tag_catalog(self) -> None:
        """Re-reads the tag list (after the Edit tags window, or a sync)."""
        self._inspector.set_tag_catalog(TagRepository(self._bookmark_repository.connection).names())

    def edit_tags(self) -> bool:
        """Opens the Edit tags window. True if anything changed; then every view that
        shows tags is refreshed (a rename or removal touches bookmarks of any song)."""
        current = self._current_inspected_bookmark()
        dialog = TagCatalogDialog(
            TagRepository(self._bookmark_repository.connection), self,
            selected=current.tags[0] if current is not None and current.tags else None,
        )
        dialog.exec()
        self.refresh_tag_catalog()
        if dialog.changed:
            self._after_tag_catalog_change()
        return dialog.changed

    def _after_tag_catalog_change(self) -> None:
        current = self._current_inspected_bookmark()
        if current is not None:
            updated = self._bookmark_repository.get(current.id)
            if updated is None:
                self._clear_inspector()
            else:
                self._load_bookmark_into_inspector(updated)
        self._refresh_bookmarks()
        self.bookmarks_changed.emit()  # the cross-song list too

    # -- session state --

    def commit_pending_edits(self) -> None:
        """Saves anything still being typed in the Inspector (before quitting)."""
        self._inspector.commit_pending()

    def save_layout(self, settings) -> None:  # noqa: ANN001 - SettingsService (no UI import cycle)
        settings.set_window_geometry(self.saveGeometry())
        for name, splitter in self._splitters.items():
            settings.set_splitter_state(name, splitter.saveState())
        settings.set_panel_tab("side", self._side_tabs.currentIndex())
        settings.set_header_state("bookmarks", self._bookmark_panel.header_state())

    def restore_layout(self, settings) -> None:  # noqa: ANN001
        # The final minimum first: a saved size below it would otherwise be widened after
        # the panels were sized, and they would not come back as saved.
        self._fit_minimum_size()
        geometry = settings.window_geometry()
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            self.restoreGeometry(geometry)
        for name, splitter in self._splitters.items():
            state = settings.splitter_state(name)
            if isinstance(state, QByteArray) and not state.isEmpty():
                splitter.restoreState(state)
        tab = settings.panel_tab("side")
        if 0 <= tab < self._side_tabs.count():
            self._side_tabs.setCurrentIndex(tab)
        columns = settings.header_state("bookmarks")
        if isinstance(columns, QByteArray) and not columns.isEmpty():
            self._bookmark_panel.restore_header_state(columns)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Closing the window is quitting too: keep what is being typed.
        self.commit_pending_edits()
        super().closeEvent(event)

    def show_playback(self, time_us: int, duration_us: int | None, current_item_id: int | None,
                      *, is_playing: bool) -> None:
        self._transport.set_time(time_us, duration_us)
        self._playlist_panel.set_current_playing(current_item_id, is_playing=is_playing)

    def set_playhead(self, time_us: int, *, follow: bool = True) -> None:
        self._waveform_scene.set_playhead_time_us(time_us)
        if follow:
            self._waveform_view.follow_playhead(time_us)

    def playhead_time_us(self) -> int:
        return self._waveform_scene.playhead_time_us()

    def clear_waveform_selection(self) -> None:
        self._waveform_scene.clear_selection()

    def show_waveform(self, pyramid, duration_us: int) -> None:
        """Draws a (possibly partial) waveform; keeps "fit" exact, leaves a manual zoom."""
        self._waveform_scene.set_waveform(pyramid, duration_us)
        if self._waveform_view._fit_mode:
            self._waveform_view.fit_entire_media()

    def fit_waveform(self) -> None:
        self._waveform_view.fit_entire_media()

    def waveform_duration_us(self) -> int:
        return self._waveform_scene._duration_us

    def set_playlist(self, items: list, bookmark_counts: dict[int, int]) -> None:
        self._playlist_panel.set_playlist(items, bookmark_counts)

    def select_playlist_item(self, vlc_id: int | None) -> None:
        self._playlist_panel.select_item(vlc_id)

    def set_follow_player(self, enabled: bool, *, notify: bool = True) -> None:
        self._playlist_panel.set_follow_vlc(enabled, notify=notify)

    def follow_player_enabled(self) -> bool:
        return self._playlist_panel.follow_vlc_enabled()

    # -- context --

    def set_context(self, *, playlist_name: str, track_name: str, playlist_id: UUID | None,
                     media_id: UUID | None, bookmark_count: int, duration_us: int = 0) -> None:
        self._current_playlist_id = playlist_id
        self._current_media_id = media_id
        self._context_names = (playlist_name, track_name)
        self._show_breadcrumb(bookmark_count)
        self._waveform_scene.set_duration_us(duration_us)

    def _show_breadcrumb(self, bookmark_count: int) -> None:
        playlist_name, track_name = self._context_names
        noun = "bookmark" if bookmark_count == 1 else "bookmarks"
        self._breadcrumb.setText(f"{playlist_name} › {track_name} › {bookmark_count} {noun}")

    def load_bookmarks(self, bookmarks: list[Bookmark]) -> None:
        """Waveform-scoped bookmarks for the one song currently displayed. The
        bookmark LIST panel is fed separately, from load_all_bookmarks: it lists every
        song's bookmarks, not just this one's.
        """
        self._waveform_scene.set_bookmarks(bookmarks)
        self._show_breadcrumb(len(bookmarks))  # stays right as bookmarks come and go

    def load_all_bookmarks(self, bookmarks: list[Bookmark], song_names: dict[UUID, str]) -> None:
        self._bookmark_panel.set_bookmarks(bookmarks, song_names)

    def set_playhead_time_us(self, time_us: int) -> None:
        self._waveform_scene.set_playhead_time_us(time_us)

    def set_connected(self, connected: bool) -> None:
        """Drives both the connection indicator (PlaylistPanel, above Launch VLC)
        and the transport buttons'
        enabled state (TransportBar) together, so callers have one place to report
        connection changes instead of reaching into two widgets.
        """
        self._transport.set_transport_enabled(connected)
        self._playlist_panel.set_connected(connected)

    # -- handlers: selection bar --

    def _on_selection_changed(self, selection: object) -> None:
        from bookmark_studio.domain.selection import Selection

        if isinstance(selection, Selection):
            self._selection_label.setText("Selection")
            self._show_selection_range(selection.start_us, selection.end_us)
            self._set_selection_buttons_enabled(True)
        else:
            self._selection_label.setText("No selection")
            self._selection_start_label.setText("--:--:--.---")
            self._selection_end_label.setText("--:--:--.---")
            self._selection_duration_label.setText("")
            self._set_selection_buttons_enabled(False)
        # Mirrors the selection in the Inspector's Start/End fields whenever no
        # bookmark is loaded there (see
        # BookmarkInspector.show_selection's docstring for why it won't clobber an
        # actively-inspected bookmark).
        self._inspector.show_selection(selection if isinstance(selection, Selection) else None)

    def _on_selection_preview_changed(self, start_us: int, end_us: int) -> None:
        """Live readout while dragging one of SelectionItem's resize handles: fires
        continuously during the drag, unlike
        _on_selection_changed (which only fires once the drag settles)."""
        from bookmark_studio.domain.selection import Selection

        self._show_selection_range(start_us, end_us)
        self._inspector.show_selection(Selection(start_us=start_us, end_us=end_us))

    def _show_selection_range(self, start_us: int, end_us: int) -> None:
        from bookmark_studio.domain.timecode import format_timecode

        self._selection_start_label.setText(format_timecode(start_us))
        self._selection_end_label.setText(format_timecode(end_us))
        self._selection_duration_label.setText(f"length {format_timecode(max(0, end_us - start_us))}")

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
            # New bookmarks loop forever by default (repeat_count=None means "forever").
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
        self._push(CreateBookmarkCommand(self._bookmark_repository, bookmark))
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
            # Selecting a bookmark shows its song: Application knows which playlist row
            # that is (this window has no playlist snapshot of its own).
            self.bookmark_song_display_requested.emit(bookmark_id)

    def _on_bookmark_move_finished(self, bookmark_id: UUID, start_us: int, end_us: int) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None or (bookmark.start_us, bookmark.end_us) == (start_us, end_us):
            return  # a plain click on a bookmark is not a move (no empty undo step)
        self._push(
            MoveBookmarkCommand(self._bookmark_repository, bookmark_id, bookmark.start_us, bookmark.end_us, start_us, end_us)
        )
        self._refresh_bookmarks()
        self._refresh_inspector_if_current(bookmark_id)

    def _on_bookmark_resize_finished(self, bookmark_id: UUID, handle: str, value_us: int) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        old_value = bookmark.start_us if handle == "start" else bookmark.end_us
        if old_value is None or old_value == value_us:
            return
        self._push(
            ResizeBookmarkCommand(self._bookmark_repository, bookmark_id, handle, old_value, value_us)
        )
        self._refresh_bookmarks()
        self._refresh_inspector_if_current(bookmark_id)

    def _refresh_inspector_if_current(self, bookmark_id: UUID) -> None:
        """Reloads the Inspector after a bookmark it shows was dragged or resized on
        the waveform, so its Start/End fields don't keep the old values."""
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
        if new_name == bookmark.name:
            return
        self._push(RenameBookmarkCommand(self._bookmark_repository, bookmark.id, bookmark.name, new_name))
        self._refresh_bookmarks()
        self._sync_inspector_snapshot(bookmark.id)

    def _on_tags_committed(self, tags: tuple) -> None:
        bookmark = self._current_inspected_bookmark()
        if bookmark is None or tuple(sorted(tags)) == tuple(sorted(bookmark.tags)):
            return
        self._push(
            EditBookmarkFieldsCommand(
                self._bookmark_repository, bookmark.id, "Edit bookmark tags",
                old={"tags": bookmark.tags}, new={"tags": tuple(tags)},
            )
        )
        self._refresh_bookmarks()
        self._sync_inspector_snapshot(bookmark.id)

    def _on_notes_committed(self, notes: object) -> None:
        bookmark = self._current_inspected_bookmark()
        if bookmark is None or notes == bookmark.notes:
            return
        self._push(
            EditBookmarkFieldsCommand(
                self._bookmark_repository, bookmark.id, "Edit bookmark notes",
                old={"notes": bookmark.notes}, new={"notes": notes},
            )
        )
        self._refresh_bookmarks()
        self._sync_inspector_snapshot(bookmark.id)

    def _push(self, command) -> None:
        self._pushing = True
        try:
            self._undo_stack.push(command)
        finally:
            self._pushing = False

    def _on_undo_index_changed(self, _index: int) -> None:
        if self._pushing:
            return
        self._refresh_bookmarks()
        current = self._current_inspected_bookmark()
        if current is None:
            return
        updated = self._bookmark_repository.get(current.id)
        if updated is None:
            self._clear_inspector()
        else:
            self._load_bookmark_into_inspector(updated)

    def _sync_inspector_snapshot(self, bookmark_id: UUID) -> None:
        """After an Inspector-originated edit, remember the saved values without
        touching the fields the user may still be typing in."""
        current = self._current_inspected_bookmark()
        if current is not None and current.id == bookmark_id:
            self._inspector.set_snapshot(self._bookmark_repository.get(bookmark_id))

    def _on_loop_settings_committed(
        self, enabled: bool, repeat_count: int | None, gap_ms: int, action: CompletionAction,
        fade_in_ms: int, fade_out_ms: int,
    ) -> None:
        bookmark = self._current_inspected_bookmark()
        if bookmark is None:
            return
        self._push(
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
        self._sync_inspector_snapshot(bookmark.id)

    # In-place edits from the bookmark list: like _on_loop_settings_committed, but
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
        self._push(
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
        """A start time typed into the Inspector (Enter commits)."""
        bookmark = self._current_inspected_bookmark()
        if bookmark is None:
            return
        if start_us < 0 or (bookmark.end_us is not None and start_us >= bookmark.end_us):
            self._load_bookmark_into_inspector(bookmark)  # reject and revert the field
            return
        self._push(
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
        self._push(
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
        self._push(DeleteBookmarkCommand(self._bookmark_repository, bookmark))
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
            self._push(DeleteBookmarkCommand(self._bookmark_repository, bookmark))
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

    def build_export_data(self):
        """Everything "Save Bookmarks..." writes: the active playlist's bookmarks for every
        song (not just the one on screen), the media they belong to, the playlist's lanes,
        and the playlist's order
        and signatures so the project is recognised again after an import. Without a
        playlist context, the current song's global bookmarks."""
        from bookmark_studio.persistence.lane_repository import LaneRepository
        from bookmark_studio.persistence.media_repository import MediaRepository
        from bookmark_studio.persistence.playlist_repository import PlaylistRepository
        from bookmark_studio.project.export_service import ProjectData

        conn = self._bookmark_repository.connection
        media_repo = MediaRepository(conn)
        playlist_repo = PlaylistRepository(conn)
        playlists = []
        lanes = []
        items: list = []
        signatures: list = []
        if self._current_playlist_id is not None:
            record = playlist_repo.get(self._current_playlist_id)
            if record is not None:
                playlists.append(record.playlist)
            bookmarks = self._bookmark_repository.list_for_playlist(self._current_playlist_id)
            lanes = LaneRepository(conn).list_for_playlist(self._current_playlist_id)
            item_media_ids = playlist_repo.list_item_media_ids(self._current_playlist_id)
            if item_media_ids:
                items.append((self._current_playlist_id, item_media_ids))
            signatures = [(self._current_playlist_id, s) for s in playlist_repo.signatures_for(self._current_playlist_id)]
        elif self._current_media_id is not None:
            bookmarks = self._bookmark_repository.list_global_for_media(self._current_media_id)
            item_media_ids = []
        else:
            bookmarks = []
            item_media_ids = []
        media_ids = list(dict.fromkeys([b.media_id for b in bookmarks] + list(item_media_ids)))
        if self._current_media_id is not None and self._current_media_id not in media_ids:
            media_ids.append(self._current_media_id)
        media = [m for m in (media_repo.get(mid) for mid in media_ids) if m is not None]
        return ProjectData(
            playlists=playlists, media=media, bookmarks=bookmarks, lanes=lanes,
            playlist_items=items, playlist_signatures=signatures,
            tag_state=TagRepository(conn).export_state(),
        )

    def _on_export_project(self) -> None:
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        from bookmark_studio.project.export_service import export_project

        path_str, _filter = QFileDialog.getSaveFileName(self, "Export Project", "", "Bookmark Studio Project (*.vlcbmk)")
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix.lower() != ".vlcbmk":  # Linux file dialogs don't always add it
            path = path.with_name(path.name + ".vlcbmk")
        data = self.build_export_data()
        try:
            export_project(path, data)
        except OSError as exc:
            QMessageBox.critical(self, "Export Project", f"Export failed: {exc}")
            return
        QMessageBox.information(
            self, "Export Project", f"Exported {len(data.bookmarks)} bookmark(s) to {path}"
        )

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
        self.bookmarks_changed.emit()
        self.project_imported.emit()

    def _on_show_diagnostics(self) -> None:
        lines = [
            f"Current playlist: {self._current_playlist_id or '(none)'}",
            f"Current media: {self._current_media_id or '(none)'}",
            f"Bookmarks loaded: {self._bookmark_panel._tree.topLevelItemCount()}",
        ]
        QMessageBox.information(self, "Diagnostics", "\n".join(lines))

    def _on_show_about(self) -> None:
        from bookmark_studio import __version__

        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        box.setIconPixmap(logo_pixmap(96))
        box.setText(f"<h3>{APP_NAME} {__version__}</h3>")
        box.setInformativeText(
            "Playlist-aware visual bookmarking and looping for VLC Media Player.<br><br>"
            "Free software under the GNU GPL v3."
        )
        box.exec()
