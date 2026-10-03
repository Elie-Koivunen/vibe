"""0.9.0 (requested on its build): the Tempo panel -- Align skew, BPM Skew / Current /
Detected BPM, Increase / Reset / Lower gliding over their time like the volume's -- and a
window wide enough for the bookmark list's columns."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QRect, QSettings
from PySide6.QtGui import QColor

from bookmark_studio.settings.settings_service import SettingsService, TempoButtons
from tests.unit.test_050_ui import _bookmark, _make_app
from tests.unit.test_090_after_loop_and_fades import _play_from


def _settings(tmp_path) -> SettingsService:
    return SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))


def _panel(app):  # noqa: ANN001, ANN202
    return app.window._volume_eq.tempo_panel


def _with_detected(app, bpm: float):  # noqa: ANN001, ANN202
    """The song's detected BPM, and the BPM panel switched on."""
    app._estimated_bpm[app._current_media_id] = bpm
    app._show_tempo()
    panel = _panel(app)
    if not panel.is_tempo_enabled():
        panel.switch.click()
    return panel


# -- the Tempo panel (as sketched and approved on the 0.9.0 build) --


def test_the_tempo_panel_is_laid_out_as_sketched(qtbot, tmp_path) -> None:
    """Align skew above the shorter fader; BPM Skew / Current / Detected beside it;
    Increase / Reset / Lower stacked, Reset level with the fader's 0; Step and Glide
    under them. Between the volume's buttons and the equalizer."""
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    window.show_side_tab("volume")
    panel = _panel(app)

    def at(widget):  # noqa: ANN001, ANN202
        return widget.mapTo(window, QPoint(0, 0))

    volume = window._volume_eq
    assert at(volume._max_button).x() < at(panel.fader).x() < at(volume._enabled_check).x()
    assert panel.fader.height() < volume.volume_strip.fader.height()  # shorter than VOL's
    button, fader = panel.align_skew_button, panel.fader
    assert at(button).y() + button.height() <= at(fader).y()  # just above the fader...
    assert abs(button.width() - fader.width()) <= 2  # ...and as wide
    assert at(panel._skew_value).x() > at(fader).x() + fader.width()  # the readings beside it
    assert at(panel._skew_value).y() < at(panel._current_value).y() < at(panel._detected_value).y()
    ys = [at(b).y() for b in (panel.increase_button, panel.reset_button, panel.lower_button)]
    assert ys == sorted(ys) and ys[1] - ys[0] < 40 and ys[2] - ys[1] < 40  # stacked
    reset_middle = at(panel.reset_button).y() + panel.reset_button.height() / 2
    fader_zero = at(fader).y() + fader._y_for(0)
    assert abs(reset_middle - fader_zero) <= 6  # Reset level with the fader's 0
    assert at(panel._step).y() > at(panel.lower_button).y()
    assert [panel._step.value(), panel._glide_ms.value()] == [5, 2000]
    assert "Normalize" not in [b.text() for b in panel.findChildren(type(panel.reset_button))]


