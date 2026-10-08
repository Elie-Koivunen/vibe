"""0.10.0 (requested on the 0.9.0 build): the top half in two tabs -- Bookmarking (the
waveform with the selection readout and View) and BM playback view: three tracks, the
bookmark ⏮ would play, the one ⏮ / ⏭ count from, and the one ⏭ would play, each showing
its bookmark's range only, at one time scale. Playback stays beside the tabs."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QSettings, Qt

from bookmark_studio.domain.enums import BookmarkType
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.bookmark_tracks import CURRENT, NEXT, PREVIOUS
from bookmark_studio.ui.qt_helpers import SELECTED_TAB_COLOR
from tests.unit.test_050_ui import _bookmark, _make_app
from tests.unit.test_090_after_loop_and_fades import _playing_bookmark, _two_song_app


def _shown(app, tab: str = "tracks"):  # noqa: ANN001, ANN202
    window = app.window
    window.resize(1400, 900)
    window.show()
    window.show_view_tab(tab)
    return window


def _ids(app) -> tuple:  # noqa: ANN001
    return tuple(t.bookmark_id if t is not None else None for t in app.window._bookmark_tracks.tracks())


def _three(app):  # noqa: ANN001, ANN202
    """a, b, c in the list's order (not by time)."""
    a = _bookmark(app, name="a", start_us=1_000_000, end_us=2_000_000)
    b = _bookmark(app, name="b", start_us=3_000_000, end_us=5_000_000, loop_enabled=True, repeat_count=4)
    c = _bookmark(app, name="c", start_us=500_000, end_us=1_000_000)
    app._on_bookmark_reorder_requested([a.id, b.id, c.id])
    return a, b, c


