from __future__ import annotations

import shutil
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from bookmark_studio.app.application import Application
from bookmark_studio.persistence.database import connect
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.playback.status import VlcPlaylistItem

FFMPEG_PATH = shutil.which("ffmpeg") or r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"
_HAS_FFMPEG = Path(FFMPEG_PATH).exists()


def _write_test_wav(path: Path, seconds: float = 1.0, sample_rate: int = 22050) -> None:
    n = int(seconds * sample_rate)
    t = np.arange(n) / sample_rate
    samples = (np.sin(2 * np.pi * 440 * t) * 0.3 * 32767).astype(np.int16)
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(samples.tobytes())


@pytest.fixture()
def running_app(qtbot, tmp_path: Path):
    """Yields (app, tmp_path); guarantees app.stop() runs even if the test body
    raises -- an earlier version skipped stop() on assertion failure, leaving live
    QTimers from one test firing into another test's already-destroyed widgets.
    """
    state: dict = {}

    def _make(adapter, ffmpeg_path: str = FFMPEG_PATH, mute_on_connect: bool = False) -> Application:
        conn = connect(tmp_path / "test.db")
        migrate(conn)
        app = Application(
            conn=conn, adapter=adapter, ffmpeg_path=ffmpeg_path, waveform_cache_dir=tmp_path / "cache",
            mute_on_connect=mute_on_connect,
        )
        qtbot.addWidget(app.window)
        state["app"] = app
        return app

    yield _make

    app = state.get("app")
    if app is not None:
        app.stop()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not found on this machine")
def test_full_session_updates_breadcrumb_bookmarks_and_waveform(qtbot, tmp_path: Path, running_app) -> None:
    """End-to-end: MockPlaybackAdapter -> Application polling loop -> MainWindow.

    Exercises the spec #178/#179 startup and song-change sequences for real: media
    resolution, ad-hoc playlist creation, breadcrumb/bookmark refresh, and a real
    ffmpeg-backed waveform arriving asynchronously via WaveformOrchestrator.
    """
    wav_path = tmp_path / "song.wav"
    _write_test_wav(wav_path)

    adapter = MockPlaybackAdapter(
        [VlcPlaylistItem(vlc_id=1, uri=wav_path.resolve().as_uri(), name="Song", duration_s=1.0)]
    )
    app = running_app(adapter)
    app.start()

    qtbot.waitUntil(lambda: "No playlist" not in app.window._breadcrumb.text(), timeout=5000)
    assert app._current_media_id is not None
    assert "Unsaved VLC Playlist" in app.window._breadcrumb.text()

    def waveform_arrived() -> bool:
        return app.window._waveform_scene._waveform_item._pyramid.levels[0].peaks.shape[0] > 0

    qtbot.waitUntil(waveform_arrived, timeout=15000)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not found on this machine")
def test_waveform_arrives_even_when_status_reports_a_bare_filename(qtbot, tmp_path: Path, running_app) -> None:
    """Regression, root-caused from a real running session's logs: VLC's built-in HTTP
    interface's status.json only ever gives StandardHttpPlaybackAdapter.get_status() a
    bare filename for media_uri (see http_fallback._extract_media_uri), never a real
    file:// URI. Resolving "current media" identity straight from that field created a
    second, disconnected Media row every track change -- unfingerprintable, so the
    waveform correctly decoded under the *real* (playlist-resolved) media_id never
    matched self._current_media_id and was silently dropped. This test's adapter
    reproduces that exact real-world shape (playlist item has a full URI; get_status()
    reports only the basename) to prove the playlist-based lookup fix actually closes
    the gap.
    """
    wav_path = tmp_path / "song.wav"
    _write_test_wav(wav_path)
    full_uri = wav_path.resolve().as_uri()

    adapter = MockPlaybackAdapter([VlcPlaylistItem(vlc_id=1, uri=full_uri, name="Song", duration_s=1.0)])
    real_get_status = adapter.get_status

    def _bare_filename_get_status():
        status = real_get_status()
        return replace(status, media_uri=wav_path.name)  # e.g. "song.wav", not a URI

    adapter.get_status = _bare_filename_get_status
    app = running_app(adapter)
    app.start()

    def waveform_arrived() -> bool:
        return app.window._waveform_scene._waveform_item._pyramid.levels[0].peaks.shape[0] > 0

    qtbot.waitUntil(waveform_arrived, timeout=15000)


