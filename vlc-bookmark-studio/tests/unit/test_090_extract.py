"""0.9.0: Extract -- bookmarks saved as audio files of their own (media/extract.py, the
Extract audio window, the Extract... button beside the bookmark list)."""
from __future__ import annotations

import threading
import time
import wave
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QSettings
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication

from bookmark_studio import platform_support as ps
from bookmark_studio.app.application import Application
from bookmark_studio.domain.enums import BookmarkType
from bookmark_studio.media import extract as extract_module
from bookmark_studio.media.extract import (
    FORMATS,
    FORMATS_BY_KEY,
    ExtractCancelled,
    ExtractJob,
    available_formats,
    build_extract_args,
    extract,
    file_name_for,
    file_timecode,
    name_problem,
    output_file_name,
    parse_encoders,
    salvage,
    unique_path,
)
from bookmark_studio.persistence.database import connect
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.playback.status import VlcPlaylistItem
from bookmark_studio.settings.settings_service import ExtractOptions, SettingsService
from bookmark_studio.ui.dialogs.extract_dialog import ExtractDialog
from bookmark_studio.waveform.ffmpeg_decoder import decode_media_to_pcm
from tests.unit.test_050_ui import _bookmark, _make_app

FFMPEG = ps.find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


def _song(path: Path, seconds: float = 4.0, rate: int = 22050, channels: int = 1) -> Path:
    n = int(seconds * rate)
    mono = (np.sin(2 * np.pi * 440 * np.arange(n) / rate) * 0.3 * 32767).astype(np.int16)
    with wave.open(str(path), "w") as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes((np.repeat(mono, channels) if channels > 1 else mono).tobytes())
    return path


def _job(source: Path | None, **overrides) -> ExtractJob:
    values = dict(bookmark_id=uuid4(), song="Groove One", name="drop", start_us=1_000_000, end_us=2_500_000,
                  source=source)
    values.update(overrides)
    return ExtractJob(**values)


def _seconds_of(path: Path) -> float:
    assert FFMPEG is not None
    return len(decode_media_to_pcm(FFMPEG, str(path))) / 4 / 8000  # mono f32le at 8 kHz


# -- names --


def test_a_file_is_named_after_the_song_the_bookmark_and_its_range() -> None:
    assert (output_file_name("Groove One", "drop", 12_000_000, 21_500_000, "mp3")
            == "Groove One - drop - 00-00-12.000 to 00-00-21.500.mp3")
    assert file_timecode(3_723_456_000) == "01-02-03.456"


def test_a_name_is_checked_against_what_common_file_systems_store() -> None:
    """(Requested on the 0.9.0 build.) Letters of any script, digits and common punctuation
    pass; what Windows forbids, symbols such as emoji, reserved names, a trailing dot,
    decomposed accents and overlong names don't."""
    for fine in ("Groove One - drop - 00-00-12.000 to 00-00-21.500.mp3", "Café del Mar (Remix) [2024].flac",
                 "Мелодия - k3x9qa.ogg", "曲名 - drop.opus", "Rock & Roll, Vol. 2 #1 - x.wav"):
        assert name_problem(fine) is None, fine
    for bad in ('a:b.mp3', 'what?.mp3', 'a"b.mp3', 'a/b.mp3', "a\\b.mp3", "song 🎵.mp3", "tab\there.mp3",
                "CON.mp3", "lpt1.mp3", "trailing dot.", " leading.mp3", "Cafe\u0301.mp3", "x" * 250 + ".mp3", "",
                "\U00020000.mp3"):  # (a letter, but beyond the BMP: 4 bytes some tools mishandle)
        assert name_problem(bad) is not None, bad
    assert salvage("Groove: One? 🎵  (Live)") == "Groove One (Live)"
    assert salvage("🎵🎵") == "" and salvage("CON") == ""


