from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.domain.lane import Lane
from bookmark_studio.domain.media import Media
from bookmark_studio.domain.playlist import Playlist
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.project.export_service import ProjectData, export_project
from bookmark_studio.project.import_service import apply_import_plan, import_project, read_import_plan
from bookmark_studio.project.schema import ProjectFormatUnsupported


def _sample_data() -> ProjectData:
    playlist = Playlist(id=uuid4(), name="Guitar Practice", source_uri=None, is_ad_hoc=True)
    media = Media(
        id=uuid4(), canonical_uri="file:///song.mp3", filename="song.mp3", title="Song",
        artist=None, album=None, duration_us=300_000_000, file_size=1, mtime_ns=1,
        fast_fingerprint="fp",
    )
    lane = Lane(id=uuid4(), playlist_id=playlist.id, name="Structure", order_index=0)
    bookmark = Bookmark(
        id=uuid4(), playlist_id=playlist.id, media_id=media.id, scope=BookmarkScope.PLAYLIST_MEDIA,
        lane_id=lane.id, bookmark_type=BookmarkType.SEGMENT, name="Chorus", start_us=1000,
        end_us=2000, loop_enabled=True, repeat_count=3, loop_gap_ms=100,
        completion_action=CompletionAction.PAUSE, tags=("practice",),
    )
    return ProjectData(playlists=[playlist], media=[media], bookmarks=[bookmark], lanes=[lane])


def test_export_then_import_roundtrip(tmp_path: Path) -> None:
    data = _sample_data()
    archive_path = tmp_path / "project.vlcbmk"
    export_project(archive_path, data)
    assert archive_path.exists()
    assert not archive_path.with_suffix(archive_path.suffix + ".tmp").exists()

    conn = sqlite3.connect(":memory:")
    migrate(conn)
    plan = import_project(conn, archive_path)

    assert len(plan.bookmarks) == 1
    bookmark_repo = BookmarkRepository(conn)
    fetched = bookmark_repo.get(data.bookmarks[0].id)
    assert fetched is not None
    assert fetched.name == "Chorus"
    assert fetched.tags == ("practice",)
    assert fetched.loop_enabled is True
    assert fetched.repeat_count == 3


def test_import_rejects_newer_major_format_version(tmp_path: Path) -> None:
    archive_path = tmp_path / "future.vlcbmk"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": "vlc-bookmark-studio", "format_version": 99}))
        archive.writestr("bookmarks.json", "[]")
        archive.writestr("playlists.json", "[]")
        archive.writestr("media.json", "[]")
        archive.writestr("lanes.json", "[]")

    with pytest.raises(ProjectFormatUnsupported):
        read_import_plan(archive_path)


def test_import_rejects_wrong_format_name(tmp_path: Path) -> None:
    archive_path = tmp_path / "other.vlcbmk"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": "some-other-app", "format_version": 1}))
        archive.writestr("bookmarks.json", "[]")
        archive.writestr("playlists.json", "[]")
        archive.writestr("media.json", "[]")
        archive.writestr("lanes.json", "[]")

    with pytest.raises(ProjectFormatUnsupported):
        read_import_plan(archive_path)


def test_import_ignores_unknown_manifest_fields(tmp_path: Path) -> None:
    """spec #91: unknown fields must be ignored, for forward compatibility."""
    archive_path = tmp_path / "forward_compat.vlcbmk"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"format": "vlc-bookmark-studio", "format_version": 1, "some_future_field": True}),
        )
        archive.writestr("bookmarks.json", "[]")
        archive.writestr("playlists.json", "[]")
        archive.writestr("media.json", "[]")
        archive.writestr("lanes.json", "[]")

    plan = read_import_plan(archive_path)
    assert plan.bookmarks == []


def test_apply_import_plan_rolls_back_on_db_error(tmp_path: Path) -> None:
    """A bookmark referencing a non-existent media_id should violate the FK and roll
    back the entire batch (spec #127: 'Then one transaction'; #80: 'Any error: ROLLBACK')."""
    data = _sample_data()
    archive_path = tmp_path / "project.vlcbmk"
    export_project(archive_path, data)

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    plan = read_import_plan(archive_path)

    # Corrupt one bookmark's media_id so it can't satisfy the foreign key -- but leave
    # the playlist/media/lane inserts, which would otherwise succeed on their own.
    from dataclasses import replace
    broken_bookmark = replace(plan.bookmarks[0], media_id=uuid4())
    broken_plan = replace(plan, bookmarks=[broken_bookmark])

    with pytest.raises(sqlite3.IntegrityError):
        apply_import_plan(conn, broken_plan)

    # Nothing from this batch should have been committed -- not even the playlist.
    row = conn.execute("SELECT COUNT(*) FROM playlists").fetchone()
    assert row[0] == 0
