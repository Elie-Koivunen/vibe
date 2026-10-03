"""0.5.0: logo, DJ-style volume fader, an audible start when a bookmark plays (85 % then;
the Reset level for a muted player since 0.9.0), centred transport
without the ±5 s buttons, a smooth waveform zoom, and Quit."""
from __future__ import annotations

import importlib.util
import statistics
import struct
import time
import wave
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QGraphicsItem, QLabel, QPushButton

from bookmark_studio.app import application as application_module
from bookmark_studio.app.application import Application

RESET_LEVEL = 128  # the Reset button's default, 50 % of VLC's 256
from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.persistence.database import connect
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.playback.status import VlcPlaylistItem
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.branding import app_icon, logo_pixmap
from bookmark_studio.ui.deck_fader import DeckFader, VolumeStrip, level_to_percent, percent_to_level
from bookmark_studio.ui.transport import TransportBar
from bookmark_studio.ui.waveform.scene import WaveformScene
from bookmark_studio.ui.waveform.view import WaveformView
from bookmark_studio.ui.waveform.waveform_item import cosmetic_pen
from bookmark_studio.waveform.peaks import compute_peaks
from bookmark_studio.waveform.pyramid import build_pyramid_from_peaks

# -- logo --


def test_the_logo_renders_at_every_icon_size(qapp) -> None:
    icon = app_icon()
    assert not icon.isNull()
    assert {s.width() for s in icon.availableSizes()} >= {16, 32, 48, 256}
    pixmap = logo_pixmap(64)
    assert (pixmap.width(), pixmap.height()) == (64, 64)
    image = pixmap.toImage()
    assert image.pixelColor(32, 32).alpha() > 0  # something was drawn


def test_windows_and_dialogs_carry_the_logo(qtbot) -> None:
    from bookmark_studio.ui.dialogs.vlc_launch_dialog import VlcLaunchDialog

    app = _make_app(qtbot, Path(qtbot._request.getfixturevalue("tmp_path")))
    assert not app.window.windowIcon().isNull()
    assert app.window._logo.pixmap() is not None and not app.window._logo.pixmap().isNull()
    dialog = VlcLaunchDialog([], "All files (*)", can_launch_vlc=True)
    qtbot.addWidget(dialog)
    logos = [label for label in dialog.findChildren(QLabel) if label.pixmap() is not None and not label.pixmap().isNull()]
    assert logos