def test_a_name_that_wont_do_starts_with_the_bookmarks_string_and_keeps_what_it_can_of_the_song() -> None:
    plain = _job(None, song="Groove One", name="k3x9qa", start_us=12_000_000, end_us=21_500_000)
    assert file_name_for(plain, "mp3") == ("Groove One - k3x9qa - 00-00-12.000 to 00-00-21.500.mp3", None)
    emoji = _job(None, song="Groove: One? 🎵", name="k3x9qa", start_us=12_000_000, end_us=21_500_000)
    name, why = file_name_for(emoji, "mp3", repeats=3)
    assert name == "k3x9qa - Groove One - 00-00-12.000 to 00-00-21.500 x3.mp3" and why
    nothing_left = _job(None, song="🎵🎵🎵", name="k3x9qa", start_us=0, end_us=1_000_000)
    assert file_name_for(nothing_left, "wav")[0] == "k3x9qa - 00-00-00.000 to 00-00-01.000.wav"
    bad_bookmark_name = _job(None, song="Song/Mix", name="a:b", start_us=0, end_us=1_000_000)
    name, _why = file_name_for(bad_bookmark_name, "wav")
    assert name.startswith(str(bad_bookmark_name.bookmark_id)[:8] + " - SongMix - ")  # its unique id instead
    for job in (plain, emoji, nothing_left, bad_bookmark_name):
        assert name_problem(file_name_for(job, "mp3")[0]) is None


def test_an_extraction_never_overwrites_a_file(tmp_path) -> None:
    assert unique_path(tmp_path, "a.mp3") == tmp_path / "a.mp3"
    (tmp_path / "a.mp3").write_bytes(b"")
    (tmp_path / "a (2).mp3").write_bytes(b"")
    assert unique_path(tmp_path, "a.mp3") == tmp_path / "a (3).mp3"


# -- formats and FFmpeg's arguments --


def test_formats_are_open_ones_including_mp3() -> None:
    assert [fmt.key for fmt in FORMATS] == ["mp3", "ogg", "opus", "flac", "wav"]
    assert FORMATS_BY_KEY["mp3"].encoder == "libmp3lame"


def test_the_encoders_an_ffmpeg_has_are_read_from_its_list() -> None:
    output = """Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D libx264              libx264 H.264
 A....D libmp3lame           libmp3lame MP3 (MPEG audio layer 3) (codec mp3)
 A....D flac                 FLAC (Free Lossless Audio Codec)
 A....D pcm_s16le            PCM signed 16-bit little-endian
"""
    assert parse_encoders(output) == {"libmp3lame", "flac", "pcm_s16le"}
    assert available_formats(None) == set()
    assert available_formats(str(Path("/nonexistent/ffmpeg"))) == set()


def test_ffmpeg_cuts_the_range_with_the_formats_settings(tmp_path) -> None:
    source = tmp_path / "song.wav"
    job = _job(source, start_us=12_000_000, end_us=21_500_000, fade_in_ms=300, fade_out_ms=500, gap_ms=250)
    args = build_extract_args("ffmpeg", job, tmp_path / "out.mp3", FORMATS_BY_KEY["mp3"], 0)
    assert args[args.index("-ss") + 1] == "12.000000" and args[args.index("-t") + 1] == "9.500000"
    assert args.index("-ss") < args.index("-i") and args.index("-t") < args.index("-i")  # read just the range
    assert args[args.index("-c:a") + 1:args.index("-c:a") + 3] == ["libmp3lame", "-b:a"]
    assert "-n" in args and "-filter_complex" not in args  # never overwrites; no fades unless asked
    assert args[args.index("-map") + 1] == "0:a:0"
    assert args[-1] == str(tmp_path / "out.mp3")
    faded = build_extract_args("ffmpeg", job, tmp_path / "out.mp3", FORMATS_BY_KEY["mp3"], 0, apply_fades=True)
    assert faded[faded.index("-filter_complex") + 1] == "[0:a:0]afade=t=in:st=0:d=0.300,afade=t=out:st=9.000:d=0.500[out]"
    # three times, the gap as silence between, fades over the whole: 3 x 9.5 s + 2 x 0.25 s = 29 s
    looped = build_extract_args("ffmpeg", job, tmp_path / "out.mp3", FORMATS_BY_KEY["mp3"], 0, apply_fades=True,
                                repeats=3)
    assert looped[looped.index("-filter_complex") + 1] == (
        "[0:a:0]asplit=3[r0][r1][r2];[r0]apad=pad_dur=0.250[p0];[r1]apad=pad_dur=0.250[p1];"
        "[p0][p1][r2]concat=n=3:v=0:a=1[joined];"
        "[joined]afade=t=in:st=0:d=0.300,afade=t=out:st=28.500:d=0.500[out]")
    assert looped[looped.index("-map") + 1] == "[out]"


