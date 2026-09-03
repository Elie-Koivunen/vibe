from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.domain.media import Media
from bookmark_studio.domain.playlist import Playlist
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.migrations import current_version, migrate
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.persistence.waveform_repository import WaveformCacheEntry, WaveformCacheRepository


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    migrate(connection)
    return connection


def test_migrate_applies_schema_and_is_idempotent(conn: sqlite3.Connection) -> None:
    assert current_version(conn) == 3
    # Re-running must not error and must not reapply.
    assert migrate(conn) == 3
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"playlists", "media", "bookmarks", "lanes", "waveform_cache"} <= tables


def test_playlist_roundtrip(conn: sqlite3.Connection) -> None:
    repo = PlaylistRepository(conn)
    playlist = Playlist(id=uuid4(), name="Guitar Practice", source_uri=None, is_ad_hoc=True)
    repo.insert(playlist)

    fetched = repo.get(playlist.id)
    assert fetched is not None
    assert fetched.playlist.name == "Guitar Practice"

    repo.add_signature(playlist.id, "abc123")
    by_sig = repo.find_by_signature("abc123")
    assert by_sig is not None
    assert by_sig.playlist.id == playlist.id


def test_media_resolve_by_uri_and_alias(conn: sqlite3.Connection) -> None:
    repo = MediaRepository(conn)
    media = Media(
        id=uuid4(),
        canonical_uri="file:///C:/Music/song.mp3",
        filename="song.mp3",
        title="Song",
        artist="Artist",
        album=None,
        duration_us=200_000_000,
        file_size=123,
        mtime_ns=456,
        fast_fingerprint="fp1",
    )
    repo.insert(media)
    assert repo.resolve_by_uri("file:///C:/Music/song.mp3") is not None
    assert repo.resolve_by_fingerprint("fp1").id == media.id

    repo.relocate(media.id, "file:///D:/Music/song.mp3")
    assert repo.resolve_by_uri("file:///D:/Music/song.mp3").id == media.id
    # Old URI still resolves via alias (spec #103).
    assert repo.resolve_by_uri("file:///C:/Music/song.mp3").id == media.id