def test_the_readings_bpm_skew_current_and_detected(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    panel = _with_detected(app, 144.0)
    assert panel.readings() == ("0", "144", "144") and not panel.is_shown_skewed()
    panel.fader._set_by_user(-10)  # BPM Skew: how far from the detected BPM (0.9.0 build)
    assert panel.readings() == ("-10", "134", "144") and panel.is_shown_skewed()
    panel.fader._set_by_user(15)
    assert panel.readings() == ("+15", "159", "144")
    app._estimated_bpm.clear()
    app._show_tempo()
    assert panel.readings() == ("+15", "135", "–")  # not detected: counted as 120


def test_increase_and_lower_move_by_the_step_and_reset_goes_back_to_the_detected_bpm(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    panel = _with_detected(app, 100.0)
    app._on_tempo_buttons_changed(TempoButtons(step_bpm=5, glide_ms=0))
    panel.increase_button.click()
    panel.increase_button.click()
    assert app._tempo_change_bpm == 10 and panel.fader.value() == 10 and panel.readings()[1] == "110"
    qtbot.waitUntil(lambda: adapter.get_status().rate == pytest.approx(1.1), timeout=3000)
    panel.lower_button.click()
    assert app._tempo_change_bpm == 5
    panel.reset_button.click()
    assert app._tempo_change_bpm == 0 and panel.fader.value() == 0 and panel.readings()[1] == "100"
    qtbot.waitUntil(lambda: adapter.get_status().rate == 1.0, timeout=3000)


def test_align_skew_moves_the_faders_middle_to_what_plays_and_shows_it_in_red(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    panel = _with_detected(app, 144.0)
    panel.fader._set_by_user(-10)  # 134
    qtbot.waitUntil(lambda: adapter.get_status().rate == pytest.approx(134 / 144), timeout=3000)
    assert panel.is_shown_skewed() and not panel.is_shown_aligned()  # off the detected BPM
    assert "#e0403a" in panel._skew_value.styleSheet() and panel.align_skew_button.styleSheet() == ""
    panel.align_skew_button.click()
    assert panel.readings() == ("-10", "134", "144")  # still 10 off the detected BPM
    assert panel.fader.value() == 0 and panel.is_shown_aligned()  # the fader back in the middle
    assert "#e0403a" in panel._skew_value.styleSheet() and "#e0403a" in panel.align_skew_button.styleSheet()
    assert panel.fader._home_colour() == QColor(224, 64, 58)  # the fader's 0 mark, red too
    assert app._rate == pytest.approx(134 / 144)  # nothing changed for the ear
    panel.fader._set_by_user(20)  # +20 from the new middle
    assert panel.readings()[:2] == ("+10", "154")
    panel.reset_button.click()  # back to the detected BPM, the skew cancelled
    assert panel.readings() == ("0", "144", "144") and not panel.is_shown_skewed()
    assert not panel.is_shown_aligned()
    assert panel._skew_value.styleSheet() == "" and panel.align_skew_button.styleSheet() == ""
    assert panel.fader._home_colour() != QColor(224, 64, 58)


def test_the_fader_reaches_50_either_way_of_the_skew(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    panel = _with_detected(app, 120.0)
    app._on_tempo_buttons_changed(TempoButtons(step_bpm=50, glide_ms=0))
    panel.increase_button.click()
    panel.increase_button.click()  # as far as the fader goes
    assert app._tempo_change_bpm == 50
    panel.align_skew_button.click()  # its middle at 170 now
    panel.increase_button.click()
    assert app._tempo_change_bpm == 100 and panel.readings()[1] == "220"
    for expected in (50, 0, 0):  # Lower: down to 50 under the middle, no further
        panel.lower_button.click()
        assert app._tempo_change_bpm == expected


def test_while_playing_the_buttons_glide_there_step_by_step(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    panel = _with_detected(app, 100.0)
    _play_from(app, qtbot, 1_000_000)
    app._on_tempo_buttons_changed(TempoButtons(step_bpm=30, glide_ms=600))
    seen: list[float] = []
    original = app.window.show_tempo

    def record(detected, skew, change) -> None:  # noqa: ANN001
        seen.append(change)
        original(detected, skew, change)

    app.window.show_tempo = record
    panel.increase_button.click()
    assert app._tempo_change_bpm < 30  # not there at once
    qtbot.waitUntil(lambda: app._tempo_glide is None, timeout=3000)
    assert seen[-1] == pytest.approx(30) and len(seen) >= 4
    assert seen == sorted(seen) and any(0 < change < 30 for change in seen)
    qtbot.waitUntil(lambda: adapter.get_status().rate == pytest.approx(1.3), timeout=3000)
    assert panel.fader.value() == 30


def test_moving_the_tempo_fader_takes_over_from_a_glide(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    panel = _with_detected(app, 100.0)
    _play_from(app, qtbot, 1_000_000)
    app._on_tempo_buttons_changed(TempoButtons(step_bpm=40, glide_ms=5000))
    panel.increase_button.click()
    assert app._tempo_glide is not None
    panel.fader._set_by_user(-10)
    assert app._tempo_glide is None and app._tempo_change_bpm == -10


def test_a_double_click_on_the_detected_bpm_corrects_it(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    panel = _with_detected(app, 170.0)  # read at double the real tempo
    panel._ask_bpm = lambda current: current / 2
    panel.fader._set_by_user(10)
    panel._detected_value.double_clicked.emit()
    assert app._corrected_bpm[app._current_media_id] == 85.0
    assert panel.readings() == ("+10", "95", "85")
    qtbot.waitUntil(lambda: adapter.get_status().rate == pytest.approx(95 / 85), timeout=3000)
    panel._ask_bpm = lambda current: None  # cancelled: nothing changes
    panel._detected_value.double_clicked.emit()
    assert panel.readings()[2] == "85"


def test_step_and_glide_are_remembered(qtbot, tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.set_tempo_buttons(TempoButtons(step_bpm=2, glide_ms=750))
    app = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: None)
    panel = _panel(app)
    assert panel.buttons() == TempoButtons(step_bpm=2, glide_ms=750)
    panel._step.setValue(10)
    assert settings.tempo_buttons() == TempoButtons(step_bpm=10, glide_ms=750)


# -- room for the bookmark list's columns --


def _with_bookmarks(app) -> None:  # noqa: ANN001
    for index in range(4):
        _bookmark(app, name=f"a longer bookmark name {index}", start_us=1_000_000 * (index + 1),
                  end_us=1_000_000 * (index + 1) + 500_000, tags=("intro", "drop"), loop_enabled=True,
                  repeat_count=3, fade_in_ms=1500, fade_out_ms=2500)


def test_widening_for_the_columns_leaves_the_tab_the_width_the_user_gave_it(qtbot, tmp_path, monkeypatch) -> None:
    """(Found on WSL's wider fonts: taking the room from the tab undid the split the user
    had set, and which the next start brings back.) The window widens; the list gets all
    of it."""
    app = _make_app(qtbot, tmp_path)
    window = app.window
    monkeypatch.setattr(window, "_screen_rect", lambda: QRect(0, 0, 3000, 1600))
    window.resize(1200, 800)
    window.show()
    qtbot.waitExposed(window)
    window.show_side_tab("bookmark")
    bottom = window._splitters["bottom"]
    bottom.setSizes([300, 880])
    tab_width = bottom.sizes()[1]
    _with_bookmarks(app)
    qtbot.waitUntil(lambda: window._bookmark_panel.columns_short_by() == 0, timeout=5000)
    qtbot.waitUntil(lambda: bottom.sizes()[1] == tab_width, timeout=2000)
    assert window.width() > 1200


def test_a_later_reload_leaves_a_window_the_user_narrowed_alone(qtbot, tmp_path, monkeypatch) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    monkeypatch.setattr(window, "_screen_rect", lambda: QRect(0, 0, 3000, 1600))
    window.show()
    qtbot.waitExposed(window)
    _with_bookmarks(app)
    qtbot.waitUntil(lambda: window._bookmark_panel.columns_short_by() == 0, timeout=5000)
    window.resize(1100, 800)  # the user narrows it
    qtbot.waitUntil(lambda: window.width() == 1100, timeout=2000)
    _bookmark(app, name="one more, and a name long enough to need a wider column", start_us=5_200_000,
              end_us=5_400_000)
    qtbot.wait(300)
    assert window.width() == 1100


def test_the_window_widens_for_the_columns_as_far_as_the_screen_allows(qtbot, tmp_path, monkeypatch) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    monkeypatch.setattr(window, "_screen_rect", lambda: QRect(0, 0, 3000, 1600))  # a large screen
    window.resize(1000, 800)
    window.show()
    qtbot.waitExposed(window)
    window.show_side_tab("volume")  # the widest tab
    _with_bookmarks(app)
    qtbot.waitUntil(lambda: window._bookmark_panel.columns_short_by() == 0, timeout=5000)
    assert window.width() > 1000
    area = window._side_tabs.currentWidget()
    qtbot.waitUntil(lambda: area.viewport().width() >= area.widget().minimumSizeHint().width(), timeout=3000)


def test_showing_a_column_makes_room_for_it(qtbot, tmp_path, monkeypatch) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    monkeypatch.setattr(window, "_screen_rect", lambda: QRect(0, 0, 3000, 1600))
    window.show()
    qtbot.waitExposed(window)
    _with_bookmarks(app)
    panel = window._bookmark_panel
    qtbot.waitUntil(lambda: panel.columns_short_by() == 0, timeout=5000)
    panel.set_column_shown(0, False)
    panel.set_column_shown(0, True)  # back: room again
    qtbot.waitUntil(lambda: panel.columns_short_by() == 0, timeout=3000)


def test_a_window_opening_with_its_bookmarks_makes_room_for_their_columns(qtbot, tmp_path, monkeypatch) -> None:
    """At start the bookmarks are there before the window shows -- a narrow saved size
    widens as it opens."""
    app = _make_app(qtbot, tmp_path)
    window = app.window
    monkeypatch.setattr(window, "_screen_rect", lambda: QRect(0, 0, 3000, 1600))
    _with_bookmarks(app)  # not shown yet: nothing to fit
    app.session.status_timer.stop()  # (no poll refreshing the list meanwhile: opening alone)
    app.session.playlist_timer.stop()
    window.resize(1000, 800)
    window.show()
    qtbot.waitExposed(window)
    qtbot.waitUntil(lambda: window._bookmark_panel.columns_short_by() == 0, timeout=5000)
    assert window.width() > 1000