def test_points_and_missing_files_are_not_extracted(tmp_path) -> None:
    assert _job(tmp_path / "a.wav", end_us=None).why_not() == "a point bookmark has no range"
    assert _job(None).why_not() == "the song's file was not found"
    with pytest.raises(ValueError):
        extract("ffmpeg", _job(None), tmp_path, FORMATS_BY_KEY["wav"])


# -- with a real FFmpeg --


@needs_ffmpeg
def test_every_format_this_ffmpeg_has_is_cut_to_the_bookmarks_length(tmp_path) -> None:
    source = _song(tmp_path / "Groove One.wav", channels=2, rate=44100)
    out = tmp_path / "out"
    out.mkdir()
    formats = available_formats(FFMPEG)
    assert {"wav", "flac"} <= formats
    for fmt in FORMATS:
        if fmt.key not in formats:
            continue
        path = extract(FFMPEG, _job(source), out, fmt, 0)
        assert path.name == f"Groove One - drop - 00-00-01.000 to 00-00-02.500.{fmt.extension}"
        assert abs(_seconds_of(path) - 1.5) < 0.06, fmt.key  # (lossy encoders pad a little)
    wav = out / "Groove One - drop - 00-00-01.000 to 00-00-02.500.wav"
    with wave.open(str(wav)) as f:
        assert f.getnchannels() == 2 and f.getframerate() == 44100
        assert f.getnframes() == round(1.5 * 44100)  # exactly the range
    assert not [p for p in out.iterdir() if ".partial." in p.name]


@needs_ffmpeg
def test_a_second_extraction_of_the_same_bookmark_gets_a_new_name(tmp_path) -> None:
    source = _song(tmp_path / "s.wav")
    first = extract(FFMPEG, _job(source), tmp_path, FORMATS_BY_KEY["wav"])
    second = extract(FFMPEG, _job(source), tmp_path, FORMATS_BY_KEY["wav"])
    assert first != second and second.name.endswith(" (2).wav") and first.exists()


@needs_ffmpeg
def test_a_failed_extraction_leaves_nothing_behind(tmp_path) -> None:
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"this is not audio" * 100)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RuntimeError):
        extract(FFMPEG, _job(broken), out, FORMATS_BY_KEY["wav"])
    assert list(out.iterdir()) == []


@needs_ffmpeg
def test_cancelling_stops_ffmpeg_and_leaves_nothing_behind(tmp_path) -> None:
    source = _song(tmp_path / "long.wav", seconds=400, rate=44100, channels=2)
    out = tmp_path / "out"
    out.mkdir()
    cancelled = threading.Event()
    threading.Timer(0.3, cancelled.set).start()
    started = time.monotonic()
    with pytest.raises(ExtractCancelled):
        extract(FFMPEG, _job(source, start_us=0, end_us=399_000_000), out, FORMATS_BY_KEY["flac"],
                cancelled=cancelled)
    assert time.monotonic() - started < 10
    assert list(out.iterdir()) == []


# -- the Extract audio window --


def _settings(tmp_path) -> SettingsService:
    return SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))


