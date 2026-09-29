"""PlaylistRepository: playlists + playlist_signatures tables (spec #73-#74, #79)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

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

    def signatures_for(self, playlist_id: UUID) -> list[str]:
        rows = self._conn.execute(
            "SELECT signature FROM playlist_signatures WHERE playlist_id = ? ORDER BY id",
            (str(playlist_id),),
        ).fetchall()
        return [row[0] for row in rows]

    def replace_items(self, playlist_id: UUID, ordered_media_ids: list[UUID]) -> None:
        """Records the playlist's current order (spec #76). Needed to recognise an
        *edited* playlist in a later session (spec #12 similarity scoring) -- the
        playlist_items table existed but was never written, so any edit between sessions
        made the app treat a known playlist as brand new and "lose" its bookmarks."""
        occurrences: dict[UUID, int] = {}
        rows = []
        for ordinal, media_id in enumerate(ordered_media_ids):
            occurrence = occurrences.get(media_id, 0)
            occurrences[media_id] = occurrence + 1
            rows.append((str(uuid4()), str(playlist_id), str(media_id), ordinal, occurrence, str(media_id)))
        with self._conn:
            self._conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (str(playlist_id),))
            # Only media rows that exist (the FK would otherwise abort the whole batch).
            self._conn.executemany(
                "INSERT INTO playlist_items (id, playlist_id, media_id, ordinal, occurrence_index) "
                "SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM media WHERE id = ?)",
                rows,
            )

    def list_item_media_ids(self, playlist_id: UUID) -> list[UUID]:
        rows = self._conn.execute(
            "SELECT media_id FROM playlist_items WHERE playlist_id = ? ORDER BY ordinal",
            (str(playlist_id),),
        ).fetchall()
        return [UUID(row[0]) for row in rows]

    def touch(self, playlist_id: UUID) -> None:
        """Marks the playlist as seen now, so list_recent() (the similarity candidates)
        really is most-recently-used first."""
        self._conn.execute(
            "UPDATE playlists SET last_seen_at = ? WHERE id = ?", (_now(), str(playlist_id))
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
