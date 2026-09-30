"""Live tests of the in-app player (libVLC through python-vlc), silent output.

Opt in with VLC_BOOKMARK_STUDIO_LIVE_VLC=1; skipped when no libVLC of Python's bitness is found
(set VLC_BOOKMARK_STUDIO_LIBVLC_DIR to a VLC folder, e.g. a 64-bit portable VLC on Windows or an unpacked
Debian VLC in WSL).
"""
from __future__ import annotations

import time
import wave
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from bookmark_studio import platform_support as ps
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction, LoopState

pytestmark = [
    pytest.mark.live_vlc,
    pytest.mark.skipif(ps.env_setting("LIVE_VLC") != "1",
                       reason="set VLC_BOOKMARK_STUDIO_LIVE_VLC=1 to run live VLC tests"),
]

SONG_SECONDS = (12.3, 20.7)
SILENT = ["--aout=adummy", "--vout=dummy"]


def _require_libvlc() -> None:
    from bookmark_studio.playback.libvlc_loader import LibVlcUnavailable, load_vlc_module

    try:
        load_vlc_module(None)
    except LibVlcUnavailable as exc:
        pytest.skip(str(exc))


def _write_song(path: Path, seconds: float, frequency: float) -> Path:
    rate = 22050
    n = int(seconds * rate)
    samples = (np.sin(2 * np.pi * frequency * np.arange(n) / rate) * 0.2 * 32767).astype(np.int16)
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(samples.tobytes())
    return path


def _songs(tmp_path: Path) -> list[str]:
    return [
        str(_write_song(tmp_path / "song one.wav", SONG_SECONDS[0], 330.0)),
        str(_write_song(tmp_path / "song two.wav", SONG_SECONDS[1], 550.0)),
    ]


def _wait(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture()
def player(tmp_path):
    _require_libvlc()
    from bookmark_studio.playback.libvlc_adapter import LibVlcPlaybackAdapter

    adapter = LibVlcPlaybackAdapter(instance_args=SILENT)
    items = adapter.load(_songs(tmp_path))
    adapter.connect()
    yield adapter, items
    adapter.disconnect()


def test_live_libvlc_reports_exact_lengths(player) -> None:
    adapter, _items = player
    playlist = _wait(lambda: (lambda p: p if all(i.duration_s for i in p) else None)(adapter.get_playlist()))
    assert [i.name for i in playlist] == ["song one.wav", "song two.wav"]
    for item, seconds in zip(playlist, SONG_SECONDS):
        assert abs(item.duration_s - seconds) < 0.05  # the HTTP interface only knows whole seconds


def test_live_libvlc_switch_pause_and_seek_precisely(player) -> None:
    adapter, items = player
    adapter.goto_item(items[1].vlc_id)
    assert adapter.get_status().current_playlist_item_id == items[1].vlc_id
    adapter.pause()
    _wait(lambda: adapter.get_status().state == "paused", timeout=5)
    adapter.seek_absolute_us(18_250_000)
    _wait(lambda: abs(adapter.get_status().time_us - 18_250_000) < 60_000, timeout=5)
    adapter.play()
    _wait(lambda: adapter.get_status().state == "playing", timeout=5)
    before = adapter.get_status().time_us
    _wait(lambda: adapter.get_status().time_us > before + 200_000, timeout=5)
    adapter.seek_relative_us(-10_000_000)
    _wait(lambda: adapter.get_status().time_us < 10_000_000, timeout=5)
    adapter.set_volume(128)  # 0-512 scale, as over HTTP
    assert abs(adapter.get_status().volume - 128) <= 3
    adapter.goto_item(items[0].vlc_id)
    assert adapter.get_status().current_playlist_item_id == items[0].vlc_id
    adapter.stop()
    _wait(lambda: adapter.get_status().state == "stopped", timeout=5)


def test_live_libvlc_next_and_previous(player) -> None:
    adapter, items = player
    adapter.play()
    _wait(lambda: adapter.get_status().current_playlist_item_id == items[0].vlc_id, timeout=5)
    adapter.next_track()
    _wait(lambda: adapter.get_status().current_playlist_item_id == items[1].vlc_id, timeout=5)
    adapter.previous_track()
    _wait(lambda: adapter.get_status().current_playlist_item_id == items[0].vlc_id, timeout=5)


def test_live_in_app_player_loops_a_bookmark_in_another_song(qtbot, tmp_path) -> None:
    _require_libvlc()
    from bookmark_studio.app.application import Application
    from bookmark_studio.domain.bookmark import Bookmark
    from bookmark_studio.persistence.database import connect
    from bookmark_studio.persistence.migrations import migrate
    from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter

    conn = connect(tmp_path / "live.db")
    migrate(conn)
    app = Application(conn=conn, adapter=MockPlaybackAdapter([]), ffmpeg_path=ps.find_ffmpeg() or "ffmpeg",
                      waveform_cache_dir=tmp_path / "wf", libvlc_args=SILENT)
    qtbot.addWidget(app.window)
    try:
        app.start()
        app.open_in_app_player(_songs(tmp_path))
        qtbot.waitUntil(lambda: len(app.playlists.resolved) == 2
                        and app.playlists.synchronizer.active_playlist_id is not None, timeout=15000)
        (_first_item, _first_media), (second_item, second_media) = app.playlists.resolved
        segment = Bookmark(
            id=uuid4(), playlist_id=app.playlists.synchronizer.active_playlist_id, media_id=second_media.id,
            scope=BookmarkScope.PLAYLIST_MEDIA, lane_id=None, bookmark_type=BookmarkType.SEGMENT, name="riff",
            start_us=15_000_000, end_us=15_800_000, loop_enabled=True, repeat_count=2, loop_gap_ms=0,
            completion_action=CompletionAction.PAUSE,
        )
        app._bookmark_repository.insert(segment)
        passes = []
        app._loop_controller.iteration_changed.connect(passes.append)
        app._on_loop_bookmark_requested(segment.id)

        adapter = app.session.adapter
        qtbot.waitUntil(lambda: adapter.get_status().current_playlist_item_id == second_item.vlc_id, timeout=5000)
        samples = []

        def inside_segment() -> bool:
            status = adapter.get_status()
            samples.append(status.time_us)
            return status.state == "playing" and 14_900_000 <= status.time_us <= 16_000_000

        qtbot.waitUntil(inside_segment, timeout=5000)
        qtbot.waitUntil(lambda: app._loop_controller.state in (LoopState.COMPLETED, LoopState.IDLE), timeout=10000)
        qtbot.waitUntil(lambda: adapter.get_status().state == "paused", timeout=5000)
        status = adapter.get_status()
        assert status.current_playlist_item_id == second_item.vlc_id
        # Paused at the end of the second pass, never far past the segment.
        assert 15_000_000 <= status.time_us <= 16_300_000, samples[-10:]
        assert passes, "the loop never repeated"
    finally:
        app.stop()
        conn.close()