def test_formats_this_ffmpeg_lacks_cannot_be_chosen(qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bookmark_studio.ui.dialogs.extract_dialog.available_formats", lambda _p: {"flac", "wav"})
    dialog = ExtractDialog([_job(tmp_path / "a.wav")], "ffmpeg", None)
    qtbot.addWidget(dialog)
    model = dialog._format_combo.model()
    enabled = {dialog._format_combo.itemData(i): model.item(i).isEnabled() for i in range(model.rowCount())}
    assert enabled == {"mp3": False, "ogg": False, "opus": False, "flac": True, "wav": True}
    assert dialog._format_combo.currentData() == "flac"  # the first one it has


def test_without_ffmpeg_nothing_can_be_extracted(qtbot, tmp_path) -> None:
    dialog = ExtractDialog([_job(tmp_path / "a.wav")], None, None)
    qtbot.addWidget(dialog)
    assert not dialog.extract_button.isEnabled()
    assert "FFmpeg was not found" in dialog._status.text()


def test_the_window_lists_what_it_skips_and_remembers_the_choices(qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bookmark_studio.ui.dialogs.extract_dialog.available_formats",
                        lambda _p: {fmt.key for fmt in FORMATS})
    monkeypatch.setattr("bookmark_studio.ui.dialogs.extract_dialog.extract",
                        lambda ffmpeg, job, folder, fmt, quality, **kw: folder / f"{job.name}.{fmt.extension}")
    settings = _settings(tmp_path)
    settings.set_extract_options(ExtractOptions(folder=str(tmp_path), format_key="ogg", quality=2, apply_fades=True))
    jobs = [_job(tmp_path / "a.wav", name="one"), _job(tmp_path / "a.wav", name="point", end_us=None),
            _job(None, name="lost")]
    dialog = ExtractDialog(jobs, "ffmpeg", settings)
    qtbot.addWidget(dialog)
    assert dialog._format_combo.currentData() == "ogg" and dialog._quality_combo.currentIndex() == 2
    assert dialog._fades_check.isChecked() and dialog.folder() == tmp_path
    texts = [dialog._list.item(i).text() for i in range(dialog._list.count())]
    assert "skipped" not in texts[0] and "skipped: a point bookmark" in texts[1] and "skipped: the song" in texts[2]
    dialog.set_format("mp3", 1)
    dialog.start()
    dialog.wait_until_done()
    qtbot.waitUntil(lambda: not dialog.is_running() and dialog._thread is None, timeout=3000)
    assert [(index, path.name) for index, path, _error in dialog.results] == [(0, "one.mp3")]
    assert settings.extract_options() == ExtractOptions(folder=str(tmp_path), format_key="mp3", quality=1,
                                                        apply_fades=True)
    assert "Saved 1 file" in dialog._status.text()


@needs_ffmpeg
def test_the_window_saves_the_selected_bookmarks(qtbot, tmp_path) -> None:
    source = _song(tmp_path / "Groove One.wav")
    out = tmp_path / "out"
    jobs = [_job(source, name="intro", start_us=0, end_us=1_000_000),
            _job(source, name="drop", start_us=2_000_000, end_us=3_000_000)]
    dialog = ExtractDialog(jobs, FFMPEG, None)
    qtbot.addWidget(dialog)
    dialog.set_folder(str(out))  # made if it isn't there
    dialog.set_format("wav")
    dialog.start()
    dialog.wait_until_done()
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=5000)
    assert sorted(p.name for p in out.iterdir()) == [
        "Groove One - drop - 00-00-02.000 to 00-00-03.000.wav",
        "Groove One - intro - 00-00-00.000 to 00-00-01.000.wav",
    ]
    assert dialog._open_folder_button.isVisibleTo(dialog)
    assert all(dialog._list.item(i).text().startswith("✓") for i in range(2))


# -- Extract... beside the list --


def test_extract_sits_between_save_and_delete_and_needs_a_range(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    panel = window._bookmark_panel
    order = [panel._export_button, panel._extract_button, panel._delete_bookmark_button]
    ys = [b.mapTo(window, QPoint(0, 0)).y() for b in order]
    assert ys == sorted(ys) and panel._extract_button.text() == "Extract..."
    segment = _bookmark(app, name="seg")
    point = _bookmark(app, name="pt", start_us=3_000_000, end_us=None, bookmark_type=BookmarkType.POINT)
    assert not panel._extract_button.isEnabled()  # nothing selected
    panel.select_bookmark(point.id)
    assert not panel._extract_button.isEnabled()  # a point has no range
    panel.select_bookmarks({point.id, segment.id})
    assert panel._extract_button.isEnabled()
    requests: list = []
    panel.extract_requested.connect(requests.append)
    panel._extract_button.click()
    rows = [panel._tree.topLevelItem(i).data(0, 32) for i in range(panel._tree.topLevelItemCount())]
    assert requests == [[i for i in rows if i in (point.id, segment.id)]]  # in list order
    action = next(a for a in window.menuBar().findChildren(QAction)
                  if a.text() == "Extract Audio of Selected Bookmarks...")
    assert action.shortcut().toString() == "Ctrl+E"


@needs_ffmpeg
def test_extract_from_the_list_names_files_after_the_songs_file(qtbot, tmp_path) -> None:
    song = _song(tmp_path / "Groove One.wav", seconds=5.0)
    adapter = MockPlaybackAdapter([VlcPlaylistItem(1, song.resolve().as_uri(), "Groove One", 5.0)])
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    app = Application(conn=conn, adapter=adapter, ffmpeg_path=FFMPEG, waveform_cache_dir=tmp_path / "wf")
    qtbot.addWidget(app.window)
    app.start()
    qtbot._request.addfinalizer(app.stop)
    qtbot.waitUntil(lambda: app._current_media_id is not None
                    and app.playlists.synchronizer.active_playlist_id is not None, timeout=5000)
    bookmark = _bookmark(app, name="drop", start_us=1_000_000, end_us=2_000_000)
    panel = app.window._bookmark_panel
    panel.select_bookmark(bookmark.id)
    panel._extract_button.click()
    dialog = app.window._extract_dialog
    assert dialog.isVisible()
    out = tmp_path / "out"
    dialog.set_folder(str(out))
    dialog.set_format("flac")
    dialog.start()
    dialog.wait_until_done()
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=5000)
    assert [p.name for p in out.iterdir()] == ["Groove One - drop - 00-00-01.000 to 00-00-02.000.flac"]
    QApplication.processEvents()


