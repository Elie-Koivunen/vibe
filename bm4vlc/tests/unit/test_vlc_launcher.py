from __future__ import annotations

from pathlib import Path

from bookmark_studio.app.vlc_launcher import parse_m3u, resolve_startup_media


def test_parse_m3u_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u"
    playlist.write_text(
        "#EXTM3U\n#EXTINF:123,Some Song\nsong1.mp3\n\nhttps://example.com/stream.mp3\n",
        encoding="utf-8",
    )
    entries = parse_m3u(playlist)
    assert entries == [str((tmp_path / "song1.mp3").resolve()), "https://example.com/stream.mp3"]


def test_parse_m3u_resolves_relative_entries_against_playlist_directory(tmp_path: Path) -> None:
    subdir = tmp_path / "music"
    subdir.mkdir()
    playlist = tmp_path / "list.m3u8"
    playlist.write_text("music/track.flac\n", encoding="utf-8")
    entries = parse_m3u(playlist)
    assert entries == [str((subdir / "track.flac").resolve())]


def test_resolve_startup_media_expands_a_single_playlist_selection(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u"
    playlist.write_text("a.mp3\nb.mp3\n", encoding="utf-8")
    result = resolve_startup_media([str(playlist)])
    assert result == [str((tmp_path / "a.mp3").resolve()), str((tmp_path / "b.mp3").resolve())]


def test_resolve_startup_media_passes_through_direct_media_selections(tmp_path: Path) -> None:
    files = [str(tmp_path / "a.mp3"), str(tmp_path / "b.wav")]
    assert resolve_startup_media(files) == files
