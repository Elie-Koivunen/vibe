"""Live tests against a real, locally installed VLC (headless, silent audio output).

Opt in with VLC_BOOKMARK_STUDIO_LIVE_VLC=1. Each test launches its own VLC on a free port with
``-I dummy --aout=adummy`` (no window, no sound) and closes it afterwards. Works on
Windows, Linux, and in WSL (where it drives the Windows vlc.exe through interop when no
Linux VLC is installed).
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import wave
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from bookmark_studio import platform_support as ps
from bookmark_studio.app.vlc_launcher import find_free_http_port, launch_managed_vlc
from bookmark_studio.domain.enums import CompletionAction, LoopState
from bookmark_studio.domain.loop import LoopSpec
from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter

pytestmark = [
    pytest.mark.live_vlc,
    pytest.mark.skipif(ps.env_setting("LIVE_VLC") != "1",
                       reason="set VLC_BOOKMARK_STUDIO_LIVE_VLC=1 to run live VLC tests"),
]

PASSWORD = "vlc-bookmark-studio-live-test"
LUA_TOKEN = "vlc-bookmark-studio-lua-live-test"
SONG_SECONDS = (12.3, 20.7)  # deliberately not whole seconds


def _vlc() -> str:
    path = ps.env_setting("VLC") or ps.find_vlc()
    if not path:
        pytest.skip("VLC is not installed")
    return path


def _headless_args(vlc: str) -> list[str]:
    args = ["-I", "dummy", "--aout=adummy", "--no-video"]
    if ps.is_windows_executable(vlc):
        args.append("--dummy-quiet")  # Windows-only option: no console window for the dummy interface
    return args


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


def _wait(predicate, timeout: float = 10.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - VLC still starting
            last_error = exc
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s (last error: {last_error})")


class LiveVlc:
    def __init__(self, tmp_path: Path, *, lua_intf: str | None = None, env: dict | None = None,
                 extra_args: list[str] | None = None) -> None:
        self.vlc = _vlc()
        self.songs = [
            _write_song(tmp_path / "song one.wav", SONG_SECONDS[0], 330.0),
            _write_song(tmp_path / "song two.wav", SONG_SECONDS[1], 550.0),
        ]
        bind_host, self.host = ps.vlc_http_hosts(self.vlc)
        windows_vlc_from_wsl = ps.is_wsl() and ps.is_windows_executable(self.vlc)
        self.port = find_free_http_port(
            47_300 + (os.getpid() % 500), connect_host=self.host,
            bind_host=None if windows_vlc_from_wsl else bind_host,
        )
        old_env = {}
        for key, value in (env or {}).items():
            old_env[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            if lua_intf is None:
                self.process = launch_managed_vlc(
                    self.vlc, [str(s) for s in self.songs], http_port=self.port, http_password=PASSWORD,
                    http_host=bind_host, extra_args=_headless_args(self.vlc) + list(extra_args or []),
                )
            else:
                from bookmark_studio.app.vlc_launcher import launch_managed_vlc_with_lua_bridge

                self.process = launch_managed_vlc_with_lua_bridge(
                    self.vlc, [str(s) for s in self.songs], http_port=self.port, token=LUA_TOKEN, lua_intf=lua_intf,
                    http_host=bind_host, extra_args=_headless_args(self.vlc) + list(extra_args or []),
                )
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def adapter(self) -> StandardHttpPlaybackAdapter:
        adapter = StandardHttpPlaybackAdapter(self.host, self.port, PASSWORD)
        _wait(lambda: adapter.connect() or True, timeout=15)
        return adapter

    def close(self) -> None:
        from bookmark_studio.app.vlc_launcher import terminate_managed_vlc

        # Also reaches the Windows vlc.exe when driven from WSL (see terminate_managed_vlc).
        terminate_managed_vlc(self.process, self.vlc, self.port)


@pytest.fixture()
def live(tmp_path):
    vlc = LiveVlc(tmp_path)
    yield vlc
    vlc.close()


def _playlist_ready(adapter):
    items = adapter.get_playlist()
    return items if len(items) == 2 and all(i.duration_s for i in items) else None


def test_live_switch_song_and_seek_precisely(live) -> None:
    adapter = live.adapter()
    items = _wait(lambda: _playlist_ready(adapter), timeout=15)
    assert all(i.duration_s and i.duration_s > 0 for i in items)

    second = items[1]
    adapter.goto_item(second.vlc_id)  # waits until VLC has actually switched
    assert adapter.get_status().current_playlist_item_id == second.vlc_id
    adapter.pause()
    _wait(lambda: adapter.get_status().state == "paused", timeout=5)

    # VLC reports whole seconds (20 for a 20.7 s file); percent seeks computed from that
    # land late. With the exact decoded length they land where asked.
    adapter.set_exact_duration(second.vlc_id, int(SONG_SECONDS[1] * 1_000_000))
    adapter.seek_absolute_us(18_000_000)
    time.sleep(0.5)
    position = float(adapter._status_json()["position"])
    assert abs(position * SONG_SECONDS[1] - 18.0) < 0.15
    status = adapter.get_status()
    assert abs(status.time_us - 18_000_000) < 150_000


def test_live_loop_with_gap_fade_and_completion(live, qtbot) -> None:
    from bookmark_studio.playback.command_queue import ThreadedCommandQueue
    from bookmark_studio.playback.loop_controller import LoopController
    from bookmark_studio.playback.playback_clock import PlaybackClock

    adapter = live.adapter()
    items = _wait(lambda: _playlist_ready(adapter), timeout=15)
    adapter.set_exact_duration(items[0].vlc_id, int(SONG_SECONDS[0] * 1_000_000))
    # The silent dummy audio output has no volume control (VLC reports 0 whatever is
    # set), so check the volume commands this app sends instead of VLC's readback.
    volumes_sent: list[int] = []
    real_set_volume = adapter.set_volume

    def recording_set_volume(level: int) -> None:
        volumes_sent.append(level)
        real_set_volume(level)

    adapter.set_volume = recording_set_volume  # type: ignore[method-assign]
    queue = ThreadedCommandQueue()
    controller = LoopController(adapter, PlaybackClock(), executor=queue)
    controller.set_target_volume(200)
    iterations, completions, gaps = [], [], []
    controller.iteration_changed.connect(lambda r: iterations.append(r))
    controller.loop_completed.connect(lambda a: completions.append(a))
    controller.gap_started.connect(lambda ms: gaps.append(ms))

    samples: list[tuple[str, int]] = []
    stop_sampling = threading.Event()
    sampler_adapter = StandardHttpPlaybackAdapter(live.host, live.port, PASSWORD)
    sampler_adapter.set_exact_duration(items[0].vlc_id, int(SONG_SECONDS[0] * 1_000_000))

    def sample() -> None:
        while not stop_sampling.is_set():
            try:
                status = sampler_adapter.get_status()
                samples.append((status.state, status.time_us))
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.05)

    sampler = threading.Thread(target=sample, daemon=True)
    first_id = items[0].vlc_id
    controller.start(
        LoopSpec(start_us=2_000_000, end_us=2_800_000, repeat_count=3, gap_ms=300,
                 completion_action=CompletionAction.PAUSE, fade_out_ms=200),
        before=lambda: adapter.goto_item(first_id),
    )
    qtbot.waitUntil(lambda: controller.state is LoopState.PLAYING, timeout=5000)
    sampler.start()
    qtbot.waitUntil(lambda: completions == [CompletionAction.PAUSE], timeout=15000)
    stop_sampling.set()
    sampler.join(timeout=2)

    assert len(iterations) == 2 and len(gaps) == 2  # 3 passes: 2 restarts, each after a gap
    qtbot.waitUntil(lambda: adapter.get_status().state == "paused", timeout=3000)
    qtbot.waitUntil(lambda: queue.is_idle(), timeout=3000)
    assert min(volumes_sent) < 100  # the fade-out ducked the volume...
    assert volumes_sent[-1] == 200  # ...and it was restored when the loop finished
    playing_times = [t for state, t in samples if state == "playing"]
    assert playing_times, "never observed playback"
    assert max(playing_times) < 2_800_000 + 600_000
    assert min(playing_times) > 2_000_000 - 400_000
    queue.shutdown()


def test_live_password_is_read_from_the_private_config_not_the_command_line(live) -> None:
    """VLC's HTTP interface refuses every request while no password is set, so a working
    connection proves the password arrived through the --config file; and it must not be
    visible to other users in the process list."""
    assert not any(PASSWORD in str(arg) for arg in live.process.args)
    adapter = live.adapter()
    assert adapter.get_status() is not None
    intruder = StandardHttpPlaybackAdapter(live.host, live.port, "wrong-password")
    with pytest.raises(Exception):
        intruder.connect()
        intruder.get_status()
    intruder.disconnect()


def test_live_port_probe_skips_vlcs_port(live) -> None:
    live.adapter()
    bind_host, connect_host = ps.vlc_http_hosts(live.vlc)
    windows_vlc_from_wsl = ps.is_wsl() and ps.is_windows_executable(live.vlc)
    assert find_free_http_port(
        live.port, connect_host=connect_host, bind_host=None if windows_vlc_from_wsl else bind_host
    ) != live.port


def test_live_application_plays_a_bookmark_in_another_song(live, qtbot, tmp_path) -> None:
    from bookmark_studio.app.application import Application
    from bookmark_studio.domain.bookmark import Bookmark
    from bookmark_studio.domain.enums import BookmarkScope, BookmarkType
    from bookmark_studio.persistence.database import connect
    from bookmark_studio.persistence.migrations import migrate

    ffmpeg = ps.find_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed")
    adapter = live.adapter()
    _wait(lambda: _playlist_ready(adapter), timeout=15)
    conn = connect(tmp_path / "live.db")
    migrate(conn)
    app = Application(conn=conn, adapter=adapter, ffmpeg_path=ffmpeg, waveform_cache_dir=tmp_path / "wf")
    qtbot.addWidget(app.window)
    try:
        app.start()
        qtbot.waitUntil(lambda: len(app.playlists.resolved) == 2 and app.playlists.synchronizer.active_playlist_id is not None,
                        timeout=15000)
        # Both songs decoded in the background: exact lengths known for both.
        qtbot.waitUntil(lambda: len(app._exact_duration_by_media) == 2, timeout=30000)
        exact = sorted(app._exact_duration_by_media.values())
        assert abs(exact[0] - SONG_SECONDS[0] * 1_000_000) < 20_000
        assert abs(exact[1] - SONG_SECONDS[1] * 1_000_000) < 20_000

        (first_item, first_media), (second_item, second_media) = app.playlists.resolved
        playlist_id = app.playlists.synchronizer.active_playlist_id
        point = Bookmark(
            id=uuid4(), playlist_id=playlist_id, media_id=second_media.id, scope=BookmarkScope.PLAYLIST_MEDIA,
            lane_id=None, bookmark_type=BookmarkType.POINT, name="verse", start_us=15_500_000, end_us=None,
            loop_enabled=False, repeat_count=None, loop_gap_ms=0, completion_action=CompletionAction.CONTINUE,
        )
        app._bookmark_repository.insert(point)
        app._on_play_bookmark_requested(point.id)

        seen: list[tuple] = []

        def landed() -> bool:
            status = adapter.get_status()
            seen.append((status.current_playlist_item_id, status.state, status.time_us))
            return (status.current_playlist_item_id == second_item.vlc_id and status.state == "playing"
                    and 15_300_000 <= status.time_us <= 16_600_000)

        deadline = time.monotonic() + 6
        while not landed():
            assert time.monotonic() < deadline, f"never landed at 15.5 s in song 2; VLC reported {seen[-12:]}"
            qtbot.wait(20)

        segment = Bookmark(
            id=uuid4(), playlist_id=playlist_id, media_id=first_media.id, scope=BookmarkScope.PLAYLIST_MEDIA,
            lane_id=None, bookmark_type=BookmarkType.SEGMENT, name="riff", start_us=1_000_000, end_us=1_700_000,
            loop_enabled=True, repeat_count=2, loop_gap_ms=0, completion_action=CompletionAction.PAUSE,
        )
        app._bookmark_repository.insert(segment)
        completions = []
        app._loop_controller.loop_completed.connect(lambda a: completions.append(a))
        app._on_play_bookmark_requested(segment.id)  # loop-enabled: loops, then pauses
        qtbot.waitUntil(lambda: completions == [CompletionAction.PAUSE], timeout=10000)
        qtbot.waitUntil(lambda: adapter.get_status().state == "paused", timeout=3000)
        status = adapter.get_status()
        assert status.current_playlist_item_id == first_item.vlc_id
        assert 1_300_000 <= status.time_us <= 2_500_000
    finally:
        app.stop()
        conn.close()


def test_live_lua_bridge(tmp_path, qtbot) -> None:
    """The opt-in Lua bridge, loaded from a private data dir (VLC_DATA_PATH) under a test
    name, so nothing is installed into the user's own VLC profile."""
    from bookmark_studio.playback.bridge_client import BridgeClient

    if ps.is_wsl() and ps.is_windows_executable(_vlc()):
        pytest.skip("VLC_DATA_PATH does not reach a Windows vlc.exe started from WSL")

    data_dir = tmp_path / "vlcdata"
    intf_dir = data_dir / "lua" / "intf"
    intf_dir.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "vlc" / "bookmarkstudio.lua"
    shutil.copyfile(source, intf_dir / "vlcbs_livetest.lua")
    live = LiveVlc(tmp_path, lua_intf="vlcbs_livetest", env={"VLC_DATA_PATH": str(data_dir)})
    assert not any(LUA_TOKEN in str(arg) for arg in live.process.args)
    from bookmark_studio.playback.enhanced_adapter import EnhancedLuaPlaybackAdapter

    try:
        client = BridgeClient(live.host, live.port, LUA_TOKEN)  # one persistent connection, as in the app
        adapter = EnhancedLuaPlaybackAdapter(client)
        _wait(lambda: adapter.connect() or True, timeout=15)  # token from the private config file
        items = _wait(lambda: (lambda i: i if len(i) == 2 else None)(adapter.get_playlist()), timeout=15)
        assert "volume" in client.status()  # status now reports volume (fades used to assume 100%)
        adapter.set_volume(128)  # the new command (was rejected before 0.2.0)
        adapter.goto_item(items[1].vlc_id)  # waits until VLC has switched
        assert adapter.get_status().current_playlist_item_id == items[1].vlc_id
        adapter.pause()
        _wait(lambda: adapter.get_status().state == "paused", timeout=5)
        adapter.seek_absolute_us(7_250_000)
        _wait(lambda: abs(adapter.get_status().time_us - 7_250_000) < 50_000, timeout=5)  # microsecond seek
        # Many requests on the same connection: before 0.2.0 the script let Lua's garbage
        # collector unregister its handlers, and VLC answered 404 on random routes.
        for _ in range(30):
            adapter.get_status()
            adapter.get_playlist()
        adapter.disconnect()
    finally:
        live.close()