# -- the song's tags, the tool's name, the cover (requested on the 0.9.0 build) --

FFPROBE = None
if FFMPEG is not None:
    _probe = Path(FFMPEG).with_name("ffprobe" + Path(FFMPEG).suffix)
    FFPROBE = str(_probe) if _probe.is_file() else None
needs_ffprobe = pytest.mark.skipif(FFPROBE is None, reason="ffprobe is not installed")


def _tags_and_pictures(path: Path) -> tuple[dict[str, str], int]:
    import json
    import subprocess

    assert FFPROBE is not None
    result = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format_tags:stream_tags:stream=codec_type",
                             "-of", "json", str(path)], capture_output=True, encoding="utf-8", check=True)
    data = json.loads(result.stdout)
    tags: dict[str, str] = {}
    for stream in data.get("streams", []):
        tags.update({k.lower(): v for k, v in stream.get("tags", {}).items()})
    tags.update({k.lower(): v for k, v in data.get("format", {}).get("tags", {}).items()})
    pictures = sum(1 for stream in data.get("streams", []) if stream.get("codec_type") == "video")
    return tags, pictures


def _tagged_song(tmp_path: Path, extension: str, encoder: str, *, cover: bool = False) -> Path:
    import subprocess

    assert FFMPEG is not None
    plain = tmp_path / f"plain.{extension}"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=5:r=44100",
                    "-metadata", "title=Groove One", "-metadata", "artist=The Artist", "-metadata", "album=The Album",
                    "-metadata", "genre=House", "-c:a", encoder, str(plain)], check=True)
    if not cover:
        return plain
    picture = tmp_path / "cover.png"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=32x32", "-frames:v", "1",
                    str(picture)], check=True)
    with_cover = tmp_path / f"song.{extension}"
    subprocess.run([FFMPEG, "-v", "error", "-i", str(plain), "-i", str(picture), "-map", "0:a", "-map", "1:v",
                    "-c:a", "copy", "-c:v", "mjpeg", "-disposition:v", "attached_pic", str(with_cover)], check=True)
    return with_cover


@needs_ffmpeg
@needs_ffprobe
@pytest.mark.parametrize("source_format", [("mp3", "libmp3lame"), ("ogg", "libvorbis")])
def test_the_files_carry_the_songs_tags_and_the_tools_name(tmp_path, source_format) -> None:
    from bookmark_studio import __version__

    extension, encoder = source_format
    if extension not in available_formats(FFMPEG):
        pytest.skip(f"this ffmpeg has no {encoder}")
    source = _tagged_song(tmp_path, extension, encoder, cover=extension == "mp3")
    out = tmp_path / "out"
    out.mkdir()
    for fmt in FORMATS:
        if fmt.key not in available_formats(FFMPEG):
            continue
        job = _job(source, title="Groove One", name="drop")
        path = extract(FFMPEG, job, out, fmt)
        tags, pictures = _tags_and_pictures(path)
        assert (tags.get("artist"), tags.get("album"), tags.get("genre")) == ("The Artist", "The Album", "House"), \
            (fmt.key, tags)
        assert tags.get("title") == "Groove One - drop", fmt.key
        assert tags.get("encoded_by") == f"VLC Bookmark Studio {__version__}", (fmt.key, tags)
        assert f"Extracted with VLC Bookmark Studio {__version__}" in tags.get("comment", ""), fmt.key
        if extension == "mp3" and fmt.key in ("mp3", "flac"):
            assert pictures == 1, fmt.key  # the cover came along


