"""0.7.0 layout: the bookmark list in its own tab with its buttons stacked beside it, a
saved column order with Tags before Name, compact Bookmark-tab rows, the selection
readout above the tool groups, and Max / Reset / Mute in the Volume & EQ tab."""
from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QSettings
from PySide6.QtWidgets import QApplication, QWidget

from bookmark_studio.app.application import MAX_BUTTON_VOLUME
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.bookmark_panel import COLUMNS, TAGS_COLUMN, BookmarkPanel
from tests.unit.test_050_ui import _bookmark, _make_app


def _top_left(widget: QWidget, window: QWidget) -> QPoint:
    return widget.mapTo(window, QPoint(0, 0))


def _ini(tmp_path: Path) -> SettingsService:
    return SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))


def _shown(app):
    app.window.resize(1400, 900)
    app.window.show()
    QApplication.processEvents()
    return app.window


# -- the bookmark list --


def test_the_list_is_a_tab_with_its_buttons_stacked_towards_the_bookmark_tab(qtbot, tmp_path) -> None:
    window = _shown(_make_app(qtbot, tmp_path))
    panel = window._bookmark_panel
    assert panel._tabs.tabText(0) == "Bookmarks" and panel._tabs.widget(0) is panel._tree
    buttons = [panel._play_bookmark_button, panel._loop_bookmark_button, panel._delete_bookmark_button,
               panel._move_up_button, panel._move_down_button]
    xs = {_top_left(b, window).x() for b in buttons}
    ys = [_top_left(b, window).y() for b in buttons]
    assert len(xs) == 1 and ys == sorted(ys)  # one column, top to bottom
    column_x = xs.pop()
    tree_right = _top_left(panel._tree, window).x() + panel._tree.width()
    side_left = _top_left(window._side_tabs, window).x()
    assert tree_right <= column_x and column_x + buttons[0].width() <= side_left  # between the two tab panels


def test_tags_is_a_column_shown_before_name_and_columns_can_be_reordered(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    header = panel._tree.header()
    assert header.sectionsMovable()
    order = [COLUMNS[header.logicalIndex(v)] for v in range(header.count())]
    assert order[:3] == ["Song", "Tags", "Name"]


def test_the_column_order_and_widths_are_remembered(qtbot, tmp_path) -> None:
    settings = _ini(tmp_path)
    first = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: None)
    header = first.window._bookmark_panel._tree.header()
    header.moveSection(header.visualIndex(COLUMNS.index("Start")), 0)  # Start first
    header.resizeSection(TAGS_COLUMN, 222)
    first.quit()
    first.stop()
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _make_app(qtbot, second_dir, settings=settings, quit_app=lambda: None)
    _bookmark(second, tags=("drop",))  # a refresh must not refit a remembered width
    restored = second.window._bookmark_panel._tree.header()
    assert COLUMNS[restored.logicalIndex(0)] == "Start"
    assert restored.sectionSize(TAGS_COLUMN) == 222


