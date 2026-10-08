"""0.10.0 (feedback on its build): no logo row; the breadcrumb in the Bookmarking tab; the
playlist's name and the connection in the Source Playlist tab; Title and Bookmarks only by
default there; Bookmark selection under Fit; the selection readout orange while a new
bookmark is marked out; an equalizer that narrows instead of scrolling."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QPoint, QSettings
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea

from bookmark_studio.domain.selection import Selection
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.playlist_panel import COLUMNS, PlaylistPanel
from bookmark_studio.ui.qt_helpers import SELECTED_TAB_COLOR
from bookmark_studio.ui.volume_eq_panel import VolumeEqPanel
from tests.unit.test_050_ui import _make_app


def _at(widget, window) -> QPoint:  # noqa: ANN001
    return widget.mapTo(window, QPoint(0, 0))


def _shown(app, width: int = 1400, height: int = 900):  # noqa: ANN001, ANN202
    window = app.window
    window.resize(width, height)
    window.show()
    return window


# -- 1. no logo under the menu; 2. the breadcrumb in the Bookmarking tab --


def test_no_logo_row_and_the_breadcrumb_is_above_the_waveform_in_the_bookmarking_tab(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = _shown(app)
    qtbot.waitExposed(window)
    logos = [label for label in window.findChildren(QLabel) if label.pixmap() is not None and not label.pixmap().isNull()]
    assert logos == [] and not hasattr(window, "_logo")
    assert not window.windowIcon().isNull()  # the window still carries it
    bookmarking = window._view_tabs.widget(0)
    assert bookmarking.isAncestorOf(window._breadcrumb)
    qtbot.waitUntil(lambda: "Unsaved VLC Playlist" in window._breadcrumb.text(), timeout=3000)
    crumb, view = _at(window._breadcrumb, window), _at(window._waveform_view, window)
    assert crumb.y() + window._breadcrumb.height() <= view.y() + 2  # above the waveform
    # Nothing between the menu and the tabs: both tab bars at the very top.
    playlist_tabs = window._playlist_panel._tabs
    assert abs(_at(window._view_tabs, window).y() - _at(playlist_tabs, window).y()) <= 10
    assert _at(window._view_tabs, window).y() <= window.menuBar().height() + 12  # right under the menu


# -- 3. the playlist's name and the connection in the Source Playlist tab --


def test_the_playlists_name_and_the_connection_are_in_the_source_playlist_tab(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = _shown(app)
    qtbot.waitExposed(window)
    panel = window._playlist_panel
    page = panel._tabs.widget(0)
    order = [panel._playlist_name, panel._connection_label, panel._launch_vlc_button, panel._follow_checkbox,
             panel._filter_edit, panel._tree]
    for widget in order:
        assert page.isAncestorOf(widget)
    ys = [_at(widget, window).y() for widget in order]
    assert ys == sorted(ys) and len(set(ys)) == len(ys)


# -- 4. Title and Bookmarks only, by default --


def test_the_source_playlist_shows_title_and_bookmarks_by_default(qtbot) -> None:
    panel = PlaylistPanel()
    qtbot.addWidget(panel)
    assert panel.shown_columns() == ["Title", "Bookmarks"]
    menu = panel.column_menu()
    ticks = {action.text(): action.isChecked() for action in menu.actions() if action.isCheckable()}
    assert ticks == {"Title": True, "Artist": False, "Duration": False, "Bookmarks": True, "Status": False}


def test_restore_and_the_arrange_windows_defaults_are_title_and_bookmarks(qtbot, monkeypatch) -> None:
    from bookmark_studio.ui.dialogs.columns_dialog import ColumnsDialog

    panel = PlaylistPanel()
    qtbot.addWidget(panel)
    for name in COLUMNS:
        panel.set_column_shown(COLUMNS.index(name), True)
    assert panel.shown_columns() == COLUMNS
    panel._columns.restore_defaults()
    assert panel.shown_columns() == ["Title", "Bookmarks"]

    def arrange(dialog: ColumnsDialog) -> int:
        dialog.restore_defaults()  # its Defaults button
        assert [shown for _logical, shown in dialog.layout_chosen()] == [True, False, False, True, False]
        return 1

    monkeypatch.setattr(ColumnsDialog, "exec", arrange)
    panel.set_column_shown(COLUMNS.index("Status"), True)
    panel._columns.arrange()
    assert panel.shown_columns() == ["Title", "Bookmarks"]


def test_a_layout_saved_before_the_new_default_is_left_behind_once(qtbot, tmp_path) -> None:
    """Saved by the 0.9.0/0.10.0 builds with every column: the new default shows."""
    settings = SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    old = PlaylistPanel()
    qtbot.addWidget(old)
    for name in COLUMNS:
        old.set_column_shown(COLUMNS.index(name), True)
    settings.set_header_state("playlist", old.header_state())  # the earlier key
    app = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: None)
    assert app.window._playlist_panel.shown_columns() == ["Title", "Bookmarks"]
    assert isinstance(settings.header_state("playlist"), QByteArray)  # left alone, not deleted


# -- 5. Bookmark selection under Fit --


def test_bookmark_selection_under_fit_makes_the_bookmark(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = _shown(app)
    qtbot.waitExposed(window)
    button = window._bookmark_selection_beside_waveform
    assert button.text() == "Bookmark selection" and window._bookmarking_column.isAncestorOf(button)
    fit = window._zoom_fit_button
    assert _at(button, window).y() >= _at(fit, window).y() + fit.height()  # under Fit
    assert abs(_at(button, window).x() - _at(fit, window).x()) <= 2
    assert not button.isEnabled()  # as the Bookmark Studio one: only with a selection
    window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=2_500_000))
    assert button.isEnabled() and window._bookmark_selection_button.isEnabled()
    before = len(app._bookmark_repository.list_for_playlist(app.playlists.active_playlist_id))
    button.click()
    bookmarks = app._bookmark_repository.list_for_playlist(app.playlists.active_playlist_id)
    assert len(bookmarks) == before + 1
    assert {(b.start_us, b.end_us) for b in bookmarks} >= {(1_000_000, 2_500_000)}
    assert window._waveform_scene.selection() is None and not button.isEnabled()


# -- 6. orange while a new bookmark is marked out --


def test_the_selection_box_is_orange_while_marking_and_back_once_bookmarked(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = _shown(app)
    qtbot.waitExposed(window)
    box = window._selection_bar

    def colour():  # noqa: ANN202
        QApplication.processEvents()
        return box.grab().toImage().pixelColor(box.width() // 2, box.height() - 4)

    normal = colour()
    assert not window.selection_box_marking()
    window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=2_000_000))
    assert window.selection_box_marking()
    orange = colour()
    assert orange.name() == SELECTED_TAB_COLOR and orange != normal
    window._bookmark_selection_beside_waveform.click()  # bookmarked: back to normal
    assert not window.selection_box_marking() and colour() == normal
    window._waveform_scene.set_selection(Selection(start_us=3_000_000, end_us=4_000_000))
    assert window.selection_box_marking()
    window._clear_selection_button.click()  # given up: back to normal too
    assert not window.selection_box_marking()


def test_editing_an_existing_bookmark_does_not_turn_it_orange(qtbot, tmp_path) -> None:
    from tests.unit.test_050_ui import _bookmark

    app = _make_app(qtbot, tmp_path)
    window = _shown(app)
    bookmark = _bookmark(app, start_us=1_000_000, end_us=2_000_000)
    window._on_bookmark_move_finished(bookmark.id, 1_500_000, 2_500_000)  # a drag on the waveform
    assert not window.selection_box_marking()


# -- 7. the equalizer narrows instead of scrolling --


def test_the_equalizer_narrows_instead_of_scrolling(qtbot) -> None:
    panel = VolumeEqPanel()
    qtbot.addWidget(panel)
    # Its controls in one row (542 px on Windows) no longer set the equalizer's minimum
    # (on Windows the whole tab went from 1028 to 796 px): the faders do.
    eq = panel.layout().itemAt(panel.layout().count() - 1).layout()
    rack = eq.itemAt(1).layout()
    assert panel._eq_header.minimumSize().width() < panel._eq_header.sizeHint().width()
    assert eq.minimumSize().width() == max(rack.minimumSize().width(), panel._eq_header.minimumSize().width())
    assert eq.minimumSize().width() < panel._eq_header.sizeHint().width()
    panel.resize(panel.sizeHint().width() + 50, 520)
    panel.show()
    qtbot.waitExposed(panel)
    QApplication.processEvents()
    def span() -> int:  # the faders' extent, Pre's left edge to 16k's right edge
        left = panel._preamp.mapTo(panel, QPoint(0, 0)).x()
        last = panel._bands[-1]
        return last.mapTo(panel, QPoint(0, 0)).x() + last.width() - left

    wide_span = span()
    assert panel._eq_header.rows() == 1  # room: one row, as before
    panel.resize(panel.minimumSizeHint().width() + 20, 520)
    QApplication.processEvents()
    assert panel._eq_header.rows() >= 2  # narrow: the controls wrap...
    assert span() < wide_span * 0.85  # ...and the faders close up (and narrow, below ~32 px a column)
    header_bottom = max(w.mapTo(panel, QPoint(0, 0)).y() + w.height()
                        for w in (panel._preset_combo, panel._glide_ms, panel._reset_button))
    assert header_bottom <= panel._band_values[0].mapTo(panel, QPoint(0, 0)).y()  # nothing overlaps
    for fader in panel._bands:
        assert fader.width() >= 24 and fader.isVisible()


def test_the_volume_tab_needs_no_sideways_scrolling_down_to_its_new_minimum(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = _shown(app, 1700, 900)
    qtbot.waitExposed(window)
    window.show_side_tab("volume")
    QApplication.processEvents()
    area = next(a for a in window._side_tabs.findChildren(QScrollArea) if a.widget() is window._volume_eq)
    tab_width = window._volume_eq.minimumSizeHint().width() + 30  # just above its new minimum
    assert tab_width < window._volume_eq.sizeHint().width()  # narrower than it would like
    window._splitters["bottom"].setSizes([window.width() - tab_width, tab_width])
    QApplication.processEvents()
    qtbot.waitUntil(lambda: not area.horizontalScrollBar().isVisible(), timeout=3000)
    assert window._volume_eq._bands[0].isVisible()