# -- repeats and fades over the whole file (requested on the 0.9.0 build) --


def test_a_jobs_repeats_are_the_bookmarks_own_count() -> None:
    """Only the count set in Bookmark Studio (reported on the 0.9.0 build: a "Forever as"
    field was one setting too many). Loop off, or Forever -- no count -- once."""
    assert _job(None, loop_enabled=False, repeat_count=5).repeats() == 1
    assert _job(None, loop_enabled=True, repeat_count=3).repeats() == 3
    assert _job(None, loop_enabled=True, repeat_count=None).repeats() == 1  # "Forever"
    assert _job(None, loop_enabled=True, repeat_count=500).repeats() == extract_module.MAX_REPEATS
    assert output_file_name("Song", "drop", 0, 1_000_000, "mp3", 3) == "Song - drop - 00-00-00.000 to 00-00-01.000 x3.mp3"


def _rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if len(samples) else 0.0


@needs_ffmpeg
def test_repeats_play_the_range_again_with_gaps_and_the_fades_go_over_the_whole_file(tmp_path) -> None:
    source = _song(tmp_path / "s.wav", seconds=4.0, rate=22050)
    job = _job(source, start_us=1_000_000, end_us=2_000_000, loop_enabled=True, repeat_count=3, gap_ms=250,
               fade_in_ms=300, fade_out_ms=400)
    path = extract(FFMPEG, job, tmp_path, FORMATS_BY_KEY["wav"], apply_fades=True, repeats=job.repeats())
    assert path.name.endswith(" x3.wav")
    with wave.open(str(path)) as f:
        rate = f.getframerate()
        samples = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
    assert abs(len(samples) - (3 * 1.0 + 2 * 0.25) * rate) <= 2  # 3 passes, 2 gaps (to the sample)

    def window(start_s: float, length_s: float = 0.05) -> float:
        return _rms(samples[int(start_s * rate):int((start_s + length_s) * rate)])

    full = window(0.6)
    assert window(0.0, 0.03) < full * 0.3  # faded in at the very start...
    assert window(1.05, 0.15) < full * 0.01 and window(2.30, 0.15) < full * 0.01  # the gaps are silent
    assert window(1.27) > full * 0.8 and window(2.52) > full * 0.8  # ...but not at each repeat
    assert window(3.45) < full * 0.3  # faded out at the very end


def test_loops_and_fades_follow_each_bookmark_and_are_on_from_the_start(qtbot, tmp_path, monkeypatch) -> None:
    """Reported on the 0.9.0 build: the fades weren't applied -- the option was off. Both
    options now default to the bookmarks' own settings, and each line says what applies."""
    monkeypatch.setattr("bookmark_studio.ui.dialogs.extract_dialog.available_formats", lambda _p: {"wav"})
    calls: list = []
    monkeypatch.setattr("bookmark_studio.ui.dialogs.extract_dialog.extract",
                        lambda ffmpeg, job, folder, fmt, quality, **kw: calls.append(kw) or folder / "x.wav")
    settings = _settings(tmp_path)
    jobs = [_job(tmp_path / "a.wav", name="forever", loop_enabled=True, repeat_count=None, fade_in_ms=3000),
            _job(tmp_path / "a.wav", name="twice", loop_enabled=True, repeat_count=2, fade_out_ms=1500),
            _job(tmp_path / "a.wav", name="once", loop_enabled=False)]
    dialog = ExtractDialog(jobs, "ffmpeg", settings)
    qtbot.addWidget(dialog)
    assert dialog._loops_check.isChecked() and dialog._fades_check.isChecked()  # nothing saved yet: on
    texts = [dialog._list.item(i).text() for i in range(3)]
    assert "loops forever: once" in texts[0] and "fade in 3 s, out 0 s" in texts[0]
    assert "×2" in texts[1] and "fade in 0 s, out 1.5 s" in texts[1]
    assert "(" not in texts[2]  # nothing to apply
    assert dialog._example.text() == "Groove One - forever - 00-00-01.000 to 00-00-02.500.wav"
    dialog._loops_check.setChecked(False)
    assert "×2" not in dialog._list.item(1).text()
    dialog._loops_check.setChecked(True)
    dialog.start()
    dialog.wait_until_done()
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=3000)
    assert [(kw["repeats"], kw["apply_fades"]) for kw in calls] == [(1, True), (2, True), (1, True)]
    options = settings.extract_options()
    assert options.apply_loops is True and options.apply_fades is True


