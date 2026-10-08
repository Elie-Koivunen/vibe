"""0.9.0 (requested on its build): the Source Playlist tab and its column menu, in-place
dropdowns wide enough to read, the BPM panel (first called Tempo) with its on/off switch,
BPM Skew following the fader, and a bookmark moved on to playing at its detected BPM."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QStyleFactory

from bookmark_studio.domain.enums import CompletionAction
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.bookmark_panel import AFTER_LOOP_COLUMN
from bookmark_studio.ui.playlist_panel import COLUMNS, PlaylistPanel
from bookmark_studio.ui.qt_helpers import SELECTED_TAB_COLOR
from tests.unit.test_050_ui import _bookmark, _make_app
from tests.unit.test_090_after_loop_and_fades import _playing_bookmark, _two_song_app


def _settings(tmp_path) -> SettingsService:
    return SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))


# -- the Source Playlist tab --


def test_the_source_playlist_tab_holds_follow_the_filter_and_the_playlist(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    panel = window._playlist_panel
    tabs = panel._tabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Source Playlist"]
    assert SELECTED_TAB_COLOR in tabs.tabBar().styleSheet()  # orange, like the other tabs
    page = tabs.widget(0)
    for widget in (panel._follow_checkbox, panel._filter_edit, panel._tree):
        assert page.isAncestorOf(widget)
    # (0.10.0 moved Launch VLC..., Quit, the playlist's name and the connection into the
    # tab too: test_0100_source_playlist.py.)

    def y(widget) -> int:  # noqa: ANN001
        return widget.mapTo(window, QPoint(0, 0)).y()

    assert y(panel._follow_checkbox) < y(panel._filter_edit) < y(panel._tree)
    assert panel._tree.topLevelItemCount() == 2  # the playlist, there


def test_the_playlists_column_titles_have_the_column_menu(qtbot) -> None:
    panel = PlaylistPanel()
    qtbot.addWidget(panel)
    header = panel._tree.header()
    assert header.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    menu = panel.column_menu(COLUMNS.index("Artist"))
    texts = [action.text() for action in menu.actions() if action.text()]
    assert texts[:len(COLUMNS)] == COLUMNS
    assert {"Move “Artist” Left", "Move “Artist” Right", "Arrange Columns...", "Restore Default Columns"} <= set(texts)
    menu.actions()[COLUMNS.index("Artist")].toggle()  # ticked: shown (0.10.0: hidden by default)
    assert panel.shown_columns() == ["Title", "Artist", "Bookmarks"]
    panel.set_column_shown(COLUMNS.index("Status"), True)
    panel.move_column(COLUMNS.index("Status"), -1)
    assert panel.shown_columns() == ["Title", "Artist", "Status", "Bookmarks"]
    restore = [a for a in panel.column_menu().actions() if a.text() == "Restore Default Columns"][0]
    restore.trigger()
    assert panel.shown_columns() == ["Title", "Bookmarks"]


def test_the_playlists_columns_are_saved_with_the_window_layout(qtbot, tmp_path) -> None:
    settings = _settings(tmp_path)
    first = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: None)
    panel = first.window._playlist_panel
    panel.set_column_shown(COLUMNS.index("Duration"), True)
    panel.move_column(COLUMNS.index("Bookmarks"), -1)
    shown = panel.shown_columns()
    assert shown == ["Title", "Bookmarks", "Duration"]
    first.quit()
    first.stop()
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _make_app(qtbot, second_dir, settings=settings, quit_app=lambda: None)
    QApplication.processEvents()
    assert second.window._playlist_panel.shown_columns() == shown


# -- the bookmark list's dropdowns, wide enough to read --


@pytest.fixture()
def windows_style():  # noqa: ANN201
    """The Windows look the app has there (windows11 / windowsvista; elsewhere Qt's plain
    Windows style): a dropdown's list is as wide as the dropdown. (Fusion, the offscreen
    default, widens it by itself.)"""
    previous = QApplication.style().name()
    keys = [key.lower() for key in QStyleFactory.keys()]
    QApplication.setStyle(QStyleFactory.create(next(k for k in ("windows11", "windowsvista", "windows") if k in keys)))
    yield
    QApplication.setStyle(QStyleFactory.create(previous))


def test_an_in_place_dropdown_shows_its_choices_in_full(qtbot, tmp_path, windows_style) -> None:
    """Its list was as narrow as the column (reported on the 0.9.0 build)."""
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    _bookmark(app, end_us=3_000_000)
    tree = window._bookmark_panel._tree
    tree.header().resizeSection(AFTER_LOOP_COLUMN, 30)  # narrow
    tree.setCurrentItem(tree.topLevelItem(0), AFTER_LOOP_COLUMN)  # a click: the dropdown opens
    qtbot.waitUntil(lambda: isinstance(tree.indexWidget(tree.currentIndex()), QComboBox), timeout=3000)
    combo = tree.indexWidget(tree.currentIndex())
    try:
        widest = max(combo.fontMetrics().horizontalAdvance(combo.itemText(i)) for i in range(combo.count()))
        assert combo.width() < widest  # the editor itself is as narrow as the column...
        qtbot.waitUntil(lambda: combo.view().isVisible(), timeout=3000)
        assert combo.view().width() >= widest + 16  # ...its list isn't
    finally:
        combo.hidePopup()


# -- the BPM panel --


def _bpm(app):  # noqa: ANN001, ANN202
    return app.window._volume_eq.tempo_panel


def test_the_bpm_panel_is_called_bpm_and_starts_switched_off_and_greyed_out(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    app.window.show()
    qtbot.waitExposed(app.window)
    app.window.show_side_tab("volume")
    panel = _bpm(app)
    texts = [label.text() for label in panel.findChildren(QLabel)]
    assert "BPM" in texts and "TEMPO" not in texts
    assert not panel.is_tempo_enabled() and panel.switch.isEnabled()
    for widget in (panel.fader, panel.align_skew_button, panel.increase_button, panel.reset_button,
                   panel.lower_button, panel._step, panel._glide_ms, panel._detected_value, panel._skew_value):
        assert not widget.isEnabled(), widget
    switch, title = panel.switch, [label for label in panel.findChildren(QLabel) if label.text() == "BPM"][0]
    assert switch.mapTo(panel, QPoint(0, 0)).x() > title.mapTo(panel, QPoint(0, 0)).x()  # beside the title
    assert abs(switch.mapTo(panel, QPoint(0, switch.height() // 2)).y()
               - title.mapTo(panel, QPoint(0, title.height() // 2)).y()) <= 3
    middle = panel.fader.rect().center()
    dimmed = panel.fader.grab().toImage().pixelColor(middle.x() // 2, middle.y()).lightness()
    panel.switch.click()
    assert panel.is_tempo_enabled()
    assert all(w.isEnabled() for w in (panel.fader, panel.align_skew_button, panel.reset_button, panel._step))
    dark = panel.fader.grab().toImage().pixelColor(middle.x() // 2, middle.y()).lightness()
    assert dimmed > dark + 40  # the dark fader greyed out while off


def test_switched_off_the_song_plays_at_its_own_tempo_and_on_again_at_the_tempo_set(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    panel = _bpm(app)
    app._estimated_bpm[app._current_media_id] = 100.0
    panel.switch.click()  # on
    panel.fader._set_by_user(20)
    qtbot.waitUntil(lambda: adapter.get_status().rate == pytest.approx(1.2), timeout=3000)
    panel.switch.click()  # off
    qtbot.waitUntil(lambda: adapter.get_status().rate == 1.0, timeout=3000)
    assert app._clock.rate == 1.0
    assert panel.readings() == ("+20", "120", "100") and not panel.fader.isEnabled()  # kept, greyed out
    panel.switch.click()  # on again
    qtbot.waitUntil(lambda: adapter.get_status().rate == pytest.approx(1.2), timeout=3000)


def test_the_switch_is_remembered(qtbot, tmp_path) -> None:
    settings = _settings(tmp_path)
    assert not settings.tempo_enabled()  # off the first time
    first = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: None)
    _bpm(first).switch.click()
    assert settings.tempo_enabled()
    first.stop()
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _make_app(qtbot, second_dir, settings=settings, quit_app=lambda: None)
    assert _bpm(second).is_tempo_enabled() and _bpm(second).fader.isEnabled()


def test_a_bookmark_moved_on_to_plays_at_its_songs_detected_bpm(qtbot, tmp_path) -> None:
    """After loop Next Bookmark, and ⏭ / ⏮: the BPM Skew back to 0 (requested on the
    0.9.0 build)."""
    app, item_a, media_a, item_b, media_b = _two_song_app(qtbot, tmp_path)
    adapter = app.session.adapter
    panel = _bpm(app)
    looped = _bookmark(app, media_id=media_a.id, name="looped", start_us=1_000_000, end_us=1_200_000,
                       loop_enabled=True, repeat_count=1, completion_action=CompletionAction.NEXT_BOOKMARK)
    after = _bookmark(app, media_id=media_b.id, name="after", start_us=500_000, end_us=900_000)
    app._on_bookmark_reorder_requested([looped.id, after.id])
    app._estimated_bpm[media_a.id] = 100.0
    app._estimated_bpm[media_b.id] = 140.0
    panel.switch.click()
    app._on_play_bookmark_requested(looped.id)
    panel.fader._set_by_user(20)
    panel.align_skew_button.click()
    panel.fader._set_by_user(10)  # 30 off, the fader's middle 20 off
    assert app._tempo_change_bpm == 30 and app._tempo_skew_bpm == 20
    qtbot.waitUntil(lambda: _playing_bookmark(app) == after.id, timeout=5000)  # After loop: Next Bookmark
    assert app._tempo_change_bpm == 0 and app._tempo_skew_bpm == 0
    assert panel.readings()[0] == "0" and not panel.is_shown_skewed() and not panel.is_shown_aligned()
    qtbot.waitUntil(lambda: adapter.get_status().rate == 1.0, timeout=3000)
    panel.fader._set_by_user(-15)
    app.window._transport.previous_bookmark_button.click()  # ⏮
    qtbot.waitUntil(lambda: _playing_bookmark(app) == looped.id, timeout=3000)
    assert app._tempo_change_bpm == 0
    qtbot.waitUntil(lambda: panel.readings() == ("0", "100", "100"), timeout=3000)  # song A's own


def test_a_bookmark_of_another_song_shows_and_plays_from_that_songs_bpm(qtbot, tmp_path) -> None:
    """The app switching the player itself (a bookmark of another song) left the previous
    song's Detected BPM shown, and the change playing from it (found on the 0.9.0 build)."""
    app, item_a, media_a, item_b, media_b = _two_song_app(qtbot, tmp_path)
    adapter = app.session.adapter
    panel = _bpm(app)
    other = _bookmark(app, media_id=media_b.id, name="other", start_us=500_000, end_us=3_000_000,
                      loop_enabled=True)
    app._estimated_bpm[media_a.id] = 100.0
    app._estimated_bpm[media_b.id] = 140.0
    qtbot.waitUntil(lambda: app._tempo_media_id() == media_a.id, timeout=3000)
    app._show_tempo()
    panel.switch.click()
    panel.fader._set_by_user(10)
    assert panel.readings() == ("+10", "110", "100")
    app._on_play_bookmark_requested(other.id)  # a double-click on its row: the tempo stays
    qtbot.waitUntil(lambda: panel.readings() == ("+10", "150", "140"), timeout=3000)
    qtbot.waitUntil(lambda: adapter.get_status().rate == pytest.approx(150 / 140), timeout=3000)
