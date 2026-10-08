"""MainWindow: menu bar, QSplitter panel layout, active-context breadcrumb (spec #7-#8)."""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

from PySide6.QtCore import QByteArray, QEvent, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut, QUndoStack
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from bookmark_studio.about import COPYRIGHT, DEVELOPERS, LICENSE_SUMMARY, OWNER, REPOSITORY_URL, license_text
from bookmark_studio.app.commands import (
    ChangeLoopCommand,
    CreateBookmarkCommand,
    DeleteBookmarkCommand,
    EditBookmarkFieldsCommand,
    MoveBookmarkCommand,
    RenameBookmarkCommand,
    ResizeBookmarkCommand,
)
from bookmark_studio.domain.bookmark import Bookmark, default_bookmark_name, unique_bookmark_name
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.domain.equalizer import EqualizerSettings
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.tag_repository import TagRepository
from bookmark_studio.ui.bookmark_panel import BookmarkPanel
from bookmark_studio.ui.bookmark_tracks import BookmarkTracksView, TrackBookmark
from bookmark_studio.ui.branding import APP_NAME, app_icon, logo_pixmap
from bookmark_studio.ui.deck_fader import VolumeStrip, level_to_percent
from bookmark_studio.ui.dialogs.extract_dialog import ExtractDialog
from bookmark_studio.ui.dialogs.tag_catalog_dialog import TagCatalogDialog
from bookmark_studio.ui.inspector import BookmarkInspector
from bookmark_studio.ui.playlist_panel import PlaylistPanel
from bookmark_studio.ui.qt_helpers import SELECTED_TAB_COLOR, SELECTED_TAB_TEXT_COLOR, style_tabs
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


