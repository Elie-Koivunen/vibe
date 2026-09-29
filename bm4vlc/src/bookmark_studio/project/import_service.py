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

Import MERGES into the local database:

* Rows are upserted (INSERT ... ON CONFLICT DO UPDATE), never `INSERT OR REPLACE`.
  REPLACE deletes the existing row first, and the ON DELETE CASCADE foreign keys then
  silently wiped that playlist's signatures and items (so VLC stopped recognising it)
  and that media's URI aliases and waveform-cache rows.
* A media file the local database already knows (same id, or same content fingerprint
  -- e.g. a project exported on another machine) is mapped onto the local record
  instead of creating a disconnected duplicate the live VLC playlist would never match.
* Likewise a playlist whose signature or source file is already known locally is
  merged into that local playlist.
"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

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
    playlist_items: list[tuple[UUID, list[UUID]]] = field(default_factory=list)
    playlist_signatures: list[tuple[UUID, str]] = field(default_factory=list)


def _optional_json(archive: zipfile.ZipFile, name: str) -> list:
    try:
        return json.loads(archive.read(name))
    except KeyError:  # not present in format_version 1 archives
        return []


def read_import_plan(path: Path) -> ImportPlan:
    """Validates the archive and every record in it without touching the database."""
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        validate_manifest(manifest)
        bookmarks_raw = json.loads(archive.read("bookmarks.json"))
        playlists_raw = json.loads(archive.read("playlists.json"))
        media_raw = json.loads(archive.read("media.json"))
        lanes_raw = json.loads(archive.read("lanes.json"))
        items_raw = _optional_json(archive, "playlist_items.json")
        signatures_raw = _optional_json(archive, "playlist_signatures.json")

    playlists = [playlist_from_dict(entry) for entry in playlists_raw]
    media = [media_from_dict(entry) for entry in media_raw]
    lanes = [lane_from_dict(entry) for entry in lanes_raw]
    bookmarks = [bookmark_from_dict(entry) for entry in bookmarks_raw]
    playlist_items = [
        (UUID(entry["playlist_id"]), [UUID(m) for m in entry.get("media_ids", [])]) for entry in items_raw
    ]
    playlist_signatures = [(UUID(entry["playlist_id"]), str(entry["signature"])) for entry in signatures_raw]
    return ImportPlan(
        playlists=playlists, media=media, lanes=lanes, bookmarks=bookmarks,
        playlist_items=playlist_items, playlist_signatures=playlist_signatures,
    )


def _map_media(conn: sqlite3.Connection, plan: ImportPlan, now: str) -> dict[UUID, UUID]:
    mapping: dict[UUID, UUID] = {}
    for media in plan.media:
        if conn.execute("SELECT 1 FROM media WHERE id = ?", (str(media.id),)).fetchone():
            mapping[media.id] = media.id
            continue
        if media.fast_fingerprint:
            row = conn.execute(
                "SELECT id FROM media WHERE fast_fingerprint = ? LIMIT 1", (media.fast_fingerprint,)
            ).fetchone()
            if row:
                mapping[media.id] = UUID(row[0])
                continue
        conn.execute(
            "INSERT INTO media "
            "(id, canonical_uri, filename, title, artist, album, duration_us, "
            "file_size, mtime_ns, fast_fingerprint, full_sha256, created_at, "
            "updated_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(media.id), media.canonical_uri, media.filename, media.title,
                media.artist, media.album, media.duration_us, media.file_size,
                media.mtime_ns, media.fast_fingerprint, media.full_sha256, now, now, now,
            ),
        )
        mapping[media.id] = media.id
    return mapping


def _map_playlists(conn: sqlite3.Connection, plan: ImportPlan, now: str) -> dict[UUID, UUID]:
    signatures_by_playlist: dict[UUID, list[str]] = {}
    for playlist_id, signature in plan.playlist_signatures:
        signatures_by_playlist.setdefault(playlist_id, []).append(signature)

    mapping: dict[UUID, UUID] = {}
    for playlist in plan.playlists:
        if conn.execute("SELECT 1 FROM playlists WHERE id = ?", (str(playlist.id),)).fetchone():
            conn.execute(
                "UPDATE playlists SET name = ?, source_uri = COALESCE(?, source_uri), "
                "is_ad_hoc = ?, updated_at = ? WHERE id = ?",
                (playlist.name, playlist.source_uri, int(playlist.is_ad_hoc), now, str(playlist.id)),
            )
            mapping[playlist.id] = playlist.id
            continue
        local_id: str | None = None
        for signature in signatures_by_playlist.get(playlist.id, []):
            row = conn.execute(
                "SELECT playlist_id FROM playlist_signatures WHERE signature = ?", (signature,)
            ).fetchone()
            if row:
                local_id = row[0]
                break
        if local_id is None and playlist.source_uri:
            row = conn.execute(
                "SELECT id FROM playlists WHERE source_uri = ? LIMIT 1", (playlist.source_uri,)
            ).fetchone()
            if row:
                local_id = row[0]
        if local_id is not None:
            mapping[playlist.id] = UUID(local_id)
            continue
        conn.execute(
            "INSERT INTO playlists (id, name, source_uri, created_at, updated_at, last_seen_at, is_ad_hoc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(playlist.id), playlist.name, playlist.source_uri, now, now, now, int(playlist.is_ad_hoc)),
        )
        mapping[playlist.id] = playlist.id
    return mapping