def _vlc_equalizer(adapter: StandardHttpPlaybackAdapter) -> tuple[float, list[float]] | None:
    """What a VLC window reports: status.json's "equalizer" is empty while it is off."""
    eq = adapter._status_json().get("equalizer")
    if not eq:
        return None
    bands = {int(key.split('"')[1]): value for key, value in eq["bands"].items()}
    return float(eq["preamp"]), [float(bands[i]) for i in range(10)]


def test_live_equalizer_over_http(live) -> None:
    """0.6.0: the Volume & EQ tab drives a VLC window's equalizer, and it sticks across
    song changes."""
    from bookmark_studio.domain.equalizer import EqualizerSettings

    adapter = live.adapter()
    items = _wait(lambda: _playlist_ready(adapter), timeout=15)
    rock = EqualizerSettings.from_preset("Rock")
    adapter.set_equalizer(rock)
    assert _wait(lambda: _vlc_equalizer(adapter)) == (rock.preamp_db, list(rock.bands_db))
    tweaked = rock.with_band(4, -6.5).with_preamp(9.0)
    adapter.set_equalizer(tweaked)  # only the changes are sent
    assert _wait(lambda: _vlc_equalizer(adapter) == (9.0, list(tweaked.bands_db)))
    adapter.goto_item(items[1].vlc_id)
    assert _vlc_equalizer(adapter) == (9.0, list(tweaked.bands_db))
    adapter.set_equalizer(tweaked.with_enabled(False))
    assert _wait(lambda: _vlc_equalizer(adapter) is None)