def test_the_tags_column_shows_each_bookmarks_tags(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    _bookmark(app, tags=("peak", "drop"))
    row = app.window._bookmark_panel._tree.topLevelItem(0)
    assert row.text(TAGS_COLUMN) == "drop, peak"
    assert row.text(1) == "b"  # Name keeps its column number


# -- the Bookmark tab --


def test_loop_settings_and_gap_fades_each_share_one_row(qtbot, tmp_path) -> None:
    window = _shown(_make_app(qtbot, tmp_path))
    window.show_side_tab("bookmark")
    inspector = window._inspector
    QApplication.processEvents()

    def y(widget):
        return _top_left(widget, window).y() + widget.height() // 2

    first_row = [inspector._loop_checkbox, inspector._repeat_spin, inspector._completion_combo]
    second_row = [inspector._gap_spin, inspector._fade_in_spin, inspector._fade_out_spin]
    assert max(y(w) for w in first_row) - min(y(w) for w in first_row) <= 4
    assert max(y(w) for w in second_row) - min(y(w) for w in second_row) <= 4
    assert y(inspector._gap_spin) > y(inspector._loop_checkbox)  # the second row is below
    xs = [_top_left(w, window).x() for w in first_row]
    assert xs == sorted(xs)


# -- the selection readout --


def test_the_selection_readout_sits_above_the_view_group(qtbot, tmp_path) -> None:
    window = _shown(_make_app(qtbot, tmp_path))
    readout = window._selection_bar
    assert readout.parent() is window._tool_column
    bottom = _top_left(readout, window).y() + readout.height()
    assert bottom <= _top_left(window._zoom_out_button, window).y()


# -- Max / Reset / Mute --


def test_max_reset_and_mute_sit_between_the_fader_and_the_equalizer(qtbot) -> None:
    from bookmark_studio.ui.volume_eq_panel import VolumeEqPanel

    panel = VolumeEqPanel()
    qtbot.addWidget(panel)
    panel.resize(900, 320)
    panel.show()
    QApplication.processEvents()
    fader_right = _top_left(panel.volume_strip, panel).x() + panel.volume_strip.width()
    eq_left = _top_left(panel._preamp, panel).x()
    widgets = [panel._max_button, panel._max_ms, panel._normalize_button, panel._normalize_level,
               panel._volume_reset_button, panel._reset_level, panel._mute_ms, panel._mute_button]
    for widget in widgets:
        assert fader_right <= _top_left(widget, panel).x() < eq_left
    ys = [_top_left(w, panel).y() for w in widgets]
    assert ys == sorted(ys)  # Max on top, Reset between, Mute at the bottom
    assert panel._max_ms.suffix() == " ms" and panel._max_ms.specialValueText() == "At once"


def _playing_app(qtbot, tmp_path, **kwargs):
    app = _make_app(qtbot, tmp_path, **kwargs)
    adapter = app.session.adapter
    adapter.play()
    qtbot.waitUntil(lambda: app._last_playback_state == "playing", timeout=3000)
    return app, adapter


def test_mute_fades_the_volume_out_then_reset_brings_it_back(qtbot, tmp_path) -> None:
    """(0.7.0's Reset went back at once to the level from before; since 0.8.0 it glides
    to its own level -- test_080_*.)"""
    app, adapter = _playing_app(qtbot, tmp_path)
    adapter.set_volume(200)
    qtbot.waitUntil(lambda: app._loop_controller.target_volume == 200, timeout=3000)
    app.window.show_volume_ramp_times(800, 400)
    app.window._volume_eq._mute_ms.setValue(400)  # as typed: also saved
    app.window._volume_eq._mute_button.click()
    assert app._volume_ramp is not None  # fading, not instant
    qtbot.wait(150)
    assert 0 < adapter.get_status().volume < 200
    qtbot.waitUntil(lambda: adapter.get_status().volume == 0, timeout=3000)
    app.window._volume_eq._reset_level.setValue(78)  # 78 % = 200 on VLC's scale
    app.window._volume_eq._volume_reset_button.click()
    qtbot.waitUntil(lambda: adapter.get_status().volume == 200, timeout=3000)


def test_max_raises_to_100_percent_over_its_time(qtbot, tmp_path) -> None:
    app, adapter = _playing_app(qtbot, tmp_path)
    adapter.set_volume(64)
    qtbot.waitUntil(lambda: app._loop_controller.target_volume == 64, timeout=3000)
    app.window._volume_eq._max_ms.setValue(400)
    app.window._volume_eq._max_button.click()
    qtbot.wait(150)
    assert 64 < adapter.get_status().volume < MAX_BUTTON_VOLUME
    qtbot.waitUntil(lambda: adapter.get_status().volume == MAX_BUTTON_VOLUME, timeout=3000)
    assert app.window.volume_level() == MAX_BUTTON_VOLUME


def test_mute_is_instant_when_nothing_plays_and_the_fader_cancels_a_ramp(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    adapter.set_volume(180)
    qtbot.waitUntil(lambda: app._loop_controller.target_volume == 180, timeout=3000)
    app.window._volume_eq._mute_button.click()  # stopped: at once
    qtbot.waitUntil(lambda: adapter.get_status().volume == 0, timeout=2000)

    adapter.play()
    qtbot.waitUntil(lambda: app._last_playback_state == "playing", timeout=3000)
    app.window._volume_eq._max_ms.setValue(5000)
    app.window._volume_eq._max_button.click()
    assert app._volume_ramp is not None
    app.window._volume.volume_changed.emit(100)  # the user grabs the fader
    assert app._volume_ramp is None
    qtbot.wait(200)
    assert adapter.get_status().volume == 100


def test_ramp_times_are_remembered(qtbot, tmp_path) -> None:
    settings = _ini(tmp_path)
    first = _make_app(qtbot, tmp_path, settings=settings)
    first.window._volume_eq._max_ms.setValue(2500)
    first.window._volume_eq._mute_ms.setValue(750)
    assert (settings.volume_ramp_ms("max"), settings.volume_ramp_ms("mute")) == (2500, 750)
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _make_app(qtbot, second_dir, settings=settings)
    assert second.window.volume_ramp_times() == (2500, 750)


@pytest.mark.parametrize("bad", ["fast", "-5"])
def test_a_hand_edited_ramp_time_falls_back_or_clamps(tmp_path, bad) -> None:
    settings = _ini(tmp_path)
    settings._settings.setValue("volume/mute_ms", bad)
    assert settings.volume_ramp_ms("mute") in (0, 1500)
