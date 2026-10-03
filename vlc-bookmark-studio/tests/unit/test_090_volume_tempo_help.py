"""0.9.0 (from testing its builds): the volume a fade restores can't be taken away by a
poll that saw it at 0; playing at 0 starts at the Reset level, other levels stay; the
Tempo fader; Help > GitHub Repository / License / About; the pre-release license."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QSettings
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QPlainTextEdit

from bookmark_studio.app.application import Application
from bookmark_studio.domain.enums import CompletionAction, LoopState
from bookmark_studio.domain.loop import LoopSpec
from bookmark_studio.persistence.database import connect
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.playback.loop_controller import LoopController
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.playback.playback_clock import PlaybackClock
from bookmark_studio.playback.status import VlcPlaylistItem
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.deck_fader import TempoFader
from bookmark_studio.waveform.pyramid import build_pyramid
from bookmark_studio.waveform.tempo import estimate_bpm, tempo_rate
from tests.unit.test_050_ui import _bookmark, _make_app
from tests.unit.test_regressions_020 import _wav

RESET_LEVEL = 128  # the Reset button's default level, 50 %


# -- a poll can't make a level that is being written the user's level --


class _HeldQueue:
    """A busy player: commands wait until run() (one), or run_all()."""

    def __init__(self) -> None:
        self.jobs: list = []
        self.fences: dict[str, int] = {}

    def submit(self, fn, *, on_done=None, on_error=None, coalesce_key=None, fences=()) -> None:  # noqa: ANN001
        self.jobs.append((fn, on_done, tuple(fences)))

    def has_pending(self, fence: str) -> bool:
        return any(fence in fences for _fn, _done, fences in self.jobs)

    def fence_ns(self, name: str) -> int:
        return self.fences.get(name, 0)

    def clear_pending(self) -> None:
        self.jobs.clear()

    def run(self, *, deliver: bool = True):  # noqa: ANN201
        fn, on_done, fences = self.jobs.pop(0)
        result = fn()
        finished = time.monotonic_ns()
        for name in fences:
            self.fences[name] = finished
        if deliver and on_done is not None:
            on_done(result)
        return on_done, result

    def run_all(self) -> None:
        while self.jobs:
            self.run()


def _controller():
    adapter = MockPlaybackAdapter([VlcPlaylistItem(1, "file:///s.mp3", "S", 60.0)])
    held = _HeldQueue()
    return adapter, held, LoopController(adapter, PlaybackClock(), executor=held)


def test_a_poll_while_the_restore_after_a_fade_out_is_on_its_way_is_ignored() -> None:
    """Reported on the 0.9.0 build: after a bookmark faded out and moved on, the volume
    stayed at 0 -- a poll that saw the faded-out 0 had become the user's level."""
    adapter, held, controller = _controller()
    controller.set_user_volume(200)
    controller._volume_owned = True  # a fade-out left it ducked
    controller._restore_volume_if_owned()  # the loop ended: back to 200 -- not yet written
    controller.set_target_volume(0, sampled_at_ns=time.monotonic_ns())  # a poll saw the 0
    assert controller.target_volume == 200
    before = time.monotonic_ns()
    held.run_all()
    controller.set_target_volume(0, sampled_at_ns=before)  # issued before the write landed
    assert controller.target_volume == 200
    later = max(held.fences["volume"], controller._volume_fence_ns) + 1  # a poll after the write landed
    controller.set_target_volume(150, sampled_at_ns=later)  # the user, in VLC's window
    assert controller.target_volume == 150


def test_a_segment_starting_at_0_for_its_fade_in_owns_the_volume_at_once() -> None:
    adapter, held, controller = _controller()
    controller.set_user_volume(200)
    spec = LoopSpec(start_us=1_000_000, end_us=4_000_000, repeat_count=1, gap_ms=0,
                    completion_action=CompletionAction.CONTINUE, fade_in_ms=500)
    controller.start(spec)
    on_done, result = held.run(deliver=False)  # the 0 is written; its callback not yet delivered
    controller.set_target_volume(0, sampled_at_ns=held.fences["volume"] + 1)  # a poll issued after it landed
    assert controller.target_volume == 200
    on_done(result)
    assert controller.target_volume == 200  # the fade-in ramps to the user's level