@pytest.mark.parametrize("how", ["selection", "play"])
def test_live_a_bookmark_made_while_its_selection_plays_is_green(live, qtbot, tmp_path, how) -> None:
    """0.9.0, reported from a real VLC: after Bookmark selection the new bookmark wasn't
    green. With ▶ Selection the bookmark takes the selection's loop over; with the main
    Play (the song simply playing through it) it is the bookmark playing."""
    from bookmark_studio.app.application import Application
    from bookmark_studio.domain.selection import Selection
    from bookmark_studio.persistence.database import connect
    from bookmark_studio.persistence.migrations import migrate

    adapter = live.adapter()
    _wait(lambda: _playlist_ready(adapter), timeout=15)
    conn = connect(tmp_path / "live.db")
    migrate(conn)
    app = Application(conn=conn, adapter=adapter, ffmpeg_path=ps.find_ffmpeg() or "/nonexistent",
                      waveform_cache_dir=tmp_path / "wf")
    qtbot.addWidget(app.window)
    try:
        app.start()
        qtbot.waitUntil(lambda: len(app.playlists.resolved) == 2
                        and app.playlists.synchronizer.active_playlist_id is not None, timeout=15000)
        # The song on screen settles first (its first status may switch it, clearing a
        # selection drawn before).
        qtbot.waitUntil(lambda: app._current_vlc_item_id is not None and app.window.waveform_duration_us() > 0,
                        timeout=15000)
        qtbot.wait(1000)
        window = app.window
        window._waveform_scene.set_selection(Selection(start_us=2_000_000, end_us=6_000_000))
        if how == "selection":
            window._loop_selection_button.click()
        else:
            app._seek_displayed(2_500_000)
            window._transport.play_pause_button.click()
        qtbot.waitUntil(lambda: adapter.get_status().state == "playing"
                        and 2_000_000 <= adapter.get_status().time_us < 4_000_000
                        and app._actually_playing_vlc_item_id == app._current_vlc_item_id, timeout=8000)
        assert window._waveform_scene.selection() is not None
        window._bookmark_selection_button.click()
        bookmark = window._inspector.current_bookmark()
        assert bookmark is not None
        for _ in range(5):  # and it stays green while it plays
            assert app._bookmark_playback is not None and app._bookmark_playback.bookmark_id == bookmark.id
            assert app._bookmark_playback.state == "playing"
            assert window._waveform_scene.bookmark_item(bookmark.id).playback_state() == "playing"
            assert window._bookmark_panel._playback == (bookmark.id, "playing")
            assert window._playlist_panel._bookmark_song == (app._current_vlc_item_id, "playing")
            qtbot.wait(150)
        assert app._active_loop_bookmark_id == bookmark.id  # it loops on, as the bookmark
        # ... counting its passes: at its end it goes back to its start (the pass playing
        # when it was saved was its first)
        qtbot.waitUntil(lambda: app._loop_controller._passes_done >= 1, timeout=8000)
        qtbot.waitUntil(lambda: 2_000_000 <= adapter.get_status().time_us < 4_500_000, timeout=3000)
        assert adapter.get_status().state == "playing"
    finally:
        app.stop()
        conn.close()