def test_resolve_current_item_uri_prefers_the_playlist_over_the_bare_status_filename(qtbot, running_app) -> None:
    adapter = MockPlaybackAdapter(
        [VlcPlaylistItem(vlc_id=1, uri="file:///real/path/song.mp3", name="Song", duration_s=10.0)]
    )
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()
    qtbot.waitUntil(lambda: len(app._playlist_items) > 0, timeout=3000)

    assert app._resolve_current_item_uri(1) == "file:///real/path/song.mp3"
    assert app._resolve_current_item_uri(999) is None  # unknown id -> caller falls back


def test_playlist_panel_populates_from_live_polling(qtbot, running_app) -> None:
    """Regression: an earlier version never wired playlist poll results into the UI's
    playlist panel at all -- only throwaway demo scripts populated it manually. A real
    user launching the real app saw a permanently empty playlist panel."""
    adapter = MockPlaybackAdapter(
        [
            VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0),
            VlcPlaylistItem(vlc_id=2, uri="file:///b.mp3", name="Song B", duration_s=20.0),
        ]
    )
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()

    qtbot.waitUntil(lambda: app.window._playlist_panel._tree.topLevelItemCount() == 2, timeout=5000)
    titles = {
        app.window._playlist_panel._tree.topLevelItem(i).text(1)
        for i in range(app.window._playlist_panel._tree.topLevelItemCount())
    }
    assert titles == {"Song A", "Song B"}


def test_one_bad_playlist_item_does_not_hide_all_others(qtbot, running_app, monkeypatch) -> None:
    """Regression: an earlier version resolved every playlist item inside a single
    list comprehension, so one item raising (a bad path, an exotic filename VLC
    reported oddly -- confirmed live with a real non-ASCII-named file added to VLC's
    playlist mid-session) aborted the whole update. Every subsequent poll hit the
    exact same failure, so the playlist panel silently never updated again for the
    rest of the session -- not just skipping the bad item, hiding every good one too.
    """
    from bookmark_studio.media.resolver import MediaResolver

    original_resolve = MediaResolver.resolve

    def flaky_resolve(self, uri, **kwargs):
        if "bad" in uri:
            raise RuntimeError("simulated resolution failure for an exotic filename")
        return original_resolve(self, uri, **kwargs)

    monkeypatch.setattr(MediaResolver, "resolve", flaky_resolve)

    adapter = MockPlaybackAdapter(
        [
            VlcPlaylistItem(vlc_id=1, uri="file:///good.mp3", name="Good Song", duration_s=10.0),
            VlcPlaylistItem(vlc_id=2, uri="file:///bad_song.mp3", name="Bad Song", duration_s=5.0),
        ]
    )
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()

    qtbot.waitUntil(lambda: app.window._playlist_panel._tree.topLevelItemCount() >= 1, timeout=5000)
    titles = {
        app.window._playlist_panel._tree.topLevelItem(i).text(1)
        for i in range(app.window._playlist_panel._tree.topLevelItemCount())
    }
    assert "Good Song" in titles
    assert "Bad Song" not in titles


def test_transport_play_pause_button_commands_the_real_adapter(qtbot, running_app) -> None:
    """Regression: TransportBar's buttons emitted signals that nothing listened to --
    clicking Play/Pause, Stop, or seek did nothing to the actual VLC connection."""
    adapter = MockPlaybackAdapter(
        [VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0)]
    )
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()
    assert adapter.get_status().state == "stopped"

    app.window._transport.play_pause_clicked.emit()
    qtbot.waitUntil(lambda: adapter.get_status().state == "playing", timeout=3000)

    app.window._transport.stop_clicked.emit()
    qtbot.waitUntil(lambda: adapter.get_status().state == "stopped", timeout=3000)


def test_transport_seek_forward_commands_the_real_adapter(qtbot, running_app) -> None:
    adapter = MockPlaybackAdapter(
        [VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0)]
    )
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()

    app.window._transport.seek_forward_clicked.emit()
    qtbot.waitUntil(lambda: adapter.get_status().time_us == 5_000_000, timeout=3000)


def test_waveform_click_seeks_the_real_adapter(qtbot, running_app) -> None:
    """Regression: clicking the waveform to seek only moved a local cosmetic playhead
    (WaveformScene.set_playhead_time_us) without ever telling VLC to seek."""
    adapter = MockPlaybackAdapter(
        [VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0)]
    )
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()

    app.window._waveform_scene.seek_requested.emit(3_000_000)
    qtbot.waitUntil(lambda: adapter.get_status().time_us == 3_000_000, timeout=3000)


