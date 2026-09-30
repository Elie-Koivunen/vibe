"""0.6.0: the rearranged window (transport above the waveform, selection readout below
it, tools beside it, Bookmark / Volume & EQ tabs) and the equalizer."""
from __future__ import annotations

from pathlib import Path

import pytest
import requests
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from bookmark_studio.domain.equalizer import (
    ISO_BANDS_HZ,
    MAX_DB,
    MIN_DB,
    NEUTRAL_PREAMP_DB,
    PRESETS,
    VLC_BANDS_HZ,
    EqualizerSettings,
    band_label,
)
from bookmark_studio.domain.selection import Selection
from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.transport import TransportBar
from bookmark_studio.ui.volume_eq_panel import CUSTOM_PRESET, EqFader, VolumeEqPanel
from bookmark_studio.ui.waveform.scene import WaveformScene
from bookmark_studio.ui.waveform.view import WaveformView
from tests.unit.test_050_ui import _make_app


def _top_left(widget: QWidget, window: QWidget) -> QPoint:
    return widget.mapTo(window, QPoint(0, 0))


def _ini_settings(tmp_path: Path) -> SettingsService:
    return SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))


# -- the layout --


def test_transport_sits_above_the_waveform_and_the_selection_below_it(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.resize(1400, 900)
    window.show()
    qtbot.waitExposed(window)
    transport, view, selection = window._transport, window._waveform_view, window._selection_bar
    view_top = _top_left(view, window).y()
    assert _top_left(transport, window).y() + transport.height() <= view_top
    assert _top_left(selection, window).y() >= view_top + view.height()
    # the selection readout spans the waveform's own column
    assert abs(_top_left(selection, window).x() - _top_left(view, window).x()) < 12


def test_view_bookmark_and_selection_buttons_stand_beside_the_waveform(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.resize(1400, 900)
    window.show()
    qtbot.waitExposed(window)
    view = window._waveform_view
    view_right = _top_left(view, window).x() + view.width()
    view_top = _top_left(view, window).y()
    buttons = [window._zoom_out_button, window._zoom_in_button, window._zoom_fit_button,
               window._bookmark_now_button, window._bookmark_selection_button,
               window._loop_selection_button, window._clear_selection_button]
    for button in buttons:
        assert button.isVisible(), button.text()
        corner = _top_left(button, window)
        assert corner.x() >= view_right, button.text()
        assert view_top <= corner.y() <= view_top + view.height(), button.text()
    # grouped top to bottom: view, then bookmark, then selection
    ys = [_top_left(b, window).y() for b in (window._zoom_fit_button, window._bookmark_now_button,
                                               window._bookmark_selection_button, window._loop_selection_button,
                                               window._clear_selection_button)]
    assert ys == sorted(ys)
    # none of them is left in the selection row
    assert not [c for c in window._selection_bar.children() if c in buttons]


def test_selection_row_shows_start_end_and_length(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window._waveform_scene.set_selection(Selection(start_us=9_855_000, end_us=23_249_000))
    assert window._selection_label.text() == "Selection"
    assert window._selection_start_label.text() == "00:00:09.855"
    assert window._selection_end_label.text() == "00:00:23.249"
    assert window._selection_duration_label.text().strip() == "length 00:00:13.394"
    assert window._loop_selection_button.isEnabled() and window._clear_selection_button.isEnabled()
    window._waveform_scene.clear_selection()
    assert window._selection_label.text() == "No selection"
    assert window._selection_duration_label.text() == ""
    assert not window._loop_selection_button.isEnabled()


def test_bookmark_settings_and_volume_eq_are_two_tabs(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    tabs = app.window._side_tabs
    assert [tabs.tabText(i).replace("&&", "&") for i in range(tabs.count())] == ["Bookmark", "Volume & EQ"]
    assert tabs.widget(0).widget() is app.window._inspector
    assert tabs.widget(1).widget() is app.window._volume_eq
    assert app.window._volume is app.window._volume_eq.volume_strip  # the fader moved into the tab
    assert tabs.currentIndex() == 0


def test_the_volume_readout_follows_the_fader_and_opens_its_tab(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show_volume(218)
    assert window._transport.volume_button.text() == "VOL 85 %"
    window._volume.fader._set_by_user(40)  # a drag in the tab
    assert window._transport.volume_button.text() == "VOL 40 %"
    window._transport.volume_button.click()
    assert window._side_tabs.currentIndex() == 1


def test_the_open_tab_is_remembered(qtbot, tmp_path) -> None:
    settings = _ini_settings(tmp_path)
    first = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: None)
    first.window.show_side_tab("volume")
    first.quit()
    first.stop()
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _make_app(qtbot, second_dir, settings=settings, quit_app=lambda: None)
    assert second.window._side_tabs.currentIndex() == 1


def test_play_pause_is_never_narrower_than_its_symbols(qtbot) -> None:
    bar = TransportBar()
    qtbot.addWidget(bar)
    button = bar.play_pause_button
    assert button.minimumWidth() >= button.fontMetrics().horizontalAdvance(button.text())


def test_at_the_smallest_window_size_nothing_overlaps(qtbot, tmp_path) -> None:
    """Wider (e.g. Linux) fonts need more than 900 px; squeezed below what its panels
    need, the playback buttons were drawn on top of each other."""
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    window.resize(100, 100)  # as small as it gets
    QApplication.processEvents()
    minimum = window.minimumSize()
    assert minimum.width() >= 900 and minimum.height() >= 600
    assert window.width() >= window.centralWidget().minimumSizeHint().width()
    bar = window._transport
    buttons = [bar.previous_bookmark_button, bar.previous_track_button, bar.stop_button,
               bar.play_pause_button, bar.next_track_button, bar.next_bookmark_button]
    edges = [(_top_left(b, window).x(), _top_left(b, window).x() + b.width()) for b in buttons]
    assert all(right <= next_left for (_l, right), (next_left, _r) in zip(edges, edges[1:])), edges
    assert _top_left(bar.volume_button, window).x() + bar.volume_button.width() <= edges[0][0]
    assert edges[-1][1] <= _top_left(bar._position_label, window).x()
    # a long playlist name is cut off in the header; it doesn't widen the window
    before = window.minimumWidth()
    window._context_names = ("A playlist with a very long name " * 12, "a song")
    window._show_breadcrumb(0)
    QApplication.processEvents()
    assert window.minimumWidth() == before


def test_ruler_labels_use_the_themes_text_colour(qtbot) -> None:
    """The ruler's labels were a fixed dark grey: unreadable on a dark theme."""
    scene = WaveformScene()
    view = WaveformView(scene)
    qtbot.addWidget(view)
    palette = view.palette()
    palette.setColor(QPalette.ColorRole.Base, QColor(20, 20, 20))
    palette.setColor(QPalette.ColorRole.Text, QColor(250, 250, 250))
    view.setPalette(palette)
    view.resize(800, 200)
    view.show()
    qtbot.waitExposed(view)
    scene.set_duration_us(60_000_000)
    view.fit_entire_media()
    QApplication.processEvents()
    image = view.viewport().grab().toImage()
    bright = sum(
        1 for x in range(0, image.width(), 2) for y in range(0, 20)
        if image.pixelColor(x, y).lightness() > 200
    )
    assert bright > 20


# -- the equalizer model --


def test_presets_are_vlcs_and_flat_is_neutral() -> None:
    assert len(PRESETS) == 18 and list(PRESETS)[:3] == ["Flat", "Classical", "Club"]
    assert PRESETS["Flat"] == (NEUTRAL_PREAMP_DB, (0.0,) * 10)
    assert PRESETS["Club"] == (6.0, (0.0, 0.0, 8.0, 5.6, 5.6, 5.6, 3.2, 0.0, 0.0, 0.0))  # as VLC reports it
    assert EqualizerSettings() == EqualizerSettings.from_preset("Flat", enabled=False)


def test_equalizer_values_are_clamped_and_matched_to_presets() -> None:
    settings = EqualizerSettings(enabled=True, preamp_db=40, bands_db=(25, -30, 1.26))
    assert settings.preamp_db == MAX_DB
    assert settings.bands_db[:3] == (MAX_DB, MIN_DB, 1.3) and len(settings.bands_db) == 10
    rock = EqualizerSettings.from_preset("Rock")
    assert rock.matching_preset() == "Rock"
    assert rock.with_band(0, 3.0).matching_preset() is None
    assert rock.with_enabled(False).matching_preset() == "Rock"


def test_band_labels() -> None:
    assert [band_label(hz) for hz in VLC_BANDS_HZ] == ["60", "170", "310", "600", "1k", "3k", "6k", "12k", "14k", "16k"]
    assert [band_label(hz) for hz in ISO_BANDS_HZ][:3] == ["31", "62", "125"]


def test_equalizer_settings_survive_a_restart_and_a_hand_edit(tmp_path) -> None:
    settings = _ini_settings(tmp_path)
    assert settings.equalizer() == EqualizerSettings()  # off and flat by default
    wanted = EqualizerSettings.from_preset("Techno").with_band(3, -2.5)
    settings.set_equalizer(wanted)
    settings.sync()
    assert _ini_settings(tmp_path).equalizer() == wanted  # an .ini says "true", not True
    raw = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    raw.setValue("equalizer/bands", "loud,louder")
    raw.sync()
    assert _ini_settings(tmp_path).equalizer() == EqualizerSettings()


# -- the Volume & EQ tab --


def test_eq_panel_switches_on_picks_presets_and_goes_custom(qtbot) -> None:
    panel = VolumeEqPanel()
    qtbot.addWidget(panel)
    seen: list[EqualizerSettings] = []
    panel.equalizer_changed.connect(seen.append)
    assert not panel._bands[0].isEnabled() and not panel._preset_combo.isEnabled()  # off: greyed out

    panel._enabled_check.setChecked(True)
    assert seen[-1].enabled and panel._bands[0].isEnabled()
    panel._preset_combo.activated.emit(panel._preset_combo.findText("Club"))
    assert seen[-1] == EqualizerSettings.from_preset("Club")
    assert panel._band_values[2].text() == "+8.0" and panel._preamp_value.text() == "6.0"

    panel._bands[0].db_changed.emit(-4.5)  # a drag
    assert seen[-1].bands_db[0] == -4.5
    assert panel._preset_combo.currentText() == CUSTOM_PRESET
    panel._reset_button.click()
    assert seen[-1] == EqualizerSettings.from_preset("Flat")
    count = len(seen)
    panel.set_equalizer(EqualizerSettings.from_preset("Pop"))  # from the settings: no signal
    assert len(seen) == count and panel._preset_combo.currentText() == "Pop"


def test_eq_panel_follows_the_players_bands_and_support(qtbot) -> None:
    panel = VolumeEqPanel()
    qtbot.addWidget(panel)
    panel.set_equalizer(EqualizerSettings(enabled=True))
    panel.set_band_frequencies(ISO_BANDS_HZ)
    assert panel._band_captions[0].text() == "31" and panel._band_captions[-1].text() == "16k"
    panel.set_equalizer_supported(False, "no equalizer here")
    assert not panel._enabled_check.isEnabled() and not panel._bands[0].isEnabled()
    assert panel._note.text() == "no equalizer here"
    panel.set_equalizer_supported(True)
    assert panel._enabled_check.isEnabled() and panel._bands[0].isEnabled()


def test_eq_fader_click_keys_double_click(qtbot) -> None:
    fader = EqFader(0.0)
    qtbot.addWidget(fader)
    fader.resize(32, 200)
    fader.show()
    qtbot.waitExposed(fader)
    seen: list[float] = []
    fader.db_changed.connect(seen.append)
    qtbot.mouseClick(fader, Qt.MouseButton.LeftButton, pos=QPoint(16, int(fader._y_for(10.0))))
    assert abs(fader.db() - 10.0) <= 0.3 and seen[-1] == fader.db()
    fader.setFocus()
    before = fader.db()
    qtbot.keyClick(fader, Qt.Key.Key_Down)
    assert seen[-1] == pytest.approx(before - 0.5)
    qtbot.mouseDClick(fader, Qt.MouseButton.LeftButton, pos=QPoint(16, 30))
    assert fader.db() == 0.0 and seen[-1] == 0.0
    qtbot.mousePress(fader, Qt.MouseButton.LeftButton, pos=QPoint(16, int(fader._y_for(-6.0))))
    fader.set_db(12.0)  # settings arriving mid-drag don't move it
    assert abs(fader.db() + 6.0) <= 0.3
    qtbot.mouseRelease(fader, Qt.MouseButton.LeftButton, pos=QPoint(16, int(fader._y_for(-6.0))))


# -- the equalizer in the running app --


def test_equalizer_changes_reach_the_player_and_are_saved(qtbot, tmp_path) -> None:
    settings = _ini_settings(tmp_path)
    app = _make_app(qtbot, tmp_path, settings=settings)
    adapter = app.session.adapter
    assert adapter.equalizer is None  # off by default: the player is left alone
    wanted = EqualizerSettings.from_preset("Rock")
    app.window._volume_eq.equalizer_changed.emit(wanted)
    qtbot.waitUntil(lambda: adapter.equalizer == wanted, timeout=3000)
    assert settings.equalizer() == wanted
    app.window._volume_eq.equalizer_changed.emit(wanted.with_enabled(False))
    qtbot.waitUntil(lambda: adapter.equalizer == wanted.with_enabled(False), timeout=3000)


def test_a_saved_equalizer_is_applied_when_a_player_connects(qtbot, tmp_path) -> None:
    settings = _ini_settings(tmp_path)
    saved = EqualizerSettings.from_preset("Dance")
    settings.set_equalizer(saved)
    app = _make_app(qtbot, tmp_path, settings=settings)
    assert app.window.equalizer_settings() == saved  # shown in the tab
    qtbot.waitUntil(lambda: app.session.adapter.equalizer == saved, timeout=3000)

    class IsoPlayer(MockPlaybackAdapter):
        equalizer_band_hz = ISO_BANDS_HZ

    new_player = IsoPlayer(list(app.session.adapter.get_playlist()))
    app._swap_adapter(new_player, mute_on_connect=False, new_vlc_process=None)
    qtbot.waitUntil(lambda: new_player.equalizer == saved, timeout=3000)
    assert app.window._volume_eq._band_captions[0].text() == "31"


def test_a_player_without_an_equalizer_is_never_sent_one(qtbot, tmp_path) -> None:
    settings = _ini_settings(tmp_path)
    settings.set_equalizer(EqualizerSettings.from_preset("Pop"))
    app = _make_app(qtbot, tmp_path, settings=settings)

    class NoEq(MockPlaybackAdapter):
        supports_equalizer = False

        def set_equalizer(self, settings, *, full=False):  # noqa: ANN001
            raise AssertionError("must not be called")

    player = NoEq(list(app.session.adapter.get_playlist()))
    app._swap_adapter(player, mute_on_connect=False, new_vlc_process=None)
    qtbot.waitUntil(lambda: app.session.connected, timeout=3000)
    app.window._volume_eq.equalizer_changed.emit(EqualizerSettings.from_preset("Ska"))
    qtbot.wait(200)
    assert not app.window._volume_eq._enabled_check.isEnabled()


# -- a VLC window's equalizer, over HTTP --


def _recording_http_adapter(monkeypatch) -> tuple[StandardHttpPlaybackAdapter, list]:
    adapter = StandardHttpPlaybackAdapter("127.0.0.1", 9, "x")
    sent: list = []
    monkeypatch.setattr(adapter, "_command", lambda command, params=None: sent.append((command, dict(params or {}))))
    return adapter, sent


def test_http_equalizer_switches_on_first_then_sends_only_changes(monkeypatch) -> None:
    adapter, sent = _recording_http_adapter(monkeypatch)
    rock = EqualizerSettings.from_preset("Rock")
    adapter.set_equalizer(rock)
    # VLC ignores preamp/band changes while its equalizer is off: "enableeq" comes first
    assert sent[0] == ("enableeq", {"val": 1})
    assert sent[1] == ("preamp", {"val": "5.0"})
    assert [p["band"] for c, p in sent[2:]] == list(range(10))
    assert sent[2] == ("equalizer", {"band": 0, "val": "8.0"})

    sent.clear()
    adapter.set_equalizer(rock.with_band(4, -1.0))
    assert sent == [("equalizer", {"band": 4, "val": "-1.0"})]
    sent.clear()
    adapter.set_equalizer(rock.with_band(4, -1.0).with_enabled(False))
    assert sent == [("enableeq", {"val": 0})]
    sent.clear()
    adapter.set_equalizer(rock)  # back on: everything again
    assert sent[0] == ("enableeq", {"val": 1}) and len(sent) == 12
    sent.clear()
    adapter.set_equalizer(rock, full=True)
    assert len(sent) == 12


def test_http_equalizer_is_resent_after_vlc_went_away(monkeypatch) -> None:
    adapter, sent = _recording_http_adapter(monkeypatch)
    rock = EqualizerSettings.from_preset("Rock")
    adapter.set_equalizer(rock)

    def gone():
        raise requests.ConnectionError("VLC closed")

    monkeypatch.setattr(adapter, "_status_json", gone)
    with pytest.raises(requests.ConnectionError):
        adapter.get_status()
    sent.clear()
    adapter.set_equalizer(rock)  # a new VLC on the same port knows nothing of it
    assert len(sent) == 12