def test_the_window_shows_a_replaced_file_name(qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bookmark_studio.ui.dialogs.extract_dialog.available_formats", lambda _p: {"mp3"})
    dialog = ExtractDialog([_job(tmp_path / "a.wav", song="Song 🎵", name="k3x9qa")], "ffmpeg", None)
    qtbot.addWidget(dialog)
    assert dialog._example.text().startswith("k3x9qa - Song - ") and "renamed:" in dialog._example.text()
    assert "file renamed" in dialog._list.item(0).text()


# -- the user's own tags (requested on the 0.9.0 build) --


def test_own_tags_are_cleaned_up_for_ffmpeg() -> None:
    from bookmark_studio.media.extract import clean_tags

    assert clean_tags([("Artist", " Me "), ("my=tag", "x"), ("empty", ""), ("", "no name"), ("Mood", "up\x07beat")]) \
        == {"artist": "Me", "mytag": "x", "Mood": "upbeat"}


def test_own_tags_go_into_the_file_over_the_songs(tmp_path) -> None:
    from bookmark_studio import __version__

    job = _job(tmp_path / "a.wav", title="Groove One", name="drop")
    args = build_extract_args("ffmpeg", job, tmp_path / "out.mp3", FORMATS_BY_KEY["mp3"],
                              tags={"artist": "DJ Me", "comment": "my loop", "Mood": "up"})
    tags = [args[i + 1] for i, arg in enumerate(args) if arg == "-metadata"]
    assert tags.index("artist=DJ Me") > tags.index(f"encoded_by=VLC Bookmark Studio {__version__}")
    assert "comment=my loop" in tags and "Mood=up" in tags
    assert [t for t in tags if t.startswith("comment=")][-1] == "comment=my loop"  # the user's wins


def test_the_window_takes_own_tags_and_remembers_them(qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bookmark_studio.ui.dialogs.extract_dialog.available_formats", lambda _p: {"wav"})
    calls: list = []
    monkeypatch.setattr("bookmark_studio.ui.dialogs.extract_dialog.extract",
                        lambda ffmpeg, job, folder, fmt, quality, **kw: calls.append(kw) or folder / "x.wav")
    settings = _settings(tmp_path)
    settings.set_extract_options(ExtractOptions(folder=str(tmp_path), format_key="wav",
                                                tags=(("artist", "DJ Me"),)))
    dialog = ExtractDialog([_job(tmp_path / "a.wav")], "ffmpeg", settings)
    qtbot.addWidget(dialog)
    assert dialog.user_tags() == [("artist", "DJ Me")]  # remembered
    dialog.add_tag("genre", "House")
    dialog.add_tag("comment", "")  # left empty: not written
    dialog.start()
    dialog.wait_until_done()
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=3000)
    assert calls[0]["tags"] == {"artist": "DJ Me", "genre": "House"}
    assert settings.extract_options().tags == (("artist", "DJ Me"), ("genre", "House"))


@needs_ffmpeg
@needs_ffprobe
def test_own_tags_are_in_the_written_file(tmp_path) -> None:
    if "mp3" not in available_formats(FFMPEG):
        pytest.skip("this ffmpeg has no MP3 encoder")
    source = _tagged_song(tmp_path, "mp3", "libmp3lame")
    path = extract(FFMPEG, _job(source, name="drop"), tmp_path, FORMATS_BY_KEY["mp3"],
                   tags={"artist": "DJ Me", "Mood": "uplifting"})
    tags, _pictures = _tags_and_pictures(path)
    assert tags.get("artist") == "DJ Me" and tags.get("album") == "The Album"  # the user's, and the song's
    assert tags.get("mood") == "uplifting"
