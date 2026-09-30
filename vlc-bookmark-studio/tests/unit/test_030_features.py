"""0.3.0: command line, self-test, libVLC discovery, in-app choice in the launch dialog,
streamed waveform peaks, and loops stopping when playback is changed in VLC's own window."""
from __future__ import annotations

import io
import json
import struct
import wave
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from bookmark_studio import bootstrap, platform_support
from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction, LoopState
from bookmark_studio.playback import libvlc_loader
from bookmark_studio.playback.status import VlcPlaylistItem
from bookmark_studio.waveform.peaks import PeakAccumulator, compute_peaks, reduce_peaks
from bookmark_studio.waveform.pyramid import build_pyramid, build_pyramid_from_peaks

# -- command line --


def _parse(*argv: str):
    return bootstrap.build_arg_parser().parse_args(list(argv))


def test_attach_accepts_port_or_host_and_port() -> None:
    assert _parse("--attach", "43119").attach == (None, 43119)
    assert _parse("--attach", "172.26.112.1:8080").attach == ("172.26.112.1", 8080)
    with pytest.raises(SystemExit):
        _parse("--attach", "vlc")
    with pytest.raises(SystemExit):
        _parse("--attach", "70000")


def test_defaults_open_nothing_and_show_the_dialog() -> None:
    args = _parse()
    assert args.media == [] and args.playlist is None and args.attach is None
    assert args.adapter == "auto" and not args.no_dialog and not args.self_test


class _Settings:
    def __init__(self, backend: str = "http") -> None:
        self.backend = backend

    def playback_backend(self) -> str:
        return self.backend


def _m3u(tmp_path: Path) -> Path:
    songs = [tmp_path / "a.mp3", tmp_path / "b.mp3"]
    for song in songs:
        song.write_bytes(b"")
    playlist = tmp_path / "mix.m3u"
    playlist.write_text("#EXTM3U\n" + "\n".join(s.name for s in songs) + "\n", encoding="utf-8")
    return playlist