def test_transport_becomes_enabled_once_status_polling_succeeds(qtbot, running_app) -> None:
    """Regression: TransportBar.set_transport_enabled() (spec #137) existed but was
    never called from anywhere -- buttons stayed clickable-looking even while
    disconnected, so a click silently did nothing. Reported live as "the player
    buttons do not map to vlc player, hence not functioning"."""
    adapter = MockPlaybackAdapter(
        [VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0)]
    )
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    assert app.window._transport.play_pause_button.isEnabled() is False

    app.start()
    qtbot.waitUntil(lambda: app.window._transport.play_pause_button.isEnabled() is True, timeout=3000)
    assert app.window._transport._connection_label.text() == "● Connected"


def test_play_bookmark_requested_seeks_and_plays(qtbot, running_app) -> None:
    """"another set below that would play explicitly from the bookmark listing
    itself" -- direct user request. Play Bookmark must work from the saved bookmark
    row alone, independent of any current waveform selection."""
    from uuid import uuid4

    from bookmark_studio.domain.bookmark import Bookmark
    from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction

    adapter = MockPlaybackAdapter([VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=60.0)])
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()
    qtbot.waitUntil(lambda: app._current_media_id is not None, timeout=3000)

    bookmark = Bookmark(
        id=uuid4(), playlist_id=app._synchronizer.active_playlist_id, media_id=app._current_media_id,
        scope=BookmarkScope.PLAYLIST_MEDIA, lane_id=None, bookmark_type=BookmarkType.SEGMENT,
        name="Chorus", start_us=20_000_000, end_us=30_000_000, loop_enabled=True, repeat_count=3,
        loop_gap_ms=500, completion_action=CompletionAction.STOP,
    )
    app._bookmark_repository.insert(bookmark)

    app._on_play_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: adapter.get_status().time_us == 20_000_000, timeout=3000)
    qtbot.waitUntil(lambda: adapter.get_status().state == "playing", timeout=3000)


def test_loop_bookmark_requested_starts_loop_with_its_own_saved_settings(qtbot, running_app) -> None:
    from uuid import uuid4

    from bookmark_studio.domain.bookmark import Bookmark
    from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction

    adapter = MockPlaybackAdapter([VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=60.0)])
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()
    qtbot.waitUntil(lambda: app._current_media_id is not None, timeout=3000)

    bookmark = Bookmark(
        id=uuid4(), playlist_id=app._synchronizer.active_playlist_id, media_id=app._current_media_id,
        scope=BookmarkScope.PLAYLIST_MEDIA, lane_id=None, bookmark_type=BookmarkType.SEGMENT,
        name="Chorus", start_us=20_000_000, end_us=30_000_000, loop_enabled=True, repeat_count=3,
        loop_gap_ms=500, completion_action=CompletionAction.STOP,
    )
    app._bookmark_repository.insert(bookmark)

    app._on_loop_bookmark_requested(bookmark.id)

    assert app._loop_controller.spec.start_us == 20_000_000
    assert app._loop_controller.spec.end_us == 30_000_000
    assert app._loop_controller.spec.repeat_count == 3
    assert app._loop_controller.spec.gap_ms == 500
    assert app._loop_controller.spec.completion_action == CompletionAction.STOP


def test_loop_bookmark_requested_is_a_noop_for_a_point_bookmark(qtbot, running_app) -> None:
    from uuid import uuid4

    from bookmark_studio.domain.bookmark import Bookmark
    from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction

    adapter = MockPlaybackAdapter([VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=60.0)])
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()
    qtbot.waitUntil(lambda: app._current_media_id is not None, timeout=3000)

    point_bookmark = Bookmark(
        id=uuid4(), playlist_id=app._synchronizer.active_playlist_id, media_id=app._current_media_id,
        scope=BookmarkScope.PLAYLIST_MEDIA, lane_id=None, bookmark_type=BookmarkType.POINT,
        name="Intro", start_us=1_000_000, end_us=None, loop_enabled=False, repeat_count=None,
        loop_gap_ms=0, completion_action=CompletionAction.CONTINUE,
    )
    app._bookmark_repository.insert(point_bookmark)

    app._on_loop_bookmark_requested(point_bookmark.id)
    assert app._loop_controller.spec is None