class _SlowVolume(MockPlaybackAdapter):
    """A busy VLC: a volume change takes a while to land."""

    def set_volume(self, level: int) -> None:
        time.sleep(0.15)
        super().set_volume(level)


def test_moving_on_to_a_bookmark_with_a_fade_in_comes_back_to_the_users_level(qtbot, tmp_path) -> None:
    a_wav = _wav(tmp_path / "a.wav", seconds=6.0)
    adapter = _SlowVolume([VlcPlaylistItem(1, a_wav.resolve().as_uri(), "A", 6.0)])
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    app = Application(conn=conn, adapter=adapter, ffmpeg_path="/nonexistent", waveform_cache_dir=tmp_path / "wf")
    qtbot.addWidget(app.window)
    app.start()
    qtbot._request.addfinalizer(app.stop)
    qtbot.waitUntil(lambda: app._current_media_id is not None
                    and app.playlists.synchronizer.active_playlist_id is not None, timeout=5000)
    app.window._volume.volume_changed.emit(200)
    qtbot.waitUntil(lambda: adapter.get_status().volume == 200, timeout=3000)
    first = _bookmark(app, name="first", start_us=1_000_000, end_us=1_400_000, loop_enabled=True, repeat_count=1,
                      fade_out_ms=300, completion_action=CompletionAction.NEXT_BOOKMARK)
    second = _bookmark(app, name="second", start_us=3_000_000, end_us=5_000_000, loop_enabled=True,
                       repeat_count=1, fade_in_ms=600)
    app._on_bookmark_reorder_requested([first.id, second.id])
    lowest_target: list[int] = [200]

    def watch() -> bool:
        lowest_target[0] = min(lowest_target[0], app._loop_controller.target_volume)
        playback = app._bookmark_playback
        return (playback is not None and playback.bookmark_id == second.id
                and not app._loop_controller._fade_active and app._loop_controller.state is LoopState.PLAYING
                and adapter.get_status().volume == 200)

    app._on_play_bookmark_requested(first.id)
    qtbot.waitUntil(watch, timeout=8000)  # the second faded in, back to 200
    assert lowest_target[0] == 200  # and the user's level never went anywhere


# -- the volume when playing starts --


def test_play_with_the_volume_at_0_starts_at_the_reset_level(qtbot, tmp_path) -> None:
    """Reported on the 0.9.0 build: a song played from the playlist started at 0 (a VLC
    the app launches starts muted)."""
    app = _make_app(qtbot, tmp_path, mute_on_connect=True)
    adapter = app.session.adapter
    qtbot.waitUntil(lambda: app._loop_controller.target_volume == 0 and adapter.get_status().volume == 0,
                    timeout=3000)
    app.window._transport.play_pause_button.click()
    qtbot.waitUntil(lambda: adapter.get_status().volume == RESET_LEVEL, timeout=3000)
    assert app.window.volume_level() == RESET_LEVEL


@pytest.mark.parametrize("way", ["double-click", "next track"])
def test_other_ways_of_playing_a_song_start_at_the_reset_level_too(qtbot, tmp_path, way) -> None:
    app = _make_app(qtbot, tmp_path, mute_on_connect=True)
    adapter = app.session.adapter
    qtbot.waitUntil(lambda: adapter.get_status().volume == 0, timeout=3000)
    if way == "double-click":
        app._on_playlist_item_double_clicked(2)
    else:
        app.window._transport.next_track_button.click()
    qtbot.waitUntil(lambda: adapter.get_status().volume == RESET_LEVEL, timeout=3000)