def test_bookmark_playlist_media_scope_isolation(conn: sqlite3.Connection) -> None:
    """Release-blocking regression from spec #163: two playlists, same song, isolated bookmarks."""
    media_repo = MediaRepository(conn)
    playlist_repo = PlaylistRepository(conn)
    bookmark_repo = BookmarkRepository(conn)

    song = Media(
        id=uuid4(),
        canonical_uri="file:///song.mp3",
        filename="song.mp3",
        title=None,
        artist=None,
        album=None,
        duration_us=400_000_000,
        file_size=1,
        mtime_ns=1,
        fast_fingerprint="fp",
    )
    media_repo.insert(song)

    playlist_a = Playlist(id=uuid4(), name="A", source_uri=None, is_ad_hoc=True)
    playlist_b = Playlist(id=uuid4(), name="B", source_uri=None, is_ad_hoc=True)
    playlist_repo.insert(playlist_a)
    playlist_repo.insert(playlist_b)

    bookmark_a = Bookmark(
        id=uuid4(),
        playlist_id=playlist_a.id,
        media_id=song.id,
        scope=BookmarkScope.PLAYLIST_MEDIA,
        lane_id=None,
        bookmark_type=BookmarkType.POINT,
        name="Bookmark A",
        start_us=1_000,
        end_us=None,
        loop_enabled=False,
        repeat_count=None,
        loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    bookmark_b = Bookmark(
        id=uuid4(),
        playlist_id=playlist_b.id,
        media_id=song.id,
        scope=BookmarkScope.PLAYLIST_MEDIA,
        lane_id=None,
        bookmark_type=BookmarkType.POINT,
        name="Bookmark B",
        start_us=2_000,
        end_us=None,
        loop_enabled=False,
        repeat_count=None,
        loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    bookmark_repo.insert(bookmark_a)
    bookmark_repo.insert(bookmark_b)

    only_a = bookmark_repo.list_for_playlist_media(playlist_a.id, song.id)
    assert [b.name for b in only_a] == ["Bookmark A"]

    only_b = bookmark_repo.list_for_playlist_media(playlist_b.id, song.id)
    assert [b.name for b in only_b] == ["Bookmark B"]


def test_bookmark_tags_roundtrip(conn: sqlite3.Connection) -> None:
    media_repo = MediaRepository(conn)
    media = Media(
        id=uuid4(),
        canonical_uri="file:///x.mp3",
        filename="x.mp3",
        title=None,
        artist=None,
        album=None,
        duration_us=None,
        file_size=None,
        mtime_ns=None,
        fast_fingerprint=None,
    )
    media_repo.insert(media)

    bookmark_repo = BookmarkRepository(conn)
    bookmark = Bookmark(
        id=uuid4(),
        playlist_id=None,
        media_id=media.id,
        scope=BookmarkScope.GLOBAL_MEDIA,
        lane_id=None,
        bookmark_type=BookmarkType.SEGMENT,
        name="Chorus",
        start_us=0,
        end_us=1_000,
        loop_enabled=True,
        repeat_count=None,
        loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
        tags=("chorus", "practice"),
    )
    bookmark_repo.insert(bookmark)

    fetched = bookmark_repo.get(bookmark.id)
    assert fetched is not None
    assert fetched.tags == ("chorus", "practice")

    updated = replace(bookmark, tags=("solo",))
    bookmark_repo.update(updated)
    assert bookmark_repo.get(bookmark.id).tags == ("solo",)


def test_bookmark_delete(conn: sqlite3.Connection) -> None:
    media_repo = MediaRepository(conn)
    media = Media(
        id=uuid4(), canonical_uri=None, filename=None, title=None, artist=None,
        album=None, duration_us=None, file_size=None, mtime_ns=None, fast_fingerprint=None,
    )
    media_repo.insert(media)
    bookmark_repo = BookmarkRepository(conn)
    bookmark = Bookmark(
        id=uuid4(), playlist_id=None, media_id=media.id, scope=BookmarkScope.GLOBAL_MEDIA,
        lane_id=None, bookmark_type=BookmarkType.POINT, name="X", start_us=0, end_us=None,
        loop_enabled=False, repeat_count=None, loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    bookmark_repo.insert(bookmark)
    bookmark_repo.delete(bookmark.id)
    assert bookmark_repo.get(bookmark.id) is None


def test_list_for_playlist_spans_every_song_in_the_playlist(conn: sqlite3.Connection) -> None:
    """Direct user request: "the bookmarks should all be listed for all songs"."""
    media_repo = MediaRepository(conn)
    playlist_repo = PlaylistRepository(conn)
    bookmark_repo = BookmarkRepository(conn)

    song_a = Media(
        id=uuid4(), canonical_uri="file:///a.mp3", filename="a.mp3", title=None, artist=None,
        album=None, duration_us=None, file_size=None, mtime_ns=None, fast_fingerprint=None,
    )
    song_b = Media(
        id=uuid4(), canonical_uri="file:///b.mp3", filename="b.mp3", title=None, artist=None,
        album=None, duration_us=None, file_size=None, mtime_ns=None, fast_fingerprint=None,
    )
    media_repo.insert(song_a)
    media_repo.insert(song_b)
    playlist = Playlist(id=uuid4(), name="P", source_uri=None, is_ad_hoc=True)
    playlist_repo.insert(playlist)

    def _bookmark(media_id, name, start_us):
        return Bookmark(
            id=uuid4(), playlist_id=playlist.id, media_id=media_id, scope=BookmarkScope.PLAYLIST_MEDIA,
            lane_id=None, bookmark_type=BookmarkType.POINT, name=name, start_us=start_us, end_us=None,
            loop_enabled=False, repeat_count=None, loop_gap_ms=0, completion_action=CompletionAction.CONTINUE,
        )

    bookmark_repo.insert(_bookmark(song_a.id, "A1", 5_000_000))
    bookmark_repo.insert(_bookmark(song_b.id, "B1", 1_000_000))

    all_bookmarks = bookmark_repo.list_for_playlist(playlist.id)
    assert {b.name for b in all_bookmarks} == {"A1", "B1"}


def test_reorder_persists_manual_order_across_songs(conn: sqlite3.Connection) -> None:
    """Direct user request: "the row entries should also be possible to manually
    reorder them moving up/down"."""
    media_repo = MediaRepository(conn)
    playlist_repo = PlaylistRepository(conn)
    bookmark_repo = BookmarkRepository(conn)

    song = Media(
        id=uuid4(), canonical_uri="file:///a.mp3", filename="a.mp3", title=None, artist=None,
        album=None, duration_us=None, file_size=None, mtime_ns=None, fast_fingerprint=None,
    )
    media_repo.insert(song)
    playlist = Playlist(id=uuid4(), name="P", source_uri=None, is_ad_hoc=True)
    playlist_repo.insert(playlist)

    def _bookmark(name, start_us):
        return Bookmark(
            id=uuid4(), playlist_id=playlist.id, media_id=song.id, scope=BookmarkScope.PLAYLIST_MEDIA,
            lane_id=None, bookmark_type=BookmarkType.POINT, name=name, start_us=start_us, end_us=None,
            loop_enabled=False, repeat_count=None, loop_gap_ms=0, completion_action=CompletionAction.CONTINUE,
        )

    first = _bookmark("First", 1_000_000)
    second = _bookmark("Second", 2_000_000)
    bookmark_repo.insert(first)
    bookmark_repo.insert(second)

    # Default order follows start_us.
    assert [b.name for b in bookmark_repo.list_for_playlist(playlist.id)] == ["First", "Second"]

    # Manually move "Second" above "First", even though its start_us is later.
    bookmark_repo.reorder([second.id, first.id])

    assert [b.name for b in bookmark_repo.list_for_playlist(playlist.id)] == ["Second", "First"]


def test_rename_legacy_default_names(conn: sqlite3.Connection) -> None:
    """Regression, reported live: "the first bookmark doesn't have the naming
    convention" -- a bookmark created before default_bookmark_name() existed keeps
    its literal old "New bookmark" name forever unless backfilled once."""
    media_repo = MediaRepository(conn)
    media = Media(
        id=uuid4(), canonical_uri=None, filename=None, title=None, artist=None,
        album=None, duration_us=None, file_size=None, mtime_ns=None, fast_fingerprint=None,
    )
    media_repo.insert(media)
    bookmark_repo = BookmarkRepository(conn)
    legacy = Bookmark(
        id=uuid4(), playlist_id=None, media_id=media.id, scope=BookmarkScope.GLOBAL_MEDIA,
        lane_id=None, bookmark_type=BookmarkType.POINT, name="New bookmark", start_us=0, end_us=None,
        loop_enabled=False, repeat_count=None, loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    already_named = replace(legacy, id=uuid4(), name="Chorus")
    bookmark_repo.insert(legacy)
    bookmark_repo.insert(already_named)

    renamed_count = bookmark_repo.rename_legacy_default_names()

    assert renamed_count == 1
    assert bookmark_repo.get(legacy.id).name != "New bookmark"
    assert re.fullmatch(r"bookmark-\d{8}-[a-z0-9]{6}", bookmark_repo.get(legacy.id).name)
    assert bookmark_repo.get(already_named.id).name == "Chorus"  # untouched

    # Idempotent: a second run finds nothing left to rename.
    assert bookmark_repo.rename_legacy_default_names() == 0


def test_waveform_cache_roundtrip(conn: sqlite3.Connection) -> None:
    media_repo = MediaRepository(conn)
    media = Media(
        id=uuid4(), canonical_uri=None, filename=None, title=None, artist=None,
        album=None, duration_us=None, file_size=None, mtime_ns=None, fast_fingerprint=None,
    )
    media_repo.insert(media)

    repo = WaveformCacheRepository(conn)
    entry = WaveformCacheEntry(
        cache_key="key1",
        media_id=media.id,
        algorithm_version=1,
        sample_rate=8000,
        channel_mode="mono",
        file_path="/tmp/key1.npz",
    )
    repo.put(entry)
    assert repo.lookup("key1") == entry
    repo.invalidate("key1")
    assert repo.lookup("key1") is None