def test_playlist_item_selected_previews_without_disturbing_actual_playback(qtbot, running_app) -> None:
    """Direct user request: "when a user clicks through the songs, it is instantly
    visible" -- a single click on a DIFFERENT song than the one actually playing must
    switch the displayed waveform/context without telling VLC to change tracks, and
    must turn off "Follow currently playing VLC song" so a later status poll doesn't
    immediately snap the view back.
    """
    adapter = MockPlaybackAdapter(
        [
            VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0),
            VlcPlaylistItem(vlc_id=2, uri="file:///b.mp3", name="Song B", duration_s=20.0),
        ]
    )
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()
    qtbot.waitUntil(lambda: app._actually_playing_vlc_item_id == 1, timeout=3000)
    playing_media_id = app._current_media_id
    assert app.window._playlist_panel.follow_vlc_enabled() is True

    app._on_playlist_item_selected(2)

    assert app._current_vlc_item_id == 2
    assert app._current_media_id != playing_media_id
    assert app._actually_playing_vlc_item_id == 1  # actual playback untouched
    assert app.window._playlist_panel.follow_vlc_enabled() is False

    # A later poll must not overwrite the preview: still previewing item 2.
    qtbot.wait(600)
    assert app._current_vlc_item_id == 2


def test_re_enabling_follow_snaps_back_to_the_actually_playing_track(qtbot, running_app) -> None:
    adapter = MockPlaybackAdapter(
        [
            VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0),
            VlcPlaylistItem(vlc_id=2, uri="file:///b.mp3", name="Song B", duration_s=20.0),
        ]
    )
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()
    qtbot.waitUntil(lambda: app._actually_playing_vlc_item_id == 1, timeout=3000)

    app._on_playlist_item_selected(2)
    assert app._current_vlc_item_id == 2

    app.window._playlist_panel.set_follow_vlc(True)
    assert app._current_vlc_item_id == 1


def test_offline_start_does_not_crash(qtbot, running_app) -> None:
    """spec #104: VLC unreachable must not prevent the app from starting."""
    adapter = MockPlaybackAdapter([])
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()
    qtbot.wait(300)
    assert app.window._breadcrumb.text()  # never crashed, still has a valid label


def test_mute_on_connect_zeroes_volume_once_vlc_is_reachable(qtbot, running_app) -> None:
    """When this Application spawned its own VLC (bootstrap.launch_vlc_with_media),
    it must come up muted per the user's explicit request -- but only once, on the
    first successful poll, not on every tick."""
    adapter = MockPlaybackAdapter(
        [VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0)]
    )
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe", mute_on_connect=True)
    app.start()

    qtbot.waitUntil(lambda: adapter._volume == 0, timeout=3000)
    assert app._mute_pending is False


def test_swap_adapter_retargets_session_at_a_new_instance(qtbot, running_app) -> None:
    """Covers the core of _attach_to_vlc/_launch_new_vlc without spawning a real VLC
    process or driving VlcLaunchDialog's UI: both just delegate to _swap_adapter."""
    old_adapter = MockPlaybackAdapter(
        [VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0)]
    )
    app = running_app(old_adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()
    qtbot.waitUntil(lambda: app._current_media_id is not None, timeout=3000)
    assert old_adapter.connected is True

    new_adapter = MockPlaybackAdapter(
        [VlcPlaylistItem(vlc_id=2, uri="file:///b.mp3", name="Song B", duration_s=5.0)]
    )
    app._swap_adapter(new_adapter, mute_on_connect=True, new_vlc_process=None)

    assert app._adapter is new_adapter
    assert new_adapter.connected is True
    assert old_adapter.connected is False
    assert app._loop_controller._adapter is new_adapter
    assert app._mute_pending is True
    assert app._current_media_id is None
    assert app._current_vlc_item_id is None
    assert app._status_timer.isActive()
    assert app._playlist_timer.isActive()

    qtbot.waitUntil(lambda: new_adapter._volume == 0, timeout=3000)


def test_prompt_vlc_launch_dialog_without_settings_shows_message_and_does_not_touch_adapter(
    qtbot, running_app, monkeypatch
) -> None:
    """Application constructed without settings/vlc_path (e.g. every other test in this
    file) must degrade to an informative message, not a crash -- this is the path any
    test-constructed Application takes if something ever calls the launch button."""
    from bookmark_studio.app import application as application_module

    shown = []
    monkeypatch.setattr(
        application_module.QMessageBox, "information", lambda *a, **k: shown.append(a) or None
    )

    adapter = MockPlaybackAdapter([])
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()

    app.prompt_vlc_launch_dialog()

    assert shown  # the "not available" message was shown
    assert app._adapter is adapter  # nothing was swapped


def test_mute_on_connect_false_never_touches_volume(qtbot, running_app) -> None:
    """Attaching to a VLC the user already had open (bootstrap: dialog cancelled) must
    never force their volume to 0 -- that's only for VLC instances this app spawned."""
    adapter = MockPlaybackAdapter(
        [VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0)]
    )
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe", mute_on_connect=False)
    app.start()

    qtbot.waitUntil(lambda: app._current_media_id is not None, timeout=3000)
    qtbot.wait(200)
    assert adapter._volume == 256  # untouched default