def apply_import_plan(conn: sqlite3.Connection, plan: ImportPlan) -> None:
    """Writes an already-validated plan in one transaction. Raises and rolls back on
    any database-level error (e.g. a foreign key violation)."""
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        media_map = _map_media(conn, plan, now)
        playlist_map = _map_playlists(conn, plan, now)

        for playlist_id, signature in plan.playlist_signatures:
            local = playlist_map.get(playlist_id)
            if local is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO playlist_signatures (playlist_id, signature, created_at) VALUES (?, ?, ?)",
                (str(local), signature, now),
            )
        for playlist_id, media_ids in plan.playlist_items:
            local = playlist_map.get(playlist_id)
            if local is None or conn.execute(
                "SELECT 1 FROM playlist_items WHERE playlist_id = ? LIMIT 1", (str(local),)
            ).fetchone():
                continue  # keep the local order if we already have one
            occurrences: dict[UUID, int] = {}
            for ordinal, media_id in enumerate(media_ids):
                mapped = media_map.get(media_id, media_id)
                occurrence = occurrences.get(mapped, 0)
                occurrences[mapped] = occurrence + 1
                conn.execute(
                    "INSERT INTO playlist_items (id, playlist_id, media_id, ordinal, occurrence_index) "
                    "SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM media WHERE id = ?)",
                    (str(uuid4()), str(local), str(mapped), ordinal, occurrence, str(mapped)),
                )

        for lane in plan.lanes:
            conn.execute(
                "INSERT INTO lanes "
                "(id, playlist_id, name, order_index, visible, locked, color_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET playlist_id = excluded.playlist_id, name = excluded.name, "
                "order_index = excluded.order_index, visible = excluded.visible, "
                "locked = excluded.locked, color_key = excluded.color_key",
                (
                    str(lane.id), str(playlist_map.get(lane.playlist_id, lane.playlist_id)), lane.name,
                    lane.order_index, int(lane.visible), int(lane.locked), lane.color_key, now,
                ),
            )

        for bookmark in plan.bookmarks:
            playlist_id = playlist_map.get(bookmark.playlist_id, bookmark.playlist_id) if bookmark.playlist_id else None
            media_id = media_map.get(bookmark.media_id, bookmark.media_id)
            conn.execute(
                "INSERT INTO bookmarks "
                "(id, playlist_id, media_id, scope, lane_id, bookmark_type, name, "
                "start_us, end_us, loop_enabled, repeat_count, loop_gap_ms, "
                "completion_action, color_key, notes, sort_index, fade_in_ms, fade_out_ms, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET playlist_id = excluded.playlist_id, "
                "media_id = excluded.media_id, scope = excluded.scope, lane_id = excluded.lane_id, "
                "bookmark_type = excluded.bookmark_type, name = excluded.name, "
                "start_us = excluded.start_us, end_us = excluded.end_us, "
                "loop_enabled = excluded.loop_enabled, repeat_count = excluded.repeat_count, "
                "loop_gap_ms = excluded.loop_gap_ms, completion_action = excluded.completion_action, "
                "color_key = excluded.color_key, notes = excluded.notes, sort_index = excluded.sort_index, "
                "fade_in_ms = excluded.fade_in_ms, fade_out_ms = excluded.fade_out_ms, "
                "updated_at = excluded.updated_at",
                (
                    str(bookmark.id),
                    str(playlist_id) if playlist_id else None,
                    str(media_id), bookmark.scope.value,
                    str(bookmark.lane_id) if bookmark.lane_id else None,
                    bookmark.bookmark_type.value, bookmark.name, bookmark.start_us,
                    bookmark.end_us, int(bookmark.loop_enabled), bookmark.repeat_count,
                    bookmark.loop_gap_ms, bookmark.completion_action.value,
                    bookmark.color_key, bookmark.notes, bookmark.sort_index,
                    bookmark.fade_in_ms, bookmark.fade_out_ms, now, now,
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