def test_startup_choice_for_a_playlist_follows_the_adapter_option(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(libvlc_loader, "libvlc_available", lambda _dir=None: True)
    playlist = _m3u(tmp_path)

    choice = bootstrap._startup_choice(_parse("--playlist", str(playlist), "--adapter", "libvlc"),
                                       _Settings(), "vlc", None)
    assert choice.mode == "in_app"
    assert [Path(p).name for p in choice.media_paths] == ["a.mp3", "b.mp3"]
    assert choice.source_uri == platform_support.path_to_uri(playlist)

    choice = bootstrap._startup_choice(_parse(str(playlist), "--adapter", "http"), _Settings(), "vlc", None)
    assert choice.mode == "launch" and len(choice.media_paths) == 2


def test_auto_adapter_uses_the_last_choice_when_it_is_available(tmp_path: Path, monkeypatch) -> None:
    song = tmp_path / "a.mp3"
    song.write_bytes(b"")
    monkeypatch.setattr(libvlc_loader, "libvlc_available", lambda _dir=None: True)
    assert bootstrap._startup_choice(_parse(str(song)), _Settings("libvlc"), "vlc", None).mode == "in_app"
    assert bootstrap._startup_choice(_parse(str(song)), _Settings("http"), "vlc", None).mode == "launch"
    assert bootstrap._startup_choice(_parse(str(song)), _Settings("http"), None, None).mode == "in_app"
    monkeypatch.setattr(libvlc_loader, "libvlc_available", lambda _dir=None: False)
    assert bootstrap._startup_choice(_parse(str(song)), _Settings("libvlc"), "vlc", None).mode == "launch"


def test_attach_without_host_uses_the_address_vlc_answers_on(monkeypatch) -> None:
    monkeypatch.setattr(platform_support, "vlc_http_hosts", lambda _vlc: ("0.0.0.0", "172.26.112.1"))
    choice = bootstrap._startup_choice(_parse("--attach", "43119"), _Settings(), "/mnt/c/vlc.exe", None)
    assert (choice.mode, choice.host, choice.port) == ("attach", "172.26.112.1", 43119)
    choice = bootstrap._startup_choice(_parse("--attach", "43119"), _Settings(), None, None)
    assert choice.host == "127.0.0.1"


def test_version_option(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        bootstrap.main(["vlc-bookmark-studio", "--version"])
    assert exit_info.value.code == 0
    from bookmark_studio import __version__

    assert __version__ in capsys.readouterr().out


def test_self_test_with_data_dir_writes_a_report(tmp_path: Path, monkeypatch, qapp) -> None:
    data_dir = tmp_path / "portable data"
    report = tmp_path / "report.json"
    code = bootstrap.main(["vlc-bookmark-studio", "--data-dir", str(data_dir), "--self-test",
                           "--self-test-report", str(report)])
    content = json.loads(report.read_text(encoding="utf-8"))
    assert code == 0, content
    assert content["passed"] is True
    names = {check["name"]: check for check in content["checks"]}
    assert {"environment", "qt", "database", "sync", "ffmpeg", "vlc", "libvlc"} <= set(names)
    assert str(data_dir.resolve()) in names["environment"]["detail"]


def test_self_test_fails_when_a_required_tool_is_missing(monkeypatch, qapp) -> None:
    from bookmark_studio.selftest import run_self_test

    monkeypatch.setattr(platform_support, "find_ffmpeg", lambda _saved=None: None)
    out = io.StringIO()
    assert run_self_test(require=("ffmpeg",), out=out) == 1
    assert "required but ffmpeg not found" in out.getvalue()
    out = io.StringIO()
    assert run_self_test(out=out) == 0  # missing but optional


# -- libVLC discovery --


def _fake_pe(path: Path, machine: int) -> Path:
    header = bytearray(0x200)
    header[0:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x80)
    header[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", header, 0x84, machine)
    path.write_bytes(bytes(header))
    return path


def test_pe_bits_reads_the_dll_architecture(tmp_path: Path) -> None:
    assert libvlc_loader.pe_bits(_fake_pe(tmp_path / "x86.dll", 0x014C)) == 32
    assert libvlc_loader.pe_bits(_fake_pe(tmp_path / "x64.dll", 0x8664)) == 64
    (tmp_path / "junk.dll").write_bytes(b"not a dll")
    assert libvlc_loader.pe_bits(tmp_path / "junk.dll") is None
    assert libvlc_loader.pe_bits(tmp_path / "missing.dll") is None


@pytest.fixture()
def only_explicit_dirs(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("VLC_BOOKMARK_STUDIO_LIBVLC_DIR", raising=False)
    monkeypatch.delenv("BM4VLC_LIBVLC_DIR", raising=False)
    monkeypatch.setattr(platform_support, "bundled_resource_dir", lambda: tmp_path / "no-bundle")
    monkeypatch.setattr(libvlc_loader, "_windows_candidate_dirs", lambda: [])
    monkeypatch.setattr(libvlc_loader.ctypes.util, "find_library", lambda _name: None)


def test_a_32_bit_vlc_is_explained_not_loaded(tmp_path: Path, monkeypatch, only_explicit_dirs) -> None:
    monkeypatch.setattr(platform_support, "IS_WINDOWS", True)
    monkeypatch.setattr(libvlc_loader, "python_bits", lambda: 64)
    vlc_dir = tmp_path / "VLC32"
    vlc_dir.mkdir()
    _fake_pe(vlc_dir / "libvlc.dll", 0x014C)
    with pytest.raises(libvlc_loader.LibVlcUnavailable, match="32-bit") as info:
        libvlc_loader.find_libvlc(str(vlc_dir))
    assert "64-bit VLC" in str(info.value)
    assert str(info.value).count("libvlc.dll") == 1  # listed once, not per candidate


def test_a_matching_windows_vlc_is_found_with_its_plugins(tmp_path: Path, monkeypatch, only_explicit_dirs) -> None:
    monkeypatch.setattr(platform_support, "IS_WINDOWS", True)
    monkeypatch.setattr(libvlc_loader, "python_bits", lambda: 64)
    vlc_dir = tmp_path / "VLC64"
    (vlc_dir / "plugins").mkdir(parents=True)
    _fake_pe(vlc_dir / "libvlc.dll", 0x8664)
    location = libvlc_loader.find_libvlc(str(vlc_dir))
    assert Path(location.library) == vlc_dir / "libvlc.dll"
    assert Path(location.plugins) == vlc_dir / "plugins"
    assert location.source == "configured libVLC directory"


def test_an_unpacked_debian_vlc_is_found(tmp_path: Path, monkeypatch, only_explicit_dirs) -> None:
    """The layout `apt-get download vlc ...; dpkg -x` produces (used for live tests
    without root)."""
    monkeypatch.setattr(platform_support, "IS_WINDOWS", False)
    lib_dir = tmp_path / "vlc-deb" / "usr" / "lib" / "x86_64-linux-gnu"
    (lib_dir / "vlc" / "plugins").mkdir(parents=True)
    (lib_dir / "libvlc.so.5").write_bytes(b"")
    location = libvlc_loader.find_libvlc(str(tmp_path / "vlc-deb"))
    assert Path(location.library) == lib_dir / "libvlc.so.5"
    assert Path(location.plugins) == lib_dir / "vlc" / "plugins"


def test_nothing_found_says_how_to_get_libvlc(monkeypatch, only_explicit_dirs) -> None:
    monkeypatch.setattr(platform_support, "IS_WINDOWS", False)
    with pytest.raises(libvlc_loader.LibVlcUnavailable, match="apt install vlc"):
        libvlc_loader.find_libvlc(None)


# -- launch dialog --


def test_dialog_offers_the_in_app_player(qtbot) -> None:
    from bookmark_studio.ui.dialogs.vlc_launch_dialog import VlcLaunchDialog

    only_in_app = VlcLaunchDialog([], "All files (*)", can_launch_vlc=False, can_play_in_app=True)
    qtbot.addWidget(only_in_app)
    assert only_in_app.choice().mode == "in_app"

    preferred = VlcLaunchDialog([], "All files (*)", can_launch_vlc=True, can_play_in_app=True, prefer_in_app=True)
    qtbot.addWidget(preferred)
    assert preferred.choice().mode == "in_app"

    default = VlcLaunchDialog([], "All files (*)", can_launch_vlc=True, can_play_in_app=True)
    qtbot.addWidget(default)
    assert default.choice().mode == "launch"

    no_libvlc = VlcLaunchDialog([], "All files (*)", can_launch_vlc=True, can_play_in_app=False, prefer_in_app=True)
    qtbot.addWidget(no_libvlc)
    assert no_libvlc.choice().mode == "launch"


# -- streamed waveform peaks --


@pytest.mark.parametrize("length", [0, 1, 63, 64, 65, 10_000, 64 * 1000 + 17])
def test_streamed_peaks_equal_peaks_of_the_whole_decode(length: int) -> None:
    rng = np.random.default_rng(length)
    samples = rng.uniform(-1, 1, length).astype("<f4")
    accumulator = PeakAccumulator(64)
    position = 0
    while position < length:
        size = int(rng.integers(1, 5000))
        accumulator.feed(samples[position:position + size])
        position += size
    assert np.array_equal(accumulator.peaks(final=True), compute_peaks(samples, 64))
    assert accumulator.samples_seen == length


@pytest.mark.parametrize("length", [1, 255, 256, 257, 100_003])
def test_reduced_peaks_equal_peaks_of_bigger_blocks(length: int) -> None:
    samples = np.random.default_rng(7).uniform(-1, 1, length).astype("<f4")
    assert np.array_equal(reduce_peaks(compute_peaks(samples, 64), 4), compute_peaks(samples, 256))


def test_pyramid_from_streamed_peaks_matches_the_classic_one() -> None:
    samples = np.random.default_rng(3).uniform(-1, 1, 8000 * 90).astype("<f4")
    classic = build_pyramid(samples, 8000)
    streamed = build_pyramid_from_peaks(compute_peaks(samples, 64), 8000)
    assert len(classic.levels) == len(streamed.levels) > 1
    for a, b in zip(classic.levels, streamed.levels):
        assert a.block_size == b.block_size
        assert np.array_equal(a.peaks, b.peaks)
    assert classic.duration_us == streamed.duration_us


def test_long_decode_reports_partial_waveforms(tmp_path: Path, monkeypatch) -> None:
    from bookmark_studio.waveform import service as service_module
    from bookmark_studio.waveform.service import WaveformKey, WaveformService

    samples = np.random.default_rng(1).uniform(-1, 1, 8000 * 20).astype("<f4")
    chunks = [samples[i:i + 8000].tobytes() for i in range(0, samples.size, 8000)]
    monkeypatch.setattr(service_module, "stream_media_pcm", lambda *_a, **_k: iter(chunks))
    monkeypatch.setattr(service_module, "PROGRESS_INTERVAL_S", 0.0)
    partial = []
    svc = WaveformService(ffmpeg_path="ffmpeg", cache_dir=tmp_path)
    result = svc.generate(WaveformKey(uuid4(), "fp"), "x.wav", on_progress=partial.append)
    assert len(partial) >= 10
    durations = [p.duration_us for p in partial]
    assert durations == sorted(durations) and durations[-1] <= result.pyramid.duration_us
    assert np.array_equal(result.pyramid.levels[0].peaks, compute_peaks(samples, 64))


# -- loops vs. playback changed in VLC's own window --


def _wav(path: Path, seconds: float) -> Path:
    n = int(seconds * 8000)
    with wave.open(str(path), "w") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes((np.sin(np.arange(n) / 3) * 8000).astype(np.int16).tobytes())
    return path


@pytest.fixture()
def looping_app(qtbot, tmp_path: Path):
    from bookmark_studio.app.application import Application
    from bookmark_studio.persistence.database import connect
    from bookmark_studio.persistence.migrations import migrate
    from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter

    a, b = _wav(tmp_path / "a.wav", 8.0), _wav(tmp_path / "b.wav", 8.0)
    adapter = MockPlaybackAdapter([
        VlcPlaylistItem(1, a.resolve().as_uri(), "Song A", 8.0),
        VlcPlaylistItem(2, b.resolve().as_uri(), "Song B", 8.0),
    ])
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    app = Application(conn=conn, adapter=adapter, ffmpeg_path="/nonexistent-ffmpeg",
                      waveform_cache_dir=tmp_path / "cache")
    qtbot.addWidget(app.window)
    app.start()
    qtbot.waitUntil(lambda: app._current_media_id is not None
                    and app.playlists.synchronizer.active_playlist_id is not None, timeout=5000)
    bookmark = Bookmark(
        id=uuid4(), playlist_id=app.playlists.synchronizer.active_playlist_id, media_id=app._current_media_id,
        scope=BookmarkScope.PLAYLIST_MEDIA, lane_id=None, bookmark_type=BookmarkType.SEGMENT, name="loop",
        start_us=1_000_000, end_us=5_000_000, loop_enabled=True, repeat_count=None, loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    app._bookmark_repository.insert(bookmark)
    app._on_loop_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.PLAYING, timeout=3000)
    qtbot.wait(900)  # past the grace period after the loop's own seek
    yield app, adapter
    app.stop()
    conn.close()


def test_loop_keeps_running_when_nothing_happens_outside(qtbot, looping_app) -> None:
    app, adapter = looping_app
    qtbot.wait(1500)
    assert app._loop_controller.state is LoopState.PLAYING
    assert adapter.get_status().state == "playing"


def test_pausing_in_vlcs_window_stops_the_loop_and_stays_paused(qtbot, looping_app) -> None:
    app, adapter = looping_app
    adapter.pause()  # the user pressed pause in VLC itself
    qtbot.waitUntil(lambda: app._loop_controller.state is not LoopState.PLAYING, timeout=4000)
    qtbot.wait(800)
    assert adapter.get_status().state == "paused"  # the loop did not resume it


def test_stopping_in_vlcs_window_stops_the_loop(qtbot, looping_app) -> None:
    app, adapter = looping_app
    adapter.stop()
    qtbot.waitUntil(lambda: app._loop_controller.state is not LoopState.PLAYING, timeout=4000)
    qtbot.wait(800)
    assert adapter.get_status().state == "stopped"


def test_switching_song_in_vlcs_window_stops_the_loop(qtbot, looping_app) -> None:
    app, adapter = looping_app
    adapter.goto_item(2)
    qtbot.waitUntil(lambda: app._loop_controller.state is not LoopState.PLAYING, timeout=4000)
    qtbot.wait(800)
    status = adapter.get_status()
    assert status.current_playlist_item_id == 2  # not dragged back to the loop's song
    assert status.time_us < 1_000_000


# -- packaged builds --


def test_frozen_linux_build_gives_child_programs_the_original_library_path(monkeypatch) -> None:
    monkeypatch.setattr(platform_support, "is_frozen", lambda: True)
    monkeypatch.setattr(platform_support, "IS_WINDOWS", False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/vlc-bookmark-studio/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    platform_support.restore_child_library_path()
    assert platform_support.os.environ["LD_LIBRARY_PATH"] == "/usr/local/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in platform_support.os.environ

    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/vlc-bookmark-studio/_internal")
    platform_support.restore_child_library_path()  # the user had none
    assert "LD_LIBRARY_PATH" not in platform_support.os.environ


def test_source_checkout_leaves_the_library_path_alone(monkeypatch) -> None:
    monkeypatch.setattr(platform_support, "is_frozen", lambda: False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/somewhere")
    platform_support.restore_child_library_path()
    assert platform_support.os.environ["LD_LIBRARY_PATH"] == "/somewhere"


def test_a_libvlc_that_cannot_be_loaded_is_reported_not_fatal(tmp_path: Path, monkeypatch, only_explicit_dirs) -> None:
    """python-vlc exits the process when its library can't be loaded; the loader must
    turn that into LibVlcUnavailable instead."""
    monkeypatch.setattr(libvlc_loader, "_loaded", None)
    broken = tmp_path / "broken-vlc"
    lib_dir = broken / "usr" / "lib" / "x86_64-linux-gnu"
    lib_dir.mkdir(parents=True)
    name = "libvlc.dll" if platform_support.IS_WINDOWS else "libvlc.so.5"
    target = (broken if platform_support.IS_WINDOWS else lib_dir) / name
    target.write_bytes(b"not a shared library")
    if platform_support.IS_WINDOWS:
        monkeypatch.setattr(libvlc_loader, "pe_bits", lambda _p: None)  # unreadable header: not rejected early
    with pytest.raises(libvlc_loader.LibVlcUnavailable, match="could not be loaded"):
        libvlc_loader.load_vlc_module(str(broken))
    assert libvlc_loader.loaded_location() is None


def test_instance_failure_hint_names_where_vlc_looks_for_plugins(monkeypatch) -> None:
    monkeypatch.setenv("VLC_PLUGIN_PATH", "/opt/vlc/plugins")
    assert "VLC_PLUGIN_PATH=/opt/vlc/plugins" in libvlc_loader.instance_failure_hint()
    monkeypatch.delenv("VLC_PLUGIN_PATH")
    assert libvlc_loader.instance_failure_hint()  # never empty