def test_live_play_plays_the_song_picked_in_the_playlist(live, qtbot, tmp_path) -> None:
    """0.9.0, reported from a real VLC: a song picked in the playlist (a single click shows
    it) and Play -- the red line didn't move: Play resumed VLC's own song."""
    from bookmark_studio.app.application import Application
    from bookmark_studio.persistence.database import connect
    from bookmark_studio.persistence.migrations import migrate

    adapter = live.adapter()
    items = _wait(lambda: _playlist_ready(adapter), timeout=15)
    conn = connect(tmp_path / "live.db")
    migrate(conn)
    app = Application(conn=conn, adapter=adapter, ffmpeg_path=ps.find_ffmpeg() or "/nonexistent",
                      waveform_cache_dir=tmp_path / "wf")
    qtbot.addWidget(app.window)
    try:
        app.start()
        qtbot.waitUntil(lambda: len(app.playlists.resolved) == 2 and app._actually_playing_vlc_item_id is not None,
                        timeout=15000)
        qtbot.wait(1000)
        other = next(item for item in items if item.vlc_id != app._actually_playing_vlc_item_id)
        app._on_playlist_item_selected(other.vlc_id)  # the single click
        assert app._current_vlc_item_id == other.vlc_id
        app.window._transport.play_pause_button.click()
        qtbot.waitUntil(lambda: adapter.get_status().current_playlist_item_id == other.vlc_id
                        and adapter.get_status().state == "playing", timeout=8000)
        start = app.window.playhead_time_us()
        qtbot.waitUntil(lambda: app.window.playhead_time_us() >= start + 700_000, timeout=5000)  # the line moves
    finally:
        app.stop()
        conn.close()