# The selection readout while a new bookmark is being marked out: the app's orange.
MARKING_STYLE = (f"QFrame#selectionBar {{ background: {SELECTED_TAB_COLOR}; border: 1px solid #c97700; border-radius: 4px; }}"
                 f" QFrame#selectionBar QLabel {{ color: {SELECTED_TAB_TEXT_COLOR}; background: transparent; }}")


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
    volume_normalize_requested = Signal()  # Normalize: to its level (80 % to begin with)
    volume_reset_requested = Signal()  # Reset: to its level (50 % to begin with)
    volume_levels_changed = Signal(int, int)  # Normalize %, Reset %
    volume_ramp_times_changed = Signal(int, int)  # Max ms, Mute ms
    equalizer_glide_ms_changed = Signal(int)  # how long a preset change takes
    tempo_changed = Signal(int)  # the BPM fader: the change from its middle
    detected_bpm_corrected = Signal(float)  # the user's correction of the playing song's BPM
    tempo_align_skew_requested = Signal()
    tempo_increase_requested = Signal()
    tempo_reset_requested = Signal()
    tempo_lower_requested = Signal()
    tempo_buttons_changed = Signal(object)  # TempoButtons (Step, Glide)
    tempo_switched = Signal(bool)  # the BPM panel's on/off switch
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
    bookmark_list_selection_changed = Signal()  # the rows selected in the bookmark list
    bookmark_track_double_clicked = Signal(int)  # BM playback view: -1 previous, 0 middle, 1 next
    # A bookmark was just made from the waveform's selection (before the selection is
    # cleared): a selection playing right now carries on as that bookmark.
    selection_bookmarked = Signal(object)  # bookmark id
    extract_bookmarks_requested = Signal(list)  # bookmark ids (Extract...)
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
        # The bookmark list's columns were given room as the window opened (see
        # _on_bookmark_columns_changed).
        self._bookmark_columns_fitted = False
        self._bookmark_fit_pending = False
        self._tab_width_to_keep: int | None = None

        self._context_names = ("No playlist", "No track")
        self._breadcrumb = QLabel("No playlist › No track › 0 bookmarks", self)
        self._breadcrumb.setStyleSheet("padding: 2px 4px; font-weight: 600;")
        self._breadcrumb.setMinimumWidth(1)  # a long playlist name is cut off, never widens the window

        self._playlist_panel = PlaylistPanel(self)
        self._waveform_scene = WaveformScene()
        self._waveform_view = WaveformView(self._waveform_scene)
        self._bookmark_tracks = BookmarkTracksView(self)
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
        # The splitters know their panels' needs at once; the layouts above them only a
        # few event passes later (one nesting level per pass). Ask them directly.
        splitters = getattr(self, "_splitters", None)
        central = self.centralWidget()
        central_layout = central.layout() if central is not None else None
        if splitters is not None and central_layout is not None:
            margins = central_layout.contentsMargins()
            panels = splitters["vertical"].minimumSizeHint().width() + margins.left() + margins.right()
            minimum = minimum.expandedTo(QSize(panels, 0))
        if minimum != self.minimumSize():
            self.setMinimumSize(minimum)

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        # The panels' needs change after construction (style polish, texts): keep up.
        if event.type() == QEvent.Type.LayoutRequest:
            self._fit_minimum_size()
        return handled

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.LayoutRequest and isinstance(watched, QSplitter):
            self._fit_minimum_size()  # a panel's needs changed: keep the window big enough
        return super().eventFilter(watched, event)

    # -- layout --

    def _build_selection_bar(self) -> None:
        """The selection readout, at the top of the tool column beside the waveform: where
        a drag-selection starts and ends and how long it is. Read-only -- it follows the
        selection while it is dragged or resized (see SelectionItem's handles); saved
        bookmarks are edited in the list and the Bookmark tab."""
        self._selection_bar = QFrame(self)
        self._selection_bar.setObjectName("selectionBar")
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
        """Beside the waveform, in the Bookmarking tab (0.10.0): the selection readout and
        View (zoom). Beside the tabs, for both of them: the Playback group (time, playback
        buttons with 🔁, volume). Bookmark selection and the selection's Play / Clear are in
        the Bookmark Studio tab (0.9.0). The selection's buttons are a persistent control
        that enables when a selection exists (spec #37), not a floating popup."""
        self._tool_column = QWidget(self)
        playback = QVBoxLayout(self._tool_column)
        playback.setContentsMargins(0, 0, 0, 0)
        playback.setSpacing(4)
        self._bookmarking_column = QWidget(self)
        layout = QVBoxLayout(self._bookmarking_column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        def heading(text: str, column: QWidget, into: QVBoxLayout) -> None:
            label = QLabel(text, column)
            label.setStyleSheet("font-size: 8pt; font-weight: 700; letter-spacing: 1px;")
            into.addSpacing(4)
            into.addWidget(label)

        def button(text: str, tooltip: str, slot) -> QPushButton:  # noqa: ANN001
            widget = QPushButton(text, self._bookmarking_column)
            widget.setToolTip(tooltip)
            widget.clicked.connect(slot)
            return widget

        heading("PLAYBACK", self._tool_column, playback)
        playback.addWidget(self._transport)
        playback.addStretch(1)

        layout.addWidget(self._selection_bar)
        # Visible zoom buttons: Ctrl+wheel/Ctrl+0 (spec #84) alone are too easy to miss.
        heading("VIEW", self._bookmarking_column, layout)
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
        # Under Fit (0.10.0): the same Bookmark selection as in the Bookmark Studio tab,
        # beside the waveform the selection is made on.
        layout.addSpacing(6)
        self._bookmark_selection_beside_waveform = QPushButton("Bookmark selection", self._bookmarking_column)
        self._bookmark_selection_beside_waveform.setToolTip(self._inspector.bookmark_selection_button.toolTip())
        layout.addWidget(self._bookmark_selection_beside_waveform)

        layout.addStretch(1)

        # In the Bookmark Studio tab, above the name (no "Bookmark now" since 0.9.0: it did
        # what Bookmark selection does, or a point bookmark -- Bookmark menu). The
        # selection's Play loops it, like a new bookmark.
        self._bookmark_selection_button = self._inspector.bookmark_selection_button
        self._bookmark_selection_button.clicked.connect(self._on_bookmark_selection_clicked)
        self._bookmark_selection_beside_waveform.clicked.connect(self._on_bookmark_selection_clicked)
        self._loop_selection_button = self._inspector.play_selection_button
        self._loop_selection_button.clicked.connect(self._on_loop_selection_clicked)
        self._clear_selection_button = self._inspector.clear_selection_button
        self._clear_selection_button.clicked.connect(lambda: self._waveform_scene.clear_selection())

        self._set_selection_buttons_enabled(False)

    def _set_selection_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self._bookmark_selection_button, self._loop_selection_button, self._clear_selection_button,
            self._bookmark_selection_beside_waveform,
        ):
            button.setEnabled(enabled)

    def _build_layout(self) -> None:
        # The deck (0.10.0): two tabs -- Bookmarking (the waveform at full height, the
        # selection readout and View beside it) and BM playback view (three bookmark
        # tracks) -- and the Playback group beside them, for both.
        bookmarking = QWidget(self)
        bookmarking_layout = QVBoxLayout(bookmarking)
        bookmarking_layout.setContentsMargins(0, 2, 0, 0)
        bookmarking_layout.setSpacing(2)
        # Playlist › song › bookmarks, above the waveform (0.10.0; it was under the menu).
        bookmarking_layout.addWidget(self._breadcrumb)
        waveform_row = QHBoxLayout()
        waveform_row.setSpacing(6)
        waveform_row.addWidget(self._waveform_view, 1)
        waveform_row.addWidget(_scrolling(self._bookmarking_column, vertical_only=True))
        bookmarking_layout.addLayout(waveform_row, 1)
        self._view_tabs = QTabWidget(self)
        self._view_tabs.setDocumentMode(True)
        self._view_tabs.addTab(bookmarking, "Bookmarking")
        self._view_tabs.addTab(self._bookmark_tracks, "BM playback view")
        style_tabs(self._view_tabs)
        self._view_tabs.setTabToolTip(0, "The song's waveform: make, move and resize bookmarks")
        self._view_tabs.setTabToolTip(1, "The bookmark playing with the one before and after it in the list")
        deck = QWidget(self)
        deck_layout = QHBoxLayout(deck)
        deck_layout.setContentsMargins(0, 0, 0, 0)
        deck_layout.setSpacing(6)
        deck_layout.addWidget(self._view_tabs, 1)
        deck_layout.addWidget(_scrolling(self._tool_column, vertical_only=True))

        top_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        top_splitter.addWidget(self._playlist_panel)
        top_splitter.addWidget(deck)
        top_splitter.setStretchFactor(1, 1)

        # Beside the bookmark list: the selected bookmark's settings, and the player's
        # volume and equalizer. Scroll areas keep a short window usable.
        self._side_tabs = QTabWidget(self)
        self._side_tabs.setDocumentMode(True)
        self._side_tabs.addTab(_scrolling(self._inspector), "Bookmark Studio")
        self._side_tabs.addTab(_scrolling(self._volume_eq), "Volume && EQ")
        style_tabs(self._side_tabs)
        self._side_tabs.setTabToolTip(
            0, "New bookmarks, the selection, and the selected bookmark's settings (or a new one's)",
        )
        self._side_tabs.setTabToolTip(1, "The player's volume and equalizer")

        bottom_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        bottom_splitter.addWidget(self._bookmark_panel)
        bottom_splitter.addWidget(self._side_tabs)
        bottom_splitter.setStretchFactor(0, 1)
        bottom_splitter.setStretchFactor(1, 1)
        # The top (waveform) and bottom (lists, tabs) halves share the height; the divider
        # can be dragged, and a tab that needs more room takes it (_fit_side_tab).
        vertical_splitter = QSplitter(Qt.Orientation.Vertical, self)
        vertical_splitter.addWidget(top_splitter)
        vertical_splitter.addWidget(bottom_splitter)
        vertical_splitter.setStretchFactor(0, 2)
        vertical_splitter.setStretchFactor(1, 1)
        vertical_splitter.setChildrenCollapsible(False)
        self._splitters = {"top": top_splitter, "bottom": bottom_splitter, "vertical": vertical_splitter}
        self._side_tabs.currentChanged.connect(lambda _index: self._schedule_side_tab_fit())
        # A splitter re-laying out its panels doesn't reliably tell the window above it
        # (nested in the vertical splitter, the window's minimum went stale): pass it on.
        for splitter in self._splitters.values():
            splitter.installEventFilter(self)

        # (No logo row under the menu since 0.10.0: the window and About carry the logo,
        # the breadcrumb is in the Bookmarking tab.)
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setSpacing(6)
        layout.addWidget(vertical_splitter, 1)
        self.setCentralWidget(central)

    # -- room for the side tab (no scrolling) --

    def _schedule_side_tab_fit(self) -> None:
        if self.isVisible():
            QTimer.singleShot(0, self._fit_side_tab)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._fit_minimum_size()
        super().showEvent(event)
        self._schedule_side_tab_fit()
        self._schedule_bookmark_fit()

    def _screen_rect(self):  # noqa: ANN202 - QRect | None
        """The room the screen has for the window (None: not known)."""
        return self.screen().availableGeometry() if self.screen() is not None else None

    def _grow_window(self, extra_width: int, extra_height: int) -> None:
        """Wider / taller by that much, as far as the screen allows; never smaller than now,
        and kept on the screen."""
        screen = self._screen_rect()
        width = self.width() + max(0, extra_width)
        height = self.height() + max(0, extra_height)
        if screen is not None:
            width = max(self.width(), min(width, screen.width()))
            height = max(self.height(), min(height, screen.height()))
        self.resize(width, height)
        if screen is not None:
            frame = self.frameGeometry()
            dx = min(0, screen.right() - frame.right())
            dy = min(0, screen.bottom() - frame.bottom())
            if dx or dy:
                self.move(max(screen.left(), frame.left() + dx), max(screen.top(), frame.top() + dy))

    # -- room for the bookmark list's columns --

    def _schedule_bookmark_fit(self) -> None:
        if self.isVisible() and not self._bookmark_fit_pending:  # several changes at once: one fit
            self._bookmark_fit_pending = True
            QTimer.singleShot(0, self._fit_bookmark_columns)

    def _on_bookmark_columns_changed(self, by_user: bool) -> None:
        """Room for the columns when the user shows one, and once as the window opens with
        its bookmarks -- not at every later reload (a window the user narrowed stays so)."""
        if by_user or not self._bookmark_columns_fitted:
            self._schedule_bookmark_fit()

    def _fit_bookmark_columns(self, *, again: bool = True) -> None:
        """Every column of the bookmark list shown, without scrolling sideways: the window
        widens (within the screen; only ever wider) and the list gets all the new room --
        the side tab keeps the width the user gave it."""
        self._bookmark_fit_pending = False
        if not self.isVisible():
            return
        if self._bookmark_panel.row_count():
            self._bookmark_columns_fitted = True
        short = self._bookmark_panel.columns_short_by()
        if short <= 0 or self.isMaximized() or self.isFullScreen():
            return
        bottom = self._splitters["bottom"]
        if self._tab_width_to_keep is None:  # (still being handed back: the width from before)
            self._tab_width_to_keep = bottom.sizes()[1]
        before = self.width()
        self._grow_window(short, 0)
        if self.width() > before:
            # Once the splitter has shared out the new room: all of it to the list.
            QTimer.singleShot(0, lambda: self._keep_tab_width(again=again))
        else:
            self._tab_width_to_keep = None

    def _keep_tab_width(self, *, again: bool = False) -> None:
        tab_width, self._tab_width_to_keep = self._tab_width_to_keep, None
        if tab_width is None:
            return
        bottom = self._splitters["bottom"]
        left, right = bottom.sizes()
        # (Never less than the open tab's page needs -- its own fit may have run meanwhile.)
        area = self._side_tabs.currentWidget()
        page = area.widget() if isinstance(area, QScrollArea) else None
        if isinstance(area, QScrollArea) and page is not None:
            tab_width = max(tab_width, page.minimumSizeHint().width() + right - area.viewport().width())
        if right != tab_width:
            bottom.setSizes([left + right - tab_width, tab_width])
        if again and self._bookmark_panel.columns_short_by() > 0:
            # The tab needed some of it back (it was below its page's needs): once more.
            QTimer.singleShot(0, lambda: self._fit_bookmark_columns(again=False))

    def _fit_side_tab(self, *, second_pass: bool = False) -> None:
        """Makes the open side tab (Bookmark Studio, Volume & EQ) fit without scrolling:
        first by taking room from the bookmark list and the waveform (down to what they
        need), then by enlarging the window (within the screen). Only ever grows it."""
        area = self._side_tabs.currentWidget()
        if not isinstance(area, QScrollArea) or not self.isVisible():
            return
        page = area.widget()
        if page is None:
            return
        # Scroll bars appear only below the page's minimum: that is all it needs. (Asking
        # for more would rearrange a layout the user set up, every time a tab opens.)
        wanted = page.minimumSizeHint()
        short_w = wanted.width() - area.viewport().width()
        short_h = wanted.height() - area.viewport().height()
        if short_w <= 0 and short_h <= 0:
            return
        bottom, vertical = self._splitters["bottom"], self._splitters["vertical"]
        if short_w > 0:
            left, right = bottom.sizes()
            take = min(short_w, max(0, left - self._bookmark_panel.minimumSizeHint().width()))
            if take > 0:
                bottom.setSizes([left - take, right + take])
                short_w -= take
        if short_h > 0:
            top, low = vertical.sizes()
            take = min(short_h, max(0, top - self._splitters["top"].minimumSizeHint().height()))
            if take > 0:
                vertical.setSizes([top - take, low + take])
                short_h -= take
        if (short_w > 0 or short_h > 0) and not second_pass and not (self.isMaximized() or self.isFullScreen()):
            self._grow_window(short_w, short_h)
            # The splitters shared the new room; hand it to the tab once settled.
            QTimer.singleShot(0, lambda: self._fit_side_tab(second_pass=True))

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
        duplicate_action = bookmark_menu.addAction("Duplicate Selected Bookmarks")
        duplicate_action.setShortcut("Ctrl+D")
        duplicate_action.triggered.connect(self._bookmark_panel.request_duplicate)
        extract_action = bookmark_menu.addAction("Extract Audio of Selected Bookmarks...")
        extract_action.setShortcut("Ctrl+E")
        extract_action.triggered.connect(self._bookmark_panel.request_extract)
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
        repository_action = help_menu.addAction("GitHub Repository")
        repository_action.setToolTip(REPOSITORY_URL)
        repository_action.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(REPOSITORY_URL)))
        license_action = help_menu.addAction("License")
        license_action.triggered.connect(self._on_show_license)
        help_menu.addSeparator()
        about_action = help_menu.addAction(f"About {APP_NAME}")
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
        self._bookmark_panel.extract_requested.connect(self.extract_bookmarks_requested.emit)
        self._bookmark_panel.duplicate_requested.connect(self._on_duplicate_requested)
        self._bookmark_panel.columns_changed.connect(self._on_bookmark_columns_changed)
        self._bookmark_panel.play_bookmark_requested.connect(self.play_bookmark_requested.emit)
        self._bookmark_panel.loop_bookmark_requested.connect(self.loop_bookmark_requested.emit)
        self._bookmark_panel.delete_bookmark_requested.connect(self._on_delete_bookmark_requested)
        self._bookmark_panel.reorder_requested.connect(self.bookmark_reorder_requested.emit)
        self._bookmark_panel.loop_edited.connect(self._on_bookmark_panel_loop_edited)
        self._bookmark_panel.gap_edited.connect(self._on_bookmark_panel_gap_edited)
        self._bookmark_panel.fade_in_edited.connect(self._on_bookmark_panel_fade_in_edited)
        self._bookmark_panel.fade_out_edited.connect(self._on_bookmark_panel_fade_out_edited)
        self._bookmark_panel.completion_edited.connect(self._on_bookmark_panel_completion_edited)
        # The 🔁 among the playback buttons loops the bookmark selected in the list.
        self._bookmark_panel.loop_target_changed.connect(self._transport.set_loop_target)
        self._bookmark_panel.selection_changed.connect(self.bookmark_list_selection_changed.emit)
        self._bookmark_tracks.track_double_clicked.connect(self.bookmark_track_double_clicked.emit)
        self._transport.loop_bookmark_clicked.connect(self._bookmark_panel.loop_selected)

        self._inspector.name_committed.connect(self._on_name_committed)
        self._inspector.loop_settings_committed.connect(self._on_loop_settings_committed)
        self._inspector.start_committed.connect(self._on_inspector_start_committed)
        self._inspector.end_committed.connect(self._on_inspector_end_committed)
        # These two were never connected, so Tags/Notes typed in the Inspector were
        # silently discarded.
        self._inspector.tags_committed.connect(self._on_tags_committed)
        self._inspector.notes_committed.connect(self._on_notes_committed)
        self._inspector.edit_tags_requested.connect(self.edit_tags)
        self._inspector.apply_requested.connect(self._on_apply_new_bookmark)
        self._inspector.draft_range_edited.connect(self._on_draft_range_edited)
        # Undo / Redo beside Apply: the same stack as the Edit menu.
        undo, redo = self._inspector.undo_button, self._inspector.redo_button
        undo.clicked.connect(self._undo_stack.undo)
        redo.clicked.connect(self._undo_stack.redo)
        # Bound to the tab's own methods (not lambdas): the undo stack outlives the window,
        # and Qt drops these connections when the tab goes.
        self._undo_stack.canUndoChanged.connect(self._inspector.set_can_undo)
        self._undo_stack.canRedoChanged.connect(self._inspector.set_can_redo)
        self._undo_stack.undoTextChanged.connect(self._inspector.set_undo_text)
        self._undo_stack.redoTextChanged.connect(self._inspector.set_redo_text)
        self._inspector.set_can_undo(self._undo_stack.canUndo())
        self._inspector.set_can_redo(self._undo_stack.canRedo())

        self._playlist_panel.launch_vlc_requested.connect(self.launch_vlc_requested.emit)
        self._playlist_panel.quit_requested.connect(self.quit_requested.emit)
        self._volume.volume_changed.connect(self.volume_requested.emit)
        self._volume.volume_changed.connect(lambda level: self._transport.set_volume_percent(level_to_percent(level)))
        self._volume_eq.equalizer_changed.connect(self.equalizer_requested.emit)
        self._volume_eq.max_requested.connect(self.volume_max_requested.emit)
        self._volume_eq.mute_requested.connect(self.volume_mute_requested.emit)
        self._volume_eq.reset_requested.connect(self.volume_reset_requested.emit)
        self._volume_eq.normalize_requested.connect(self.volume_normalize_requested.emit)
        self._volume_eq.levels_changed.connect(self.volume_levels_changed.emit)
        self._volume_eq.ramp_times_changed.connect(self.volume_ramp_times_changed.emit)
        self._volume_eq.glide_ms_changed.connect(self.equalizer_glide_ms_changed.emit)
        tempo = self._volume_eq.tempo_panel
        tempo.fader_moved.connect(self.tempo_changed.emit)
        tempo.detected_bpm_corrected.connect(self.detected_bpm_corrected.emit)
        tempo.align_skew_requested.connect(self.tempo_align_skew_requested.emit)
        tempo.increase_requested.connect(self.tempo_increase_requested.emit)
        tempo.reset_requested.connect(self.tempo_reset_requested.emit)
        tempo.lower_requested.connect(self.tempo_lower_requested.emit)
        tempo.buttons_changed.connect(self.tempo_buttons_changed.emit)
        tempo.switched.connect(self.tempo_switched.emit)
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

    def show_volume_levels(self, normalize_percent: int, reset_percent: int) -> None:
        """Saved Normalize/Reset levels (no volume_levels_changed)."""
        self._volume_eq.set_levels(normalize_percent, reset_percent)

    def volume_levels(self) -> tuple[int, int]:
        return self._volume_eq.levels()

    def show_equalizer_glide_ms(self, value_ms: int) -> None:
        """The saved preset glide time (no equalizer_glide_ms_changed)."""
        self._volume_eq.set_glide_ms(value_ms)

    def equalizer_glide_ms(self) -> int:
        return self._volume_eq.glide_ms()

    def set_equalizer_glide_condition(self, allowed) -> None:  # noqa: ANN001 - Callable[[], bool]
        """When a preset may glide rather than jump (Application: while something plays)."""
        self._volume_eq.set_glide_condition(allowed)

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
        settings.set_panel_tab("view", self._view_tabs.currentIndex())
        settings.set_header_state("bookmarks", self._bookmark_panel.header_state(),
                                  columns=self._bookmark_panel.column_count())
        settings.set_header_state("source_playlist", self._playlist_panel.header_state())

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
        view_tab = settings.panel_tab("view")
        if 0 <= view_tab < self._view_tabs.count():
            self._view_tabs.setCurrentIndex(view_tab)
        columns = settings.header_state("bookmarks")
        if isinstance(columns, QByteArray) and not columns.isEmpty():
            saved_columns = settings.header_columns("bookmarks")
            if saved_columns is None:
                self._bookmark_panel.restore_header_state(columns)  # 0.7.0-0.8.0: 9 columns
            else:
                self._bookmark_panel.restore_header_state(columns, saved_columns=saved_columns)
        # (Saved under a new name in 0.10.0: Title and Bookmarks only is the new default,
        # so layouts saved before it are left behind once.)
        playlist_columns = settings.header_state("source_playlist")
        if isinstance(playlist_columns, QByteArray) and not playlist_columns.isEmpty():
            self._playlist_panel.restore_header_state(playlist_columns)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Closing the window is quitting too: keep what is being typed.
        self.commit_pending_edits()
        super().closeEvent(event)

    def show_playback(self, time_us: int, duration_us: int | None, current_item_id: int | None,
                      *, is_playing: bool) -> None:
        self._transport.set_time(time_us, duration_us)
        self._playlist_panel.set_current_playing(current_item_id, is_playing=is_playing)

    def show_bookmark_playback(self, bookmark_id: UUID | None, song_vlc_id: int | None, state: str | None) -> None:
        """The bookmark playing ("playing": green) or played last ("done": yellow), on all
        three places it shows: its song in the playlist, its area on the waveform, its row
        in the bookmark list. None clears them."""
        self._waveform_scene.set_bookmark_playback(bookmark_id, state)
        self._bookmark_panel.set_bookmark_playback(bookmark_id, state)
        self._playlist_panel.set_bookmark_song(song_vlc_id, state)

    def show_bookmark_crossing(self, bookmark_ids: frozenset[UUID]) -> None:
        """The bookmarks the playhead is crossing while a song plays (orange in the list)."""
        self._bookmark_panel.set_crossing(bookmark_ids)

    def show_tempo(self, detected: float | None, skew: float, change: float) -> None:
        """The BPM panel: the song's detected BPM (None: not detected), where the fader's
        middle is (the skew) and what plays (the change), both from the detected BPM."""
        self._volume_eq.tempo_panel.show_tempo(detected, skew, change)

    def show_tempo_buttons(self, buttons) -> None:  # noqa: ANN001 - TempoButtons
        self._volume_eq.tempo_panel.set_buttons(buttons)

    def show_tempo_enabled(self, enabled: bool) -> None:
        self._volume_eq.tempo_panel.set_tempo_enabled(enabled)

    def show_selection_playback(self, state: str | None) -> None:
        """The waveform's selection looping ("playing": green) or looped ("done":
        yellow); None: neither."""
        self._waveform_scene.set_selection_playback(state)

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

    # -- the BM playback view --

    def show_bookmark_tracks(self, previous: TrackBookmark | None, current: TrackBookmark | None,
                             following: TrackBookmark | None) -> None:
        self._bookmark_tracks.show_tracks(previous, current, following)

    def set_bookmark_tracks_playhead(self, media_id: UUID | None, time_us: int | None) -> None:
        self._bookmark_tracks.set_playhead(media_id, time_us)

    def selection_box_marking(self) -> bool:
        """The selection readout in orange: a new bookmark being marked out."""
        return bool(self._selection_bar.styleSheet())

    def show_view_tab(self, name: str) -> None:
        """"bookmarking" or "tracks" (the BM playback view)."""
        self._view_tabs.setCurrentIndex({"bookmarking": 0, "tracks": 1}[name])

    def clear_playlist_name(self) -> None:
        """Another player: its playlist isn't known yet."""
        self._playlist_panel.set_playlist_name(None)

    def set_context(self, *, playlist_name: str, track_name: str, playlist_id: UUID | None,
                     media_id: UUID | None, bookmark_count: int, duration_us: int = 0) -> None:
        self._current_playlist_id = playlist_id
        self._current_media_id = media_id
        self._context_names = (playlist_name, track_name)
        self._show_breadcrumb(bookmark_count)
        self._playlist_panel.set_playlist_name(playlist_name if playlist_id is not None else None)
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
        """Drives both the connection indicator (PlaylistPanel, above its tab)
        and the transport buttons'
        enabled state (TransportBar) together, so callers have one place to report
        connection changes instead of reaching into two widgets.
        """
        self._transport.set_transport_enabled(connected)
        self._playlist_panel.set_connected(connected)

    # -- handlers: selection bar --

    def _on_selection_changed(self, selection: object) -> None:
        from bookmark_studio.domain.selection import Selection

        # Orange while a new bookmark is being marked out (0.10.0): Bookmark selection (or
        # Apply) makes it and clears the selection -- the box is back to normal then.
        self._selection_bar.setStyleSheet(MARKING_STYLE if isinstance(selection, Selection) else "")
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
        # A selection is a new bookmark in the making: the Bookmark Studio tab becomes
        # its form (Apply saves it). Without one, the form goes away.
        if isinstance(selection, Selection):
            self._show_new_bookmark_form(selection)
        elif self._inspector.is_drafting():
            self._inspector.end_draft()
        else:
            self._inspector.show_selection(None)

    def _on_selection_preview_changed(self, start_us: int, end_us: int) -> None:
        """Live readout while dragging one of SelectionItem's resize handles: fires
        continuously during the drag, unlike
        _on_selection_changed (which only fires once the drag settles)."""
        from bookmark_studio.domain.selection import Selection

        self._show_selection_range(start_us, end_us)
        self._show_new_bookmark_form(Selection(start_us=start_us, end_us=end_us))

    def _show_new_bookmark_form(self, selection) -> None:  # noqa: ANN001 - Selection
        """The Bookmark Studio tab as the form for a new bookmark over `selection`. A
        bookmark shown there is left first -- its changes are already saved, and what is
        still being typed is saved now -- so nothing is lost by switching."""
        if self._inspector.is_drafting():
            self._inspector.show_selection(selection)  # Start/End follow the selection
            return
        if self._inspector.current_bookmark() is not None:
            self._inspector.commit_pending()
        self._inspector.begin_draft(selection)

    def _on_draft_range_edited(self, start_us: int, end_us: int) -> None:
        """Start/End typed into the new-bookmark form move the waveform's selection (and
        are refused, back to the selection, if they make no range)."""
        from bookmark_studio.domain.selection import Selection

        duration_us = self._waveform_scene._duration_us
        if 0 <= start_us < end_us and (not duration_us or end_us <= duration_us):
            self._waveform_scene.set_selection(Selection(start_us=start_us, end_us=end_us))
        else:
            self._inspector.show_selection(self._waveform_scene.selection())

    def _on_apply_new_bookmark(self) -> None:
        """Apply (or Enter in the name, or Bookmark selection): saves the new bookmark set
        up in the Bookmark Studio tab, over the waveform's selection."""
        values = self._inspector.draft_values()
        selection = self._waveform_scene.selection()
        if values is None or selection is None or self._current_media_id is None:
            return
        loop_enabled = bool(values["loop_enabled"])
        bookmark = Bookmark(
            id=uuid4(),
            playlist_id=self._current_playlist_id,
            media_id=self._current_media_id,
            scope=BookmarkScope.PLAYLIST_MEDIA if self._current_playlist_id else BookmarkScope.GLOBAL_MEDIA,
            lane_id=None,
            bookmark_type=BookmarkType.SEGMENT,
            name=values["name"],
            start_us=selection.start_us,
            end_us=selection.end_us,
            loop_enabled=loop_enabled,
            repeat_count=values["repeat_count"],
            loop_gap_ms=values["loop_gap_ms"],
            completion_action=values["completion_action"],
            notes=values["notes"],
            tags=tuple(values["tags"]),
            fade_in_ms=values["fade_in_ms"],
            fade_out_ms=values["fade_out_ms"],
        )
        self._create_bookmark_and_focus_name(bookmark)
        self.selection_bookmarked.emit(bookmark.id)
        self._waveform_scene.clear_selection()
        self._flash_saved(bookmark.id)

    def _flash_saved(self, bookmark_id: UUID) -> None:
        """The orange flash: the bookmark's row in the list, and the name field when the
        Bookmark Studio tab shows it -- the change was saved."""
        self._bookmark_panel.flash_bookmark(bookmark_id)
        current = self._current_inspected_bookmark()
        if current is not None and current.id == bookmark_id:
            self._inspector.flash_saved()

    def _show_selection_range(self, start_us: int, end_us: int) -> None:
        from bookmark_studio.domain.timecode import format_timecode

        self._selection_start_label.setText(format_timecode(start_us))
        self._selection_end_label.setText(format_timecode(end_us))
        self._selection_duration_label.setText(f"length {format_timecode(max(0, end_us - start_us))}")

    def _on_bookmark_selection_clicked(self) -> None:
        selection = self._waveform_scene.selection()
        if selection is None or self._current_media_id is None:
            return
        if self._inspector.is_drafting():
            # The same as Apply: what was set up in the Bookmark Studio tab.
            self._on_apply_new_bookmark()
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
        self.selection_bookmarked.emit(bookmark.id)
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
        # New bookmarks go to the top of the list (0.9.0; by start time among the rest
        # before), above whatever order the user has set.
        bookmark = replace(bookmark, sort_index=self._top_sort_index())
        self._push(CreateBookmarkCommand(self._bookmark_repository, bookmark))
        self._refresh_bookmarks()
        # spec #46: inline name editor appears immediately after creation, no modal.
        self._load_bookmark_into_inspector(bookmark)
        self._bookmark_panel.select_bookmark(bookmark.id)
        self._inspector._name_edit.setFocus()
        self._inspector._name_edit.selectAll()

    def _on_duplicate_requested(self, bookmark_ids: list) -> None:
        """Duplicate: copies of the selected bookmarks -- everything the same but the name,
        a new random one no other bookmark has -- at the top of the list, in the order they
        were in; one undo step for them all. The copies are selected (the Bookmark Studio
        tab shows a single one) and flash orange: saved."""
        originals = [b for b in (self._bookmark_repository.get(i) for i in bookmark_ids) if b is not None]
        if not originals:
            return
        taken = {b.name for b in self._bookmark_repository.list_all()}
        top = self._top_sort_index()
        copies = []
        for position, original in enumerate(originals):
            name = unique_bookmark_name(taken)
            taken.add(name)
            copies.append(replace(original, id=uuid4(), name=name,
                                  sort_index=top - len(originals) + 1 + position))
        self._pushing = True
        try:
            plural = "s" if len(copies) != 1 else ""
            self._undo_stack.beginMacro(f"Duplicate {len(copies)} bookmark{plural}")
            for copy in copies:
                self._undo_stack.push(CreateBookmarkCommand(self._bookmark_repository, copy))
            self._undo_stack.endMacro()
        finally:
            self._pushing = False
        self._refresh_bookmarks()
        self._bookmark_panel.select_bookmarks({copy.id for copy in copies})
        if len(copies) == 1:
            self._load_bookmark_into_inspector(copies[0])
        for copy in copies:
            self._bookmark_panel.flash_bookmark(copy.id)

    def _top_sort_index(self) -> int:
        """A sort_index above every bookmark in the list (the list is sorted by it)."""
        if self._current_playlist_id is None:
            return 0
        listing = self._bookmark_repository.list_for_playlist(self._current_playlist_id)
        return min((b.sort_index for b in listing), default=1) - 1

    def selected_bookmark_id(self) -> UUID | None:
        return self._bookmark_panel.selected_bookmark_id()

    def open_extract_dialog(self, jobs, ffmpeg_path: str | None, settings) -> ExtractDialog:  # noqa: ANN001
        """The Extract audio window for `jobs` (media.extract.ExtractJob), over this one."""
        dialog = ExtractDialog(jobs, ffmpeg_path, settings, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._extract_dialog = dialog
        dialog.open()
        return dialog

    def end_session(self) -> None:
        """The session is over (Application.stop): undo/redo can't change anything any
        more. Without this, the undo stack -- clearing itself as the window is destroyed --
        redrew a window whose waveform was already gone (an error at every quit)."""
        try:
            self._undo_stack.indexChanged.disconnect(self._on_undo_index_changed)
        except (RuntimeError, TypeError):
            pass  # already unhooked

    def show_bookmark_in_studio(self, bookmark_id: UUID) -> None:
        """Selects the bookmark in the list, so the Bookmark Studio tab shows its settings
        (After loop: Next/Previous Bookmark moved on to it). Not while a new bookmark is
        being set up there -- that form isn't saved yet."""
        if self._inspector.is_drafting():
            return
        self._bookmark_panel.select_bookmark(bookmark_id)

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
        self._flash_saved(bookmark_id)

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
        self._flash_saved(bookmark_id)

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
        self._flash_saved(bookmark.id)

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
        self._flash_saved(bookmark.id)

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
        self._flash_saved(bookmark.id)

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
        self._flash_saved(bookmark.id)

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

    def _on_bookmark_panel_completion_edited(self, bookmark_id: UUID, action: CompletionAction) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        self._push_bookmark_loop_change(
            bookmark, loop_enabled=bookmark.loop_enabled, repeat_count=bookmark.repeat_count,
            gap_ms=bookmark.loop_gap_ms, completion_action=action,
            fade_in_ms=bookmark.fade_in_ms, fade_out_ms=bookmark.fade_out_ms,
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
        self._flash_saved(bookmark.id)

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
        self._flash_saved(bookmark.id)

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
        self._flash_saved(bookmark.id)

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

    def about_text(self) -> str:
        from bookmark_studio import __version__

        return (
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>Playlist-aware visual bookmarking and looping for VLC Media Player.</p>"
            f"<p><b>Owner:</b> {OWNER}<br><b>Developed by:</b> {DEVELOPERS}</p>"
            f'<p><b>GitHub:</b> <a href="{REPOSITORY_URL}">{REPOSITORY_URL}</a></p>'
            f"<p>{COPYRIGHT}<br>{LICENSE_SUMMARY}</p>"
        )

    def _on_show_about(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        box.setIconPixmap(logo_pixmap(96))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(self.about_text())
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()

    def _on_show_license(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{APP_NAME} license")
        dialog.resize(640, 520)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(dialog)
        text.setReadOnly(True)
        text.setPlainText(license_text())
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._license_dialog = dialog
        dialog.open()