def test_multi_size_ico_is_written(qapp, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location("vbs_build", Path(__file__).resolve().parents[2] / "packaging" / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    png, ico = build.render_icons(tmp_path)
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    if ico is not None:  # Windows only
        data = ico.read_bytes()
        _reserved, kind, count = struct.unpack_from("<HHH", data, 0)
        assert kind == 1 and count == len(build.ICO_SIZES)
        sizes = [struct.unpack_from("<B", data, 6 + 16 * i)[0] or 256 for i in range(count)]
        assert sizes == list(build.ICO_SIZES)


# -- transport --


def test_transport_is_one_group_without_seek_buttons(qtbot) -> None:
    """0.5.0: one centred row without -5s/+5s. Since 0.9.0 the group is a compact grid in
    the column beside the waveform, in the same order, with 🔁 after the others."""
    bar = TransportBar()
    qtbot.addWidget(bar)
    bar.show()
    qtbot.waitExposed(bar)
    assert not hasattr(bar, "seek_back_button") and not hasattr(bar, "seek_forward_button")
    buttons = [bar.previous_bookmark_button, bar.previous_track_button, bar.stop_button,
               bar.play_pause_button, bar.next_track_button, bar.next_bookmark_button,
               bar.loop_bookmark_button]
    texts = [b.text() for b in bar.findChildren(QPushButton)]
    assert "−5s" not in texts and "+5s" not in texts and len(texts) == 7
    corners = [b.mapTo(bar, QPoint(0, 0)) for b in buttons]
    reading_order = sorted(corners, key=lambda p: (p.y(), p.x()))
    assert corners == reading_order  # row by row, left to right


def test_arrow_keys_still_seek_five_seconds(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    seen = []
    app.window.seek_relative_requested.connect(seen.append)
    actions = {a.text(): a for a in app.window.menuBar().actions()}
    playback = actions["Playback"].menu()
    for action in playback.actions():
        if action.text() in ("Seek -5s", "Seek +5s"):
            action.trigger()
    assert seen == [-5_000_000, 5_000_000]


# -- the fader --


def test_fader_follows_clicks_wheel_keys_and_double_click(qtbot) -> None:
    fader = DeckFader()
    qtbot.addWidget(fader)
    fader.resize(86, 300)
    fader.show()
    qtbot.waitExposed(fader)
    seen = []
    fader.volume_changed.connect(seen.append)

    qtbot.mouseClick(fader, Qt.MouseButton.LeftButton, pos=QPoint(43, int(fader._y_for(50))))
    assert abs(fader.value() - 50) <= 1
    fader.setFocus()
    qtbot.keyClick(fader, Qt.Key.Key_Up)
    assert fader.value() == seen[-1] and seen[-1] == seen[-2] + 1
    qtbot.keyClick(fader, Qt.Key.Key_PageDown)
    assert seen[-1] == seen[-2] - 10
    qtbot.mouseDClick(fader, Qt.MouseButton.LeftButton, pos=QPoint(43, 20))
    assert fader.value() == 100 and seen[-1] == 100

    fader.set_volume_percent(30)  # from the player: no signal
    assert fader.value() == 30 and seen[-1] == 100


def test_player_updates_do_not_move_the_cap_under_the_users_finger(qtbot) -> None:
    fader = DeckFader()
    qtbot.addWidget(fader)
    fader.resize(86, 300)
    fader.show()
    qtbot.mousePress(fader, Qt.MouseButton.LeftButton, pos=QPoint(43, int(fader._y_for(60))))
    fader.set_volume_percent(10)  # a poll arrives mid-drag
    assert abs(fader.value() - 60) <= 1
    qtbot.mouseRelease(fader, Qt.MouseButton.LeftButton, pos=QPoint(43, int(fader._y_for(60))))
    fader.set_volume_percent(10)
    assert fader.value() == 10


def test_volume_strip_speaks_vlcs_scale(qtbot) -> None:
    strip = VolumeStrip()
    qtbot.addWidget(strip)
    assert (level_to_percent(256), level_to_percent(218), percent_to_level(85)) == (100, 85, 218)
    strip.set_level(128)
    assert strip.level() == 128 and strip._readout.text() == "50 %"


# -- volume in the running app --


def _wav(path: Path, seconds: float = 6.0) -> Path:
    n = int(seconds * 8000)
    with wave.open(str(path), "w") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes((np.sin(np.arange(n) / 3) * 8000).astype(np.int16).tobytes())
    return path


def _make_app(qtbot, tmp_path: Path, **kwargs) -> Application:
    a = _wav(tmp_path / "a.wav")
    b = _wav(tmp_path / "b.wav")
    adapter = MockPlaybackAdapter([VlcPlaylistItem(1, a.resolve().as_uri(), "A", 6.0),
                                   VlcPlaylistItem(2, b.resolve().as_uri(), "B", 6.0)])
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    app = Application(conn=conn, adapter=adapter, ffmpeg_path="/nonexistent", waveform_cache_dir=tmp_path / "wf",
                      **kwargs)
    qtbot.addWidget(app.window)
    app.start()
    qtbot.waitUntil(lambda: app._current_media_id is not None
                    and app.playlists.synchronizer.active_playlist_id is not None, timeout=5000)
    request = qtbot._request
    request.addfinalizer(app.stop)
    return app


def _bookmark(app: Application, **overrides) -> Bookmark:
    values = dict(
        id=uuid4(), playlist_id=app.playlists.synchronizer.active_playlist_id, media_id=app._current_media_id,
        scope=BookmarkScope.PLAYLIST_MEDIA, lane_id=None, bookmark_type=BookmarkType.SEGMENT, name="b",
        start_us=1_000_000, end_us=2_000_000, loop_enabled=False, repeat_count=None, loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    values.update(overrides)
    bookmark = Bookmark(**values)
    app._bookmark_repository.insert(bookmark)
    app._refresh_bookmark_views()
    return bookmark


def test_moving_the_fader_sets_the_player_volume_and_polls_move_the_fader(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    app.window._volume.volume_changed.emit(160)  # as a drag would
    qtbot.waitUntil(lambda: adapter.get_status().volume == 160, timeout=3000)
    assert app._loop_controller.target_volume == 160
    adapter.set_volume(300)  # changed in VLC's own window
    qtbot.waitUntil(lambda: app.window.volume_level() == 300, timeout=3000)


@pytest.mark.parametrize("start, expected", [(0, RESET_LEVEL), (100, 100), (300, 300)])
def test_playing_a_bookmark_keeps_the_users_level_and_a_muted_player_starts_at_reset(
    qtbot, tmp_path, start, expected,
) -> None:
    """0.5.0 raised anything below 85 %; since 0.9.0 the user's level stays across
    bookmarks -- only a player at 0 (e.g. launched muted) starts at the Reset level."""
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    adapter.set_volume(start)
    qtbot.waitUntil(lambda: app._loop_controller.target_volume == start, timeout=3000)
    bookmark = _bookmark(app)
    app._on_play_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: adapter.get_status().state == "playing", timeout=3000)
    qtbot.waitUntil(lambda: adapter.get_status().volume == expected, timeout=3000)
    assert app._loop_controller.target_volume == expected


def test_looping_a_bookmark_raises_the_volume_too_and_a_fade_in_ramps_to_it(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    adapter.set_volume(0)  # e.g. a VLC launched muted
    qtbot.waitUntil(lambda: app._loop_controller.target_volume == 0, timeout=3000)
    bookmark = _bookmark(app, end_us=4_000_000, loop_enabled=True, fade_in_ms=300)
    app._on_loop_bookmark_requested(bookmark.id)
    assert app._loop_controller.target_volume == RESET_LEVEL
    qtbot.waitUntil(lambda: adapter.get_status().volume == RESET_LEVEL, timeout=4000)


def test_a_muted_launch_is_known_at_once_so_a_bookmark_is_audible(qtbot, tmp_path) -> None:
    """A VLC the app launches starts muted. The volume model has to know that at once:
    it used to learn it one poll later, and a bookmark played in between stayed silent."""
    app = _make_app(qtbot, tmp_path, mute_on_connect=True)
    adapter = app.session.adapter
    qtbot.waitUntil(lambda: adapter.get_status().volume == 0, timeout=3000)
    assert app._loop_controller.target_volume == 0  # immediately, not a poll later
    assert app.window.volume_level() == 0
    bookmark = _bookmark(app)
    app._on_play_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: adapter.get_status().volume == RESET_LEVEL, timeout=3000)
    qtbot.wait(900)  # a later poll must not mute it again
    assert adapter.get_status().volume == RESET_LEVEL


# -- quit --


def test_quit_saves_typing_and_layout_then_exits_and_closes_the_launched_vlc(qtbot, tmp_path, monkeypatch) -> None:
    settings = SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    exits = []
    terminated = []
    monkeypatch.setattr(application_module, "terminate_managed_vlc", lambda *args: terminated.append(args))
    app = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: exits.append(True))
    bookmark = _bookmark(app)
    app.window._bookmark_panel.select_bookmark(bookmark.id)
    qtbot.waitUntil(lambda: app.window._inspector.current_bookmark() is not None, timeout=3000)
    app.window._inspector._name_edit.setText("typed but not committed")
    app._vlc_process = object()  # as if this app had launched the VLC

    app.window.quit_requested.emit()
    assert exits == [True]
    assert app._bookmark_repository.get(bookmark.id).name == "typed but not committed"
    assert settings.window_geometry() is not None
    app.stop()  # what aboutToQuit triggers
    assert len(terminated) == 1


def test_quit_is_in_the_file_menu_on_ctrl_q_and_next_to_launch_vlc(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path, quit_app=lambda: None)
    file_menu = {a.text(): a for a in app.window.menuBar().actions()}["File"].menu()
    quit_action = next(a for a in file_menu.actions() if a.text() == "Quit")
    assert quit_action.shortcut().toString() == "Ctrl+Q"
    assert app.window._playlist_panel._quit_button.text() == "Quit"


def test_window_layout_is_saved_on_quit_and_restored_on_the_next_start(qtbot, tmp_path) -> None:
    settings = SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    first = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: None)
    # Let the window settle first: its minimum size follows the panels' needs (0.6.0),
    # which can widen it a moment after it opens -- and the splitters with it.
    QApplication.processEvents()
    first.window._splitters["bottom"].setSizes([300, 900])
    QApplication.processEvents()
    saved_sizes = first.window._splitters["bottom"].sizes()
    first.quit()
    assert settings.window_geometry() == first.window.saveGeometry()
    first.stop()
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _make_app(qtbot, second_dir, settings=settings, quit_app=lambda: None)
    QApplication.processEvents()
    assert second.window._splitters["bottom"].sizes() == saved_sizes


def test_breadcrumb_counts_the_songs_bookmarks(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    _bookmark(app)
    _bookmark(app, start_us=3_000_000, end_us=4_000_000)
    app.window._refresh_bookmarks()
    assert app.window._breadcrumb.text().endswith("› 2 bookmarks")


# -- waveform zoom --


@pytest.fixture()
def long_waveform(qtbot):
    seconds = 600
    samples = (np.random.default_rng(0).uniform(-1, 1, 8000 * seconds)).astype("<f4")
    pyramid = build_pyramid_from_peaks(compute_peaks(samples, 64), 8000)
    scene = WaveformScene()
    view = WaveformView(scene)
    qtbot.addWidget(view)
    view.resize(1400, 380)
    view.show()
    qtbot.waitExposed(view)
    scene.set_duration_us(seconds * 1_000_000)
    scene.set_waveform(pyramid, seconds * 1_000_000)
    view.fit_entire_media()
    QApplication.processEvents()
    return scene, view


def _wheel(view: WaveformView, delta_y: int, delta_x: int = 0) -> None:
    pos = QPointF(700, 190)
    event = QWheelEvent(pos, view.viewport().mapToGlobal(pos), QPoint(0, 0), QPoint(delta_x, delta_y),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase,
                        False)
    QApplication.sendEvent(view.viewport(), event)


def test_waveform_items_only_paint_what_is_exposed(long_waveform) -> None:
    scene, _view = long_waveform
    flag = QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption
    assert scene._waveform_item.flags() & flag
    assert scene._ruler_item.flags() & flag
    assert cosmetic_pen(Qt.GlobalColor.black).isCosmetic()


def test_wheel_events_are_combined_and_sized_by_the_wheel(long_waveform) -> None:
    _scene, view = long_waveform
    before = view.transform().m11()
    for _ in range(8):
        _wheel(view, 15)  # a touchpad: 1/8 notch each
    assert view.transform().m11() == before  # nothing yet: combined, applied once
    QApplication.processEvents()
    assert view.transform().m11() == pytest.approx(before * 1.25, rel=1e-6)  # one notch in total
    _wheel(view, 0, delta_x=120)  # horizontal scroll: no zoom (used to zoom out)
    QApplication.processEvents()
    assert view.transform().m11() == pytest.approx(before * 1.25, rel=1e-6)


def test_zooming_a_ten_minute_song_stays_fast(long_waveform) -> None:
    """0.4 took ~0.5 s per zoom step here (up to 8 s zoomed in); now ~25 ms."""
    _scene, view = long_waveform
    times = []
    for delta in [120] * 10 + [-120] * 6:
        start = time.perf_counter()
        _wheel(view, delta)
        QApplication.processEvents()
        view.viewport().repaint()
        times.append(time.perf_counter() - start)
    assert statistics.median(times) < 0.25, times