def test_live_tempo_over_http(live, qtbot, tmp_path) -> None:
    """0.9.0's Tempo fader through VLC's HTTP interface: +20 BPM on a 100 BPM song plays
    it at 1.2x; quitting puts VLC back at its normal speed."""
    from bookmark_studio.app.application import Application
    from bookmark_studio.persistence.database import connect
    from bookmark_studio.persistence.migrations import migrate

    adapter = live.adapter()
    _wait(lambda: _playlist_ready(adapter), timeout=15)
    conn = connect(tmp_path / "live.db")
    migrate(conn)
    app = Application(conn=conn, adapter=adapter, ffmpeg_path=ps.find_ffmpeg() or "/nonexistent",
                      waveform_cache_dir=tmp_path / "wf")
    qtbot.addWidget(app.window)
    try:
        app.start()
        qtbot.waitUntil(lambda: app._current_media_id is not None and app._actually_playing_vlc_item_id is not None,
                        timeout=15000)
        app.window._transport.play_pause_button.click()
        qtbot.waitUntil(lambda: adapter.get_status().state == "playing", timeout=8000)
        app.window._volume_eq.tempo_panel.switch.click()  # BPM on
        app._on_detected_bpm_corrected(100.0)  # the song's BPM, as the user knows it
        app.window._volume_eq.tempo_panel.fader._set_by_user(20)
        qtbot.waitUntil(lambda: abs(adapter.get_status().rate - 1.2) < 0.01, timeout=5000)
        start_time, start_clock = adapter.get_status().time_us, time.monotonic()
        qtbot.wait(1500)
        advanced = (adapter.get_status().time_us - start_time) / ((time.monotonic() - start_clock) * 1_000_000)
        assert 0.9 < advanced < 1.6, advanced  # (VLC reports whole seconds over HTTP: coarse)
    finally:
        app.stop()
        conn.close()
    assert abs(adapter.get_status().rate - 1.0) < 0.01


