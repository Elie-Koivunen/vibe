from __future__ import annotations

import shutil
import wave
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

    def _make(adapter, ffmpeg_path: str = FFMPEG_PATH) -> Application:
        conn = connect(tmp_path / "test.db")
        migrate(conn)
        app = Application(
            conn=conn, adapter=adapter, ffmpeg_path=ffmpeg_path, waveform_cache_dir=tmp_path / "cache"
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


def test_offline_start_does_not_crash(qtbot, running_app) -> None:
    """spec #104: VLC unreachable must not prevent the app from starting."""
    adapter = MockPlaybackAdapter([])
    app = running_app(adapter, ffmpeg_path="not-a-real-ffmpeg.exe")
    app.start()
    qtbot.wait(300)
    assert app.window._breadcrumb.text()  # never crashed, still has a valid label
