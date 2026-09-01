"""PlaylistRepository: playlists + playlist_signatures tables (spec #73-#74, #79)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from bookmark_studio.domain.playlist import Playlist


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class PlaylistRecord:
    """A Playlist plus repository-owned bookkeeping fields not in the domain object."""

    playlist: Playlist
    created_at: str
    updated_at: str
    last_seen_at: str | None


class PlaylistRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, playlist_id: UUID) -> PlaylistRecord | None:
        row = self._conn.execute(
            "SELECT id, name, source_uri, is_ad_hoc, created_at, updated_at, last_seen_at "
            "FROM playlists WHERE id = ?",
            (str(playlist_id),),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def find_by_source_uri(self, source_uri: str) -> PlaylistRecord | None:
        row = self._conn.execute(
            "SELECT id, name, source_uri, is_ad_hoc, created_at, updated_at, last_seen_at "
            "FROM playlists WHERE source_uri = ?",
            (source_uri,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def find_by_signature(self, signature: str) -> PlaylistRecord | None:
        row = self._conn.execute(
            "SELECT p.id, p.name, p.source_uri, p.is_ad_hoc, p.created_at, p.updated_at, "
            "p.last_seen_at FROM playlists p "
            "JOIN playlist_signatures s ON s.playlist_id = p.id "
            "WHERE s.signature = ?",
            (signature,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def insert(self, playlist: Playlist) -> PlaylistRecord:
        now = _now()
        self._conn.execute(
            "INSERT INTO playlists (id, name, source_uri, created_at, updated_at, "
            "last_seen_at, is_ad_hoc) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(playlist.id),
                playlist.name,
                playlist.source_uri,
                now,
                now,
                now,
                int(playlist.is_ad_hoc),
            ),
        )
        self._conn.commit()
        return PlaylistRecord(playlist=playlist, created_at=now, updated_at=now, last_seen_at=now)

    def update(self, playlist: Playlist) -> None:
        now = _now()
        self._conn.execute(
            "UPDATE playlists SET name = ?, source_uri = ?, updated_at = ?, "
            "last_seen_at = ?, is_ad_hoc = ? WHERE id = ?",
            (
                playlist.name,
                playlist.source_uri,
                now,
                now,
                int(playlist.is_ad_hoc),
                str(playlist.id),
            ),
        )
        self._conn.commit()

    def add_signature(self, playlist_id: UUID, signature: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO playlist_signatures (playlist_id, signature, created_at) "
            "VALUES (?, ?, ?)",
            (str(playlist_id), signature, _now()),
        )
        self._conn.commit()

    def list_recent(self, limit: int = 20) -> list[PlaylistRecord]:
        rows = self._conn.execute(
            "SELECT id, name, source_uri, is_ad_hoc, created_at, updated_at, last_seen_at "
            "FROM playlists ORDER BY last_seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row | tuple) -> PlaylistRecord:
        playlist_id, name, source_uri, is_ad_hoc, created_at, updated_at, last_seen_at = row
        return PlaylistRecord(
            playlist=Playlist(
                id=UUID(playlist_id),
                name=name,
                source_uri=source_uri,
                is_ad_hoc=bool(is_ad_hoc),
            ),
            created_at=created_at,
            updated_at=updated_at,
            last_seen_at=last_seen_at,
        )
