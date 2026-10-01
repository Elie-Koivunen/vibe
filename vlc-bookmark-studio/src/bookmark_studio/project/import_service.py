"""Validates and imports a .vlcbmk archive inside one transaction (spec #127).

The write phase uses raw SQL in a single `with conn:` block rather than the repositories
(which commit after every write, as spec #81's autosave wants): an import is all or
nothing -- any database error rolls everything back. Every record is validated (parsed
into a domain object) before the first row is written.

Import MERGES into the local database:

* Rows are upserted (INSERT ... ON CONFLICT DO UPDATE), never `INSERT OR REPLACE`, whose
  delete-then-insert let ON DELETE CASCADE wipe a playlist's signatures and items and a
  media's aliases and waveform-cache rows.
* A media file the local database already knows (same id, or same content fingerprint,
  e.g. from another machine) is mapped onto the local record.
* A playlist already known locally (same id, same signature -- also after mapping its
  songs onto local media -- or same source file) is merged into the local playlist.

Two modes:

* ``restore`` (File > Import Project): the archive's version of each bookmark wins.
* ``sync`` (folder sync between machines): the most recent change wins, bookmark by
  bookmark, and deletions (tombstones) are applied the same way.
"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.lane import Lane
from bookmark_studio.domain.media import Media
from bookmark_studio.domain.playlist import Playlist
from bookmark_studio.persistence.tag_repository import TagRepository
from bookmark_studio.playlist.signatures import strict_signature
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
    # Sync metadata (sync.json): when each bookmark last changed, and deletions.
    bookmark_updated_at: dict[UUID, str] = field(default_factory=dict)
    tombstones: dict[UUID, str] = field(default_factory=dict)
    # tags.json (0.7.0): the tag catalog and its renames/removals.
    tag_state: dict[str, Any] | None = None


@dataclass
class ImportStats:
    bookmarks_written: int = 0
    bookmarks_deleted: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.bookmarks_written or self.bookmarks_deleted)


def _optional_json(archive: zipfile.ZipFile, name: str, default: Any) -> Any:
    try:
        return json.loads(archive.read(name))
    except KeyError:  # not present in older archives
        return default


def read_import_plan(path: Path) -> ImportPlan:
    """Validates the archive and every record in it without touching the database."""
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        validate_manifest(manifest)
        bookmarks_raw = json.loads(archive.read("bookmarks.json"))
        playlists_raw = json.loads(archive.read("playlists.json"))
        media_raw = json.loads(archive.read("media.json"))
        lanes_raw = json.loads(archive.read("lanes.json"))
        items_raw = _optional_json(archive, "playlist_items.json", [])
        signatures_raw = _optional_json(archive, "playlist_signatures.json", [])
        sync_raw = _optional_json(archive, "sync.json", {})
        tags_raw = _optional_json(archive, "tags.json", None)

    return ImportPlan(
        playlists=[playlist_from_dict(entry) for entry in playlists_raw],
        media=[media_from_dict(entry) for entry in media_raw],
        lanes=[lane_from_dict(entry) for entry in lanes_raw],
        bookmarks=[bookmark_from_dict(entry) for entry in bookmarks_raw],
        playlist_items=[
            (UUID(entry["playlist_id"]), [UUID(m) for m in entry.get("media_ids", [])]) for entry in items_raw
        ],
        playlist_signatures=[(UUID(entry["playlist_id"]), str(entry["signature"])) for entry in signatures_raw],
        bookmark_updated_at={UUID(k): str(v) for k, v in (sync_raw.get("bookmark_updated_at") or {}).items()},
        tombstones={UUID(k): str(v) for k, v in (sync_raw.get("tombstones") or {}).items()},
        tag_state=tags_raw if isinstance(tags_raw, dict) else None,
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


def _map_playlists(conn: sqlite3.Connection, plan: ImportPlan, now: str, media_map: dict[UUID, UUID],
                   *, update_existing: bool = True) -> dict[UUID, UUID]:
    signatures_by_playlist: dict[UUID, list[str]] = {}
    for playlist_id, signature in plan.playlist_signatures:
        signatures_by_playlist.setdefault(playlist_id, []).append(signature)
    # Signatures hash media *ids*, which differ between databases (another machine, or
    # Windows vs. WSL). Re-hashing the playlist's order with the ids mapped onto local
    # media finds the same playlist here too.
    for playlist_id, media_ids in plan.playlist_items:
        mapped = [media_map.get(m, m) for m in media_ids]
        if mapped:
            signatures_by_playlist.setdefault(playlist_id, []).append(strict_signature(mapped))

    mapping: dict[UUID, UUID] = {}
    for playlist in plan.playlists:
        if conn.execute("SELECT 1 FROM playlists WHERE id = ?", (str(playlist.id),)).fetchone():
            if update_existing:
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


_EPOCH = "1970-01-01T00:00:00+00:00"

_UPSERT_BOOKMARK = (
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
    "updated_at = excluded.updated_at"
)


def _bookmark_wins(conn: sqlite3.Connection, bookmark_id: UUID, incoming_at: str) -> bool:
    """Sync rule: the incoming version is applied only if it is newer than both the local
    row and a local deletion of it."""
    row = conn.execute("SELECT updated_at FROM bookmarks WHERE id = ?", (str(bookmark_id),)).fetchone()
    if row is not None:
        return incoming_at > (row[0] or "")
    tomb = conn.execute(
        "SELECT deleted_at FROM bookmark_tombstones WHERE bookmark_id = ?", (str(bookmark_id),)
    ).fetchone()
    return tomb is None or incoming_at > tomb[0]


def apply_import_plan(conn: sqlite3.Connection, plan: ImportPlan, *, mode: str = "restore") -> ImportStats:
    """Writes an already-validated plan in one transaction (see the module docstring for
    the modes). Raises and rolls back on any database-level error (e.g. a foreign key
    violation)."""
    if mode not in ("restore", "sync"):
        raise ValueError(f"unknown import mode {mode!r}")
    sync = mode == "sync"
    stats = ImportStats()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        # The other side's tag renames/removals first, so the bookmarks below are stored
        # with current tag names (stale ones are mapped or dropped by canonical()).
        tags = TagRepository(conn)
        if plan.tag_state:
            tags.merge_state(plan.tag_state)
        media_map = _map_media(conn, plan, now)
        playlist_map = _map_playlists(conn, plan, now, media_map, update_existing=not sync)

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
            if local is None:
                continue
            mapped = [media_map.get(m, m) for m in media_ids]
            if mapped:  # the order as the other side knows it, in local ids
                conn.execute(
                    "INSERT OR IGNORE INTO playlist_signatures (playlist_id, signature, created_at) VALUES (?, ?, ?)",
                    (str(local), strict_signature(mapped), now),
                )
            if conn.execute("SELECT 1 FROM playlist_items WHERE playlist_id = ? LIMIT 1", (str(local),)).fetchone():
                continue  # keep the local order if we already have one
            occurrences: dict[UUID, int] = {}
            for ordinal, media_id in enumerate(mapped):
                occurrence = occurrences.get(media_id, 0)
                occurrences[media_id] = occurrence + 1
                conn.execute(
                    "INSERT INTO playlist_items (id, playlist_id, media_id, ordinal, occurrence_index) "
                    "SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM media WHERE id = ?)",
                    (str(uuid4()), str(local), str(media_id), ordinal, occurrence, str(media_id)),
                )

        # Lanes carry no timestamps: a sync adds missing ones but never overwrites.
        lane_conflict = (
            "ON CONFLICT(id) DO NOTHING" if sync else
            "ON CONFLICT(id) DO UPDATE SET playlist_id = excluded.playlist_id, name = excluded.name, "
            "order_index = excluded.order_index, visible = excluded.visible, "
            "locked = excluded.locked, color_key = excluded.color_key"
        )
        for lane in plan.lanes:
            conn.execute(
                "INSERT INTO lanes "
                "(id, playlist_id, name, order_index, visible, locked, color_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) " + lane_conflict,
                (
                    str(lane.id), str(playlist_map.get(lane.playlist_id, lane.playlist_id)), lane.name,
                    lane.order_index, int(lane.visible), int(lane.locked), lane.color_key, now,
                ),
            )

        for bookmark in plan.bookmarks:
            stamp = now
            if sync:
                # Without a timestamp the incoming copy counts as the oldest possible one.
                stamp = plan.bookmark_updated_at.get(bookmark.id, _EPOCH)
                if not _bookmark_wins(conn, bookmark.id, stamp):
                    continue
            local_playlist = (
                playlist_map.get(bookmark.playlist_id, bookmark.playlist_id) if bookmark.playlist_id else None
            )
            media_id = media_map.get(bookmark.media_id, bookmark.media_id)
            conn.execute(
                _UPSERT_BOOKMARK,
                (
                    str(bookmark.id),
                    str(local_playlist) if local_playlist else None,
                    str(media_id), bookmark.scope.value,
                    str(bookmark.lane_id) if bookmark.lane_id else None,
                    bookmark.bookmark_type.value, bookmark.name, bookmark.start_us,
                    bookmark.end_us, int(bookmark.loop_enabled), bookmark.repeat_count,
                    bookmark.loop_gap_ms, bookmark.completion_action.value,
                    bookmark.color_key, bookmark.notes, bookmark.sort_index,
                    bookmark.fade_in_ms, bookmark.fade_out_ms, stamp, stamp,
                ),
            )
            conn.execute("DELETE FROM bookmark_tags WHERE bookmark_id = ?", (str(bookmark.id),))
            conn.executemany(
                "INSERT INTO bookmark_tags (bookmark_id, tag) VALUES (?, ?)",
                [(str(bookmark.id), tag) for tag in tags.canonical(bookmark.tags)],
            )
            conn.execute("DELETE FROM bookmark_tombstones WHERE bookmark_id = ?", (str(bookmark.id),))
            stats.bookmarks_written += 1

        if sync:
            for bookmark_id, deleted_at in plan.tombstones.items():
                row = conn.execute("SELECT updated_at FROM bookmarks WHERE id = ?", (str(bookmark_id),)).fetchone()
                if row is not None:
                    if (row[0] or "") >= deleted_at:
                        continue  # changed here after it was deleted there: keep it
                    conn.execute("DELETE FROM bookmarks WHERE id = ?", (str(bookmark_id),))
                    stats.bookmarks_deleted += 1
                conn.execute(
                    "INSERT INTO bookmark_tombstones (bookmark_id, deleted_at) VALUES (?, ?) "
                    "ON CONFLICT(bookmark_id) DO UPDATE SET deleted_at = MAX(deleted_at, excluded.deleted_at)",
                    (str(bookmark_id), deleted_at),
                )
    return stats


def import_project(conn: sqlite3.Connection, path: Path) -> ImportPlan:
    plan = read_import_plan(path)
    apply_import_plan(conn, plan)
    return plan
