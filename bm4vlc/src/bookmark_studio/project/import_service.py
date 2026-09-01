"""Validates and imports a .vlcbmk archive inside one transaction (spec #127).

Deliberately bypasses the auto-committing repository classes for the write phase and
uses raw SQL inside a single `with conn:` block instead. The repositories elsewhere in
this codebase call `conn.commit()` after every individual write, which is correct for
spec #81's Autosave model (each user edit is durable immediately) but would defeat
atomicity here: if repo calls were chained and a later one failed, the earlier ones
would already be permanently committed, contradicting spec #127's explicit "Any error:
ROLLBACK." Import is validated fully (every dict parsed into a domain object, which
runs Bookmark's own range validation) before a single row is written, so the
transaction below only needs to guard against database-level failures, not domain
validation failures.
"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.lane import Lane
from bookmark_studio.domain.media import Media
from bookmark_studio.domain.playlist import Playlist
from bookmark_studio.project.schema import (
    bookmark_from_dict,
    lane_from_dict,
    media_from_dict,
    playlist_from_dict,
    validate_manifest,
)


@dataclass(frozen=True, slots=True)
class ImportPlan:
    playlists: list[Playlist]
    media: list[Media]
    lanes: list[Lane]
    bookmarks: list[Bookmark]


def read_import_plan(path: Path) -> ImportPlan:
    """Validates the archive and every record in it without touching the database."""
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        validate_manifest(manifest)
        bookmarks_raw = json.loads(archive.read("bookmarks.json"))
        playlists_raw = json.loads(archive.read("playlists.json"))
        media_raw = json.loads(archive.read("media.json"))
        lanes_raw = json.loads(archive.read("lanes.json"))

    playlists = [playlist_from_dict(entry) for entry in playlists_raw]
    media = [media_from_dict(entry) for entry in media_raw]
    lanes = [lane_from_dict(entry) for entry in lanes_raw]
    bookmarks = [bookmark_from_dict(entry) for entry in bookmarks_raw]
    return ImportPlan(playlists=playlists, media=media, lanes=lanes, bookmarks=bookmarks)


def apply_import_plan(conn: sqlite3.Connection, plan: ImportPlan) -> None:
    """Writes an already-validated plan in one transaction. Raises and rolls back on
    any database-level error (e.g. a foreign key violation)."""
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        for playlist in plan.playlists:
            conn.execute(
                "INSERT OR REPLACE INTO playlists "
                "(id, name, source_uri, created_at, updated_at, last_seen_at, is_ad_hoc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(playlist.id), playlist.name, playlist.source_uri, now, now, now, int(playlist.is_ad_hoc)),
            )
        for media in plan.media:
            conn.execute(
                "INSERT OR REPLACE INTO media "
                "(id, canonical_uri, filename, title, artist, album, duration_us, "
                "file_size, mtime_ns, fast_fingerprint, full_sha256, created_at, "
                "updated_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(media.id), media.canonical_uri, media.filename, media.title,
                    media.artist, media.album, media.duration_us, media.file_size,
                    media.mtime_ns, media.fast_fingerprint, media.full_sha256, now, now, now,
                ),
            )
        for lane in plan.lanes:
            conn.execute(
                "INSERT OR REPLACE INTO lanes "
                "(id, playlist_id, name, order_index, visible, locked, color_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(lane.id), str(lane.playlist_id), lane.name, lane.order_index,
                    int(lane.visible), int(lane.locked), lane.color_key, now,
                ),
            )
        for bookmark in plan.bookmarks:
            conn.execute(
                "INSERT OR REPLACE INTO bookmarks "
                "(id, playlist_id, media_id, scope, lane_id, bookmark_type, name, "
                "start_us, end_us, loop_enabled, repeat_count, loop_gap_ms, "
                "completion_action, color_key, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(bookmark.id),
                    str(bookmark.playlist_id) if bookmark.playlist_id else None,
                    str(bookmark.media_id), bookmark.scope.value,
                    str(bookmark.lane_id) if bookmark.lane_id else None,
                    bookmark.bookmark_type.value, bookmark.name, bookmark.start_us,
                    bookmark.end_us, int(bookmark.loop_enabled), bookmark.repeat_count,
                    bookmark.loop_gap_ms, bookmark.completion_action.value,
                    bookmark.color_key, bookmark.notes, now, now,
                ),
            )
            conn.execute(
                "DELETE FROM bookmark_tags WHERE bookmark_id = ?", (str(bookmark.id),)
            )
            conn.executemany(
                "INSERT INTO bookmark_tags (bookmark_id, tag) VALUES (?, ?)",
                [(str(bookmark.id), tag) for tag in bookmark.tags],
            )


def import_project(conn: sqlite3.Connection, path: Path) -> ImportPlan:
    plan = read_import_plan(path)
    apply_import_plan(conn, plan)
    return plan
