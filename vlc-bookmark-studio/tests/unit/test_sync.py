"""Folder sync between installations (Windows <-> WSL <-> other PCs), 0.3.0."""
from __future__ import annotations

import time
import zipfile
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.domain.lane import Lane
from bookmark_studio.domain.media import Media
from bookmark_studio.domain.playlist import Playlist
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.database import connect
from bookmark_studio.persistence.lane_repository import LaneRepository
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.playlist.signatures import strict_signature
from bookmark_studio.project.import_service import apply_import_plan, read_import_plan
from bookmark_studio.project.sync_service import SyncService, machine_id


@pytest.fixture()
def dbs(tmp_path: Path):
    conns = []

    def _open(name: str):
        conn = connect(tmp_path / f"{name}.db")
        migrate(conn)
        conns.append(conn)
        return conn

    yield _open
    for conn in conns:
        conn.close()


def _media(uri: str, fingerprint: str) -> Media:
    return Media(id=uuid4(), canonical_uri=uri, filename=uri.rsplit("/", 1)[-1], title=None, artist=None,
                 album=None, duration_us=180_000_000, file_size=1234, mtime_ns=None, fast_fingerprint=fingerprint)


def _bookmark(media_id: UUID, playlist_id: UUID | None = None, **overrides) -> Bookmark:
    values = dict(
        id=uuid4(), playlist_id=playlist_id, media_id=media_id,
        scope=BookmarkScope.PLAYLIST_MEDIA if playlist_id else BookmarkScope.GLOBAL_MEDIA,
        lane_id=None, bookmark_type=BookmarkType.SEGMENT, name="chorus", start_us=1_000_000,
        end_us=2_000_000, loop_enabled=True, repeat_count=None, loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    values.update(overrides)
    return Bookmark(**values)


def _names(conn) -> dict[UUID, str]:
    return {b.id: b.name for b in BookmarkRepository(conn).list_all()}


def _tick() -> None:
    time.sleep(0.02)  # distinct updated_at timestamps on coarse clocks


def test_bookmark_reaches_the_other_side_on_the_same_song_under_another_path(dbs, tmp_path) -> None:
    """Windows knows the song as C:\\Music\\a.mp3, WSL as /mnt/c/Music/a.mp3: different
    media rows, same content fingerprint."""
    win, wsl = dbs("win"), dbs("wsl")
    song_win = MediaRepository(win).insert(_media("file:///C:/Music/a.mp3", "fp-a"))
    song_wsl = MediaRepository(wsl).insert(_media("file:///mnt/c/Music/a.mp3", "fp-a"))
    bookmark = BookmarkRepository(win).insert(_bookmark(song_win.id, name="intro"))

    folder = tmp_path / "shared"
    SyncService(win, folder).sync_now()
    assert SyncService(wsl, folder).sync_now() is True

    received = BookmarkRepository(wsl).get(bookmark.id)
    assert received is not None and received.name == "intro"
    assert received.media_id == song_wsl.id  # mapped onto the local song, no duplicate media
    assert len(MediaRepository(wsl).list_all()) == 1


def test_playlist_is_matched_by_its_song_order_across_databases(dbs, tmp_path) -> None:
    win, wsl = dbs("win"), dbs("wsl")
    folder = tmp_path / "shared"
    songs = {}
    playlists = {}
    for conn, prefix, name in ((win, "file:///C:/Music", "win"), (wsl, "file:///mnt/c/Music", "wsl")):
        media = [MediaRepository(conn).insert(_media(f"{prefix}/{n}.mp3", f"fp-{n}")) for n in ("a", "b")]
        repo = PlaylistRepository(conn)
        record = repo.insert(Playlist(id=uuid4(), name=f"mix ({name})", source_uri=None, is_ad_hoc=True))
        ids = [m.id for m in media]
        repo.replace_items(record.playlist.id, ids)
        repo.add_signature(record.playlist.id, strict_signature(ids))
        songs[name], playlists[name] = media, record.playlist.id

    bookmark = BookmarkRepository(win).insert(_bookmark(songs["win"][1].id, playlists["win"]))
    SyncService(win, folder).sync_now()
    SyncService(wsl, folder).sync_now()

    received = BookmarkRepository(wsl).get(bookmark.id)
    assert received is not None
    assert received.playlist_id == playlists["wsl"]
    assert received.media_id == songs["wsl"][1].id
    assert len(PlaylistRepository(wsl).list_all()) == 1  # no duplicate playlist


def test_the_most_recent_edit_wins_on_both_sides(dbs, tmp_path) -> None:
    a_conn, b_conn = dbs("a"), dbs("b")
    folder = tmp_path / "shared"
    a, b = SyncService(a_conn, folder), SyncService(b_conn, folder)
    song = MediaRepository(a_conn).insert(_media("file:///song.mp3", "fp"))
    bookmark = BookmarkRepository(a_conn).insert(_bookmark(song.id, name="v1"))
    a.sync_now()
    b.sync_now()

    BookmarkRepository(a_conn).update(replace(bookmark, name="edited on A"))
    _tick()
    BookmarkRepository(b_conn).update(replace(bookmark, name="edited on B, later"))
    for _round in range(2):
        a.sync_now()
        b.sync_now()

    assert _names(a_conn)[bookmark.id] == "edited on B, later"
    assert _names(b_conn)[bookmark.id] == "edited on B, later"


def test_a_deletion_reaches_the_other_side_and_does_not_come_back(dbs, tmp_path) -> None:
    a_conn, b_conn = dbs("a"), dbs("b")
    folder = tmp_path / "shared"
    a, b = SyncService(a_conn, folder), SyncService(b_conn, folder)
    song = MediaRepository(a_conn).insert(_media("file:///song.mp3", "fp"))
    keep = BookmarkRepository(a_conn).insert(_bookmark(song.id, name="keep"))
    gone = BookmarkRepository(a_conn).insert(_bookmark(song.id, name="gone", start_us=5_000_000, end_us=6_000_000))
    a.sync_now()
    b.sync_now()
    assert set(_names(b_conn)) == {keep.id, gone.id}

    _tick()
    BookmarkRepository(a_conn).delete(gone.id)
    for _round in range(3):
        a.sync_now()
        b.sync_now()

    assert set(_names(a_conn)) == {keep.id}
    assert set(_names(b_conn)) == {keep.id}


def test_an_edit_made_after_the_deletion_keeps_the_bookmark(dbs, tmp_path) -> None:
    a_conn, b_conn = dbs("a"), dbs("b")
    folder = tmp_path / "shared"
    a, b = SyncService(a_conn, folder), SyncService(b_conn, folder)
    song = MediaRepository(a_conn).insert(_media("file:///song.mp3", "fp"))
    bookmark = BookmarkRepository(a_conn).insert(_bookmark(song.id))
    a.sync_now()
    b.sync_now()

    BookmarkRepository(a_conn).delete(bookmark.id)
    _tick()
    BookmarkRepository(b_conn).update(replace(bookmark, name="still wanted"))
    for _round in range(2):
        a.sync_now()
        b.sync_now()

    assert _names(a_conn).get(bookmark.id) == "still wanted"
    assert _names(b_conn).get(bookmark.id) == "still wanted"


def test_undo_of_a_delete_wins_over_the_deletion(dbs, tmp_path) -> None:
    a_conn, b_conn = dbs("a"), dbs("b")
    folder = tmp_path / "shared"
    a, b = SyncService(a_conn, folder), SyncService(b_conn, folder)
    song = MediaRepository(a_conn).insert(_media("file:///song.mp3", "fp"))
    bookmark = BookmarkRepository(a_conn).insert(_bookmark(song.id))
    a.sync_now()
    b.sync_now()

    BookmarkRepository(a_conn).delete(bookmark.id)
    a.sync_now()
    b.sync_now()
    assert bookmark.id not in _names(b_conn)
    _tick()
    BookmarkRepository(a_conn).insert(bookmark)  # what undo does
    a.sync_now()
    b.sync_now()
    assert bookmark.id in _names(b_conn)


def test_unchanged_database_is_not_rewritten(dbs, tmp_path) -> None:
    conn = dbs("a")
    service = SyncService(conn, tmp_path / "shared")
    assert service.export_now() is True
    assert service.export_now() is False  # no churn in a cloud-synced folder
    song = MediaRepository(conn).insert(_media("file:///song.mp3", "fp"))
    BookmarkRepository(conn).insert(_bookmark(song.id))
    assert service.export_now() is True
    assert not list((tmp_path / "shared").glob("*.tmp"))


def test_two_idle_installations_settle_without_rewriting_each_other(dbs, tmp_path) -> None:
    a_conn, b_conn = dbs("a"), dbs("b")
    folder = tmp_path / "shared"
    a, b = SyncService(a_conn, folder), SyncService(b_conn, folder)
    song = MediaRepository(a_conn).insert(_media("file:///song.mp3", "fp"))
    BookmarkRepository(a_conn).insert(_bookmark(song.id))
    for _round in range(2):
        a.sync_now()
        b.sync_now()
    stamps = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
    for _round in range(2):
        assert a.sync_now() is False
        assert b.sync_now() is False
    assert {p.name: p.stat().st_mtime_ns for p in folder.iterdir()} == stamps


def test_sync_adds_missing_lanes_but_never_overwrites_local_ones(dbs, tmp_path) -> None:
    a_conn, b_conn = dbs("a"), dbs("b")
    folder = tmp_path / "shared"
    playlist = Playlist(id=uuid4(), name="mix", source_uri="file:///mix.m3u", is_ad_hoc=False)
    PlaylistRepository(a_conn).insert(playlist)
    lane = Lane(id=uuid4(), playlist_id=playlist.id, name="Drums", order_index=0, visible=True, locked=False,
                color_key=None)
    LaneRepository(a_conn).insert(lane)
    SyncService(a_conn, folder).sync_now()
    b = SyncService(b_conn, folder)
    b.sync_now()
    assert [x.name for x in LaneRepository(b_conn).list_all()] == ["Drums"]

    LaneRepository(b_conn).update(replace(lane, name="Percussion"))
    b.import_others()
    b._imported.clear()  # force a re-merge of A's (older) file
    b.import_others()
    assert [x.name for x in LaneRepository(b_conn).list_all()] == ["Percussion"]


def test_own_file_is_ignored_and_a_broken_file_does_not_stop_the_others(dbs, tmp_path) -> None:
    a_conn, b_conn, c_conn = dbs("a"), dbs("b"), dbs("c")
    folder = tmp_path / "shared"
    song = MediaRepository(a_conn).insert(_media("file:///song.mp3", "fp"))
    bookmark = BookmarkRepository(a_conn).insert(_bookmark(song.id))
    SyncService(a_conn, folder).sync_now()
    (folder / "vlc-bookmark-studio-sync-broken.vlcbmk").write_bytes(b"not a zip")
    with zipfile.ZipFile(folder / "vlc-bookmark-studio-sync-empty.vlcbmk", "w"):
        pass

    b = SyncService(b_conn, folder)
    assert b.sync_now() is True
    assert bookmark.id in _names(b_conn)
    assert b.own_path.name not in [p.name for p in b.other_files()]
    assert SyncService(c_conn, folder).sync_now() is True


def test_machine_id_is_stable_per_database_and_differs_between_databases(dbs) -> None:
    a_conn, b_conn = dbs("a"), dbs("b")
    assert machine_id(a_conn) == machine_id(a_conn)
    assert machine_id(a_conn) != machine_id(b_conn)
    assert all(ch.isalnum() or ch in "-_" for ch in machine_id(a_conn))


def test_restore_import_still_lets_the_archive_win(dbs, tmp_path) -> None:
    """File > Import Project keeps its 0.2.0 meaning: the archive's version wins even if
    the local copy was edited later."""
    a_conn, b_conn = dbs("a"), dbs("b")
    folder = tmp_path / "shared"
    song = MediaRepository(a_conn).insert(_media("file:///song.mp3", "fp"))
    bookmark = BookmarkRepository(a_conn).insert(_bookmark(song.id, name="from archive"))
    a = SyncService(a_conn, folder)
    a.export_now()
    b = SyncService(b_conn, folder)
    b.sync_now()
    _tick()
    BookmarkRepository(b_conn).update(replace(bookmark, name="local, newer"))
    stats = apply_import_plan(b_conn, read_import_plan(a.own_path), mode="restore")
    assert stats.bookmarks_written == 1
    assert _names(b_conn)[bookmark.id] == "from archive"


def test_unknown_import_mode_is_rejected(dbs, tmp_path) -> None:
    conn = dbs("a")
    service = SyncService(conn, tmp_path / "shared")
    service.export_now()
    with pytest.raises(ValueError):
        apply_import_plan(conn, read_import_plan(service.own_path), mode="merge")


def test_application_merges_on_start_and_refreshes(qtbot, dbs, tmp_path) -> None:
    from bookmark_studio.app.application import Application
    from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter

    other, mine = dbs("other"), dbs("mine")
    folder = tmp_path / "shared"
    song = MediaRepository(other).insert(_media("file:///song.mp3", "fp"))
    bookmark = BookmarkRepository(other).insert(_bookmark(song.id))
    SyncService(other, folder).sync_now()

    app = Application(conn=mine, adapter=MockPlaybackAdapter([]), ffmpeg_path="/nonexistent-ffmpeg",
                      waveform_cache_dir=tmp_path / "cache", sync_service=SyncService(mine, folder))
    qtbot.addWidget(app.window)
    try:
        app.start()
        assert BookmarkRepository(mine).get(bookmark.id) is not None
        assert len(list(folder.glob("vlc-bookmark-studio-sync-*.vlcbmk"))) == 2  # published its own file
    finally:
        app.stop()