def _loudness_windows(path: Path, window_s: float = 0.010) -> np.ndarray:
    """RMS of each `window_s` of a WAV file, over the part with sound (edges trimmed)."""
    with wave.open(str(path)) as f:
        rate, channels, width = f.getframerate(), f.getnchannels(), f.getsampwidth()
        raw = f.readframes(f.getnframes())
    samples = np.frombuffer(raw, dtype={2: np.int16, 4: np.int32}[width]).astype(np.float64)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    size = int(rate * window_s)
    rms = np.sqrt((samples[: len(samples) // size * size].reshape(-1, size) ** 2).mean(axis=1))
    loud = rms > rms.max() * 0.2
    first, last = int(np.argmax(loud)), len(loud) - int(np.argmax(loud[::-1]))
    return rms[first + 5:last - 5]


def test_live_a_tempo_glide_plays_on_without_gaps(qtbot, tmp_path) -> None:
    """0.9.0's Tempo buttons glide the rate in small steps: VLC's output (written to a file
    here) has no silent stretch, nor more than a passing dip, all the way up to 1.5x and
    back."""
    from bookmark_studio.app.application import Application
    from bookmark_studio.persistence.database import connect
    from bookmark_studio.persistence.migrations import migrate
    from bookmark_studio.settings.settings_service import TempoButtons

    if ps.is_wsl() and ps.is_windows_executable(_vlc()):
        pytest.skip("a Windows vlc.exe started from WSL can't write its output into WSL's temporary folder")
    out = tmp_path / "out.wav"
    live = LiveVlc(tmp_path, extra_args=["--aout=afile", f"--audiofile-file={out}", "--audiofile-wav"])
    adapter = live.adapter()
    _wait(lambda: _playlist_ready(adapter), timeout=15)
    conn = connect(tmp_path / "live.db")
    migrate(conn)
    app = Application(conn=conn, adapter=adapter, ffmpeg_path=ps.find_ffmpeg() or "/nonexistent",
                      waveform_cache_dir=tmp_path / "wf")
    qtbot.addWidget(app.window)
    try:
        app.start()
        qtbot.waitUntil(lambda: app._current_media_id is not None and app._actually_playing_vlc_item_id is not None,
                        timeout=15000)
        app.window._transport.play_pause_button.click()
        qtbot.waitUntil(lambda: adapter.get_status().state == "playing", timeout=8000)
        qtbot.wait(1500)
        app.window._volume_eq.tempo_panel.switch.click()  # BPM on
        app._on_detected_bpm_corrected(100.0)
        app._on_tempo_buttons_changed(TempoButtons(step_bpm=50, glide_ms=2000))
        app.window._volume_eq.tempo_panel.increase_button.click()  # 100 -> 150 BPM: 1.0x -> 1.5x over 2 s
        qtbot.wait(3000)
        assert abs(adapter.get_status().rate - 1.5) < 0.01
        app.window._volume_eq.tempo_panel.reset_button.click()  # and back
        qtbot.wait(3000)
        assert abs(adapter.get_status().rate - 1.0) < 0.01
        adapter.stop()
        qtbot.wait(500)
    finally:
        app.stop()
        conn.close()
        live.close()
    windows = _loudness_windows(out)
    median = float(np.median(windows))
    assert windows.size > 500  # several seconds of sound
    assert float(windows.min()) > median * 0.5  # no gap, no deep dip anywhere
    assert int((windows < median * 0.9).sum()) <= 3  # at most a passing dip or two