def test_the_top_half_has_a_bookmarking_and_a_bm_playback_view_tab(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = _shown(app, "bookmarking")
    qtbot.waitExposed(window)
    tabs = window._view_tabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Bookmarking", "BM playback view"]
    assert SELECTED_TAB_COLOR in tabs.tabBar().styleSheet()
    for widget in (window._waveform_view, window._selection_bar, window._zoom_fit_button):
        assert tabs.widget(0).isAncestorOf(widget)
    assert tabs.widget(1) is window._bookmark_tracks
    assert not tabs.isAncestorOf(window._transport)  # Playback: beside the tabs, for both
    assert window._transport.isVisible()
    window.show_view_tab("tracks")
    assert window._transport.isVisible() and not window._waveform_view.isVisible()
    assert window._transport.mapTo(window, QPoint(0, 0)).x() > tabs.mapTo(window, QPoint(0, 0)).x() + tabs.width() - 5


def test_the_tracks_are_the_bookmark_before_the_one_selected_and_the_one_after(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    a, b, c = _three(app)
    view = app.window._bookmark_tracks
    # Nothing to count from: as ⏮ / ⏭ then, the last and the first.
    qtbot.waitUntil(lambda: _ids(app) == (c.id, None, a.id), timeout=3000)
    app.window._bookmark_panel.select_bookmark(b.id)
    qtbot.waitUntil(lambda: _ids(app) == (a.id, b.id, c.id), timeout=3000)
    assert view.header_text(CURRENT).startswith("● CURRENT") and "b · " in view.header_text(CURRENT)
    assert "×4" in view.header_text(CURRENT) and "00:00:03.000 → 00:00:05.000" in view.header_text(CURRENT)
    assert view.header_text(PREVIOUS).startswith("◀ PREVIOUS") and "a · " in view.header_text(PREVIOUS)
    assert view.header_text(NEXT).startswith("▷ NEXT") and "c · " in view.header_text(NEXT)
    app.window._bookmark_panel.select_bookmark(a.id)  # the first: nothing before it
    qtbot.waitUntil(lambda: _ids(app) == (None, a.id, b.id), timeout=3000)
    assert view.header_text(PREVIOUS) == "◀ PREVIOUS"


def test_a_playing_bookmark_is_the_middle_track_as_for_previous_and_next_bookmark(qtbot, tmp_path) -> None:
    """The middle track is the bookmark ⏮ / ⏭ count from: playing beats selected."""
    app = _make_app(qtbot, tmp_path)
    a, b, c = _three(app)
    app._on_play_bookmark_requested(a.id)
    qtbot.waitUntil(lambda: _ids(app) == (None, a.id, b.id), timeout=3000)
    view = app.window._bookmark_tracks
    assert view.header_text(CURRENT).startswith("▶ PLAYING") and view.tracks()[1].state == "playing"
    app.window._bookmark_panel.select_bookmark(c.id)
    qtbot.wait(200)
    assert _ids(app) == (None, a.id, b.id)  # still the one playing
    app.window._transport.next_bookmark_button.click()  # ⏭: the bottom track's
    qtbot.waitUntil(lambda: _ids(app) == (a.id, b.id, c.id), timeout=3000)
    assert _playing_bookmark(app) == b.id


def test_double_clicks_do_what_previous_next_and_the_list_do(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = _shown(app)
    qtbot.waitExposed(window)
    a, b, c = _three(app)
    window._bookmark_panel.select_bookmark(b.id)
    qtbot.waitUntil(lambda: _ids(app) == (a.id, b.id, c.id), timeout=3000)
    view = window._bookmark_tracks
    qtbot.mouseDClick(view.canvas(NEXT), Qt.MouseButton.LeftButton)  # as ⏭
    qtbot.waitUntil(lambda: _playing_bookmark(app) == c.id and _ids(app) == (b.id, c.id, None), timeout=3000)
    assert window.selected_bookmark_id() == c.id  # selected, as ⏭ does
    qtbot.mouseDClick(view.canvas(PREVIOUS), Qt.MouseButton.LeftButton)  # as ⏮
    qtbot.waitUntil(lambda: _playing_bookmark(app) == b.id, timeout=3000)
    app._stop_loop()
    app._on_stop_clicked()
    app._clear_bookmark_playback()
    qtbot.mouseDClick(view.canvas(CURRENT), Qt.MouseButton.LeftButton)  # plays it, as the list does
    qtbot.waitUntil(lambda: _playing_bookmark(app) == b.id and app._bookmark_playback.state == "playing",
                    timeout=3000)


def test_the_tracks_show_their_ranges_at_one_time_scale(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = _shown(app)
    qtbot.waitExposed(window)
    a, b, c = _three(app)  # 1 s, 2 s, 0.5 s
    window._bookmark_panel.select_bookmark(b.id)
    qtbot.waitUntil(lambda: _ids(app) == (a.id, b.id, c.id), timeout=3000)
    view = window._bookmark_tracks
    widths = {role: view.canvas(role).region_rect().width() for role in (PREVIOUS, CURRENT, NEXT)}
    full = view.canvas(CURRENT).width()
    assert widths[CURRENT] == full  # the longest fills the width
    assert abs(widths[PREVIOUS] - full / 2) <= 2 and abs(widths[NEXT] - full / 4) <= 2


def test_the_red_line_moves_on_the_middle_track(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = _shown(app)
    qtbot.waitExposed(window)
    adapter = app.session.adapter
    a, b, c = _three(app)
    app._on_play_bookmark_requested(b.id)  # 3 s - 5 s
    qtbot.waitUntil(lambda: _ids(app)[1] == b.id and app._last_playback_state == "playing", timeout=3000)
    canvas = window._bookmark_tracks.canvas(CURRENT)
    qtbot.waitUntil(lambda: canvas.playhead_x() is not None, timeout=3000)
    first = canvas.playhead_x()
    adapter.advance_time_us(1_000_000)  # the middle of the range
    qtbot.waitUntil(lambda: canvas.playhead_x() is not None and canvas.playhead_x() > first + canvas.width() // 4,
                    timeout=3000)
    assert abs(canvas.playhead_x() - canvas.width() / 2) <= canvas.width() * 0.1
    assert window._bookmark_tracks.canvas(NEXT).playhead_x() is None  # only the middle one


def test_a_track_in_another_song_shows_that_songs_waveform(qtbot, tmp_path, monkeypatch) -> None:
    """Its song's waveform is asked for though that song isn't on screen, and drawn when it
    comes. (Unit tests have no FFmpeg: the decoded waveform is handed over as the
    orchestrator would.)"""
    import numpy as np

    from bookmark_studio.waveform.peaks import compute_peaks
    from bookmark_studio.waveform.pyramid import build_pyramid_from_peaks

    app, item_a, media_a, item_b, media_b = _two_song_app(qtbot, tmp_path)
    window = _shown(app)
    qtbot.waitExposed(window)
    asked: list = []
    monkeypatch.setattr(app._waveform_orchestrator, "request", lambda media_id, *_a, **_k: asked.append(media_id))
    in_a = _bookmark(app, media_id=media_a.id, name="in a", start_us=1_000_000, end_us=2_000_000)
    in_b = _bookmark(app, media_id=media_b.id, name="in b", start_us=500_000, end_us=1_500_000)
    app._on_bookmark_reorder_requested([in_a.id, in_b.id])
    window._bookmark_panel.select_bookmark(in_a.id)
    qtbot.waitUntil(lambda: _ids(app) == (None, in_a.id, in_b.id), timeout=3000)
    view = window._bookmark_tracks
    assert " · b.wav · " in view.header_text(NEXT)  # its song
    assert media_b.id in asked and asked.count(media_b.id) == 1  # asked for once
    samples = (np.sin(np.arange(8000 * 4) / 3.0) * 0.8).astype("<f4")
    app._waveform_orchestrator.waveform_ready.emit(media_b.id, build_pyramid_from_peaks(compute_peaks(samples, 64), 8000))
    qtbot.waitUntil(lambda: view.tracks()[2].pyramid is not None, timeout=3000)
    canvas = view.canvas(NEXT)
    image = canvas.grab().toImage()
    region = canvas.region_rect()
    middle = image.pixelColor(region.center().x(), canvas.height() // 2)  # inside the waveform
    fill = image.pixelColor(region.center().x(), 4)  # above it: the range's (orange) fill
    assert middle.blue() - middle.red() > fill.blue() - fill.red() + 30  # the blue waveform, drawn (dimmed)
    assert app._current_media_id == media_a.id  # the waveform on screen stays the first song's


def test_a_point_bookmark_track(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    point = _bookmark(app, name="cue", bookmark_type=BookmarkType.POINT, start_us=2_500_000, end_us=None)
    app.window._bookmark_panel.select_bookmark(point.id)
    qtbot.waitUntil(lambda: _ids(app)[1] == point.id, timeout=3000)
    assert "00:00:02.500 (point)" in app.window._bookmark_tracks.header_text(CURRENT)


def test_the_open_view_tab_is_remembered(qtbot, tmp_path) -> None:
    settings = SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    first = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: None)
    first.window.show_view_tab("tracks")
    first.quit()
    first.stop()
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _make_app(qtbot, second_dir, settings=settings, quit_app=lambda: None)
    assert second.window._view_tabs.currentIndex() == 1