def test_a_level_the_user_set_stays_when_playing(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    app.window._volume.volume_changed.emit(90)  # 35 %
    qtbot.waitUntil(lambda: adapter.get_status().volume == 90, timeout=3000)
    app.window._transport.play_pause_button.click()
    app._on_play_bookmark_requested(_bookmark(app).id)
    qtbot.wait(500)
    assert adapter.get_status().volume == 90 and app._loop_controller.target_volume == 90


def test_the_reset_level_is_the_one_set_in_the_volume_tab(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path, mute_on_connect=True)
    adapter = app.session.adapter
    qtbot.waitUntil(lambda: adapter.get_status().volume == 0, timeout=3000)
    app._on_volume_levels_changed(80, 30)  # Reset: 30 %
    app.window._transport.play_pause_button.click()
    qtbot.waitUntil(lambda: adapter.get_status().volume == round(30 * 256 / 100), timeout=3000)


# -- Tempo --


def _beat_track(bpm: float, seconds: float = 40.0, rate: int = 8000) -> np.ndarray:
    rng = np.random.default_rng(7)
    signal = rng.normal(0, 0.02, int(seconds * rate))
    length = int(0.06 * rate)
    kick = np.sin(2 * np.pi * 60 * np.arange(length) / rate) * np.exp(-np.arange(length) / (0.012 * rate))
    beat = 0
    while (start := int(beat * 60.0 / bpm * rate)) + length < signal.size:
        signal[start:start + length] += kick
        beat += 1
    return signal.astype(np.float32)


@pytest.mark.parametrize("bpm", [85, 90, 100, 120, 124, 128, 140, 150])
def test_a_songs_bpm_is_estimated_from_its_waveform(bpm) -> None:
    estimate = estimate_bpm(build_pyramid(_beat_track(bpm), 8000))
    assert estimate is not None and abs(estimate - bpm) <= 1.0


def test_no_bpm_without_a_beat() -> None:
    rate = 8000
    tone = (np.sin(2 * np.pi * 440 * np.arange(rate * 30) / rate) * 0.3).astype(np.float32)
    assert estimate_bpm(build_pyramid(tone, rate)) is None
    assert estimate_bpm(build_pyramid(np.zeros(rate * 30, dtype=np.float32), rate)) is None
    assert estimate_bpm(build_pyramid(_beat_track(120, seconds=5), rate)) is None  # too short to tell


def test_the_rate_turns_the_songs_bpm_into_the_changed_one() -> None:
    assert tempo_rate(120, 0) == 1.0
    assert tempo_rate(120, 10) == pytest.approx(130 / 120)
    assert tempo_rate(100, -50) == 0.5
    assert tempo_rate(60, -50) == 0.25  # VLC's slowest
    assert tempo_rate(0, 10) == 1.0


def test_the_tempo_fader_runs_from_minus_to_plus_50_bpm_in_steps_of_5(qtbot) -> None:
    fader = TempoFader()
    qtbot.addWidget(fader)
    assert (fader.minimum(), fader.maximum(), fader.value(), fader.singleStep()) == (-50, 50, 0, 5)
    moves: list[int] = []
    fader.volume_changed.connect(moves.append)
    fader._set_by_user(12)  # snaps to a step
    fader._set_by_user(99)  # and stays in range
    assert moves == [10, 50]
    fader.mouseDoubleClickEvent(_DoubleClick())
    assert fader.value() == 0 and moves[-1] == 0


class _DoubleClick:
    def accept(self) -> None:
        pass


def test_the_tempo_sits_between_the_volume_buttons_and_the_equalizer(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    window.show_side_tab("volume")
    panel = window._volume_eq
    x = [w.mapTo(window, QPoint(0, 0)).x() for w in (panel._max_button, panel.tempo_panel, panel._enabled_check)]
    assert x == sorted(x)
    assert panel.tempo_panel.fader.width() == panel.volume_strip.fader.width()  # the same shape


def test_moving_the_tempo_sets_the_players_rate_from_the_songs_bpm(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    media_id = app._current_media_id
    app._estimated_bpm[media_id] = 120.0
    app._show_tempo()
    app.window._volume_eq.tempo_panel.switch.click()  # BPM on
    app.window._volume_eq.tempo_panel.fader._set_by_user(30)
    qtbot.waitUntil(lambda: adapter.get_status().rate == pytest.approx(150 / 120), timeout=3000)
    assert app._clock.rate == pytest.approx(150 / 120)  # timing follows at once
    app._on_detected_bpm_corrected(100.0)  # the user's correction
    qtbot.waitUntil(lambda: adapter.get_status().rate == pytest.approx(130 / 100), timeout=3000)
    assert app._corrected_bpm[media_id] == 100.0
    app.window._volume_eq.tempo_panel.fader._set_by_user(0)
    qtbot.waitUntil(lambda: adapter.get_status().rate == 1.0, timeout=3000)


def test_the_tempo_is_kept_nowhere_and_is_undone_at_quit(qtbot, tmp_path) -> None:
    """Only for playing: not a bookmark setting, not saved; a VLC window the app attached
    to isn't left playing at another tempo."""
    from bookmark_studio.domain.bookmark import Bookmark

    settings = SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    app = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: None)
    adapter = app.session.adapter
    app.window._volume_eq.tempo_panel.switch.click()  # BPM on
    app.window._volume_eq.tempo_panel.fader._set_by_user(20)
    qtbot.waitUntil(lambda: adapter.get_status().rate != 1.0, timeout=3000)
    assert not [field for field in Bookmark.__dataclass_fields__ if "tempo" in field or "rate" in field]
    app.quit()  # saves the layout and settings
    app.stop()  # the session ends
    assert adapter.get_status().rate == 1.0
    settings.sync()
    kept = [key for key in settings._settings.allKeys() if "tempo" in key.lower() or "bpm" in key.lower()]
    assert kept == ["tempo/enabled"]  # the switch is remembered, not the tempo


def test_a_loops_pass_is_timed_anew_when_the_tempo_changes(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    bookmark = _bookmark(app, start_us=1_000_000, end_us=5_000_000, loop_enabled=True)
    app._on_loop_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.PLAYING, timeout=3000)
    before = app._loop_controller._boundary_timer.remainingTime()
    app._estimated_bpm[app._current_media_id] = 100.0
    app._on_tempo_switched(True)
    app.window._volume_eq.tempo_panel.fader._set_by_user(50)  # x1.5
    after = app._loop_controller._boundary_timer.remainingTime()
    assert after < before * 0.8  # the end comes sooner


# -- Help: GitHub, license, about (requested on the 0.9.0 build) --


def test_help_has_the_repository_the_license_and_about(qtbot, tmp_path) -> None:
    from bookmark_studio.about import DEVELOPERS, OWNER, REPOSITORY_URL

    app = _make_app(qtbot, tmp_path)
    window = app.window
    help_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "Help")
    texts = [a.text() for a in help_menu.actions() if a.text()]
    assert texts == ["GitHub Repository", "License", "About VLC Bookmark Studio"]
    assert REPOSITORY_URL == "https://github.com/Elie-Koivunen/vibe/tree/main/vlc-bookmark-studio"
    assert (OWNER, DEVELOPERS) == ("Elie Koivunen", "Elie Koivunen and Claude (Anthropic)")
    about = window.about_text()
    for part in ("Owner:</b> Elie Koivunen", "Developed by:</b> Elie Koivunen and Claude (Anthropic)",
                 REPOSITORY_URL, "Proprietary pre-release"):
        assert part in about, part
    license_action = next(a for a in help_menu.actions() if a.text() == "License")
    license_action.trigger()
    shown = window._license_dialog.findChild(QPlainTextEdit)
    assert shown.toPlainText().startswith("VLC Bookmark Studio - Pre-release License")
    window._license_dialog.close()
    assert isinstance(next(a for a in help_menu.actions() if a.text() == "GitHub Repository"), QAction)


def test_the_license_is_proprietary_until_the_final_version() -> None:
    from bookmark_studio.about import license_text

    project = Path(__file__).resolve().parents[2]
    text = (project / "LICENSE").read_text(encoding="utf-8")
    assert license_text() == text
    assert "All rights reserved" in text and "proprietary pre-release" in text
    assert "final version of the Software under an\nopen-source license" in text
    assert "0.1.0 to 0.8.0" in text and "GNU General Public\nLicense v3" in text  # what was published stays GPL
    notices = (project / "packaging" / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
    assert "proprietary pre-release software" in notices and "free software under the GNU" not in notices
    build = (project / "packaging" / "build.py").read_text(encoding="utf-8")
    assert 'shutil.copy2(ROOT / "LICENSE", app_dir / "LICENSE.txt")' in build  # its own, not the repository's
    pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = { file = "LICENSE" }' in pyproject and "Proprietary" in pyproject

