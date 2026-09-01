"""MediaRepository: media + media_uri_aliases tables (spec #75, #79)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import UUID

from bookmark_studio.domain.media import Media


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MediaRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, media_id: UUID) -> Media | None:
        row = self._conn.execute(
            "SELECT id, canonical_uri, filename, title, artist, album, duration_us, "
            "file_size, mtime_ns, fast_fingerprint, full_sha256 FROM media WHERE id = ?",
            (str(media_id),),
        ).fetchone()
        return self._row_to_media(row) if row else None

    def resolve_by_uri(self, uri: str) -> Media | None:
        """Matches canonical_uri first, then known aliases (spec #69 steps 1-2)."""
        row = self._conn.execute(
            "SELECT id, canonical_uri, filename, title, artist, album, duration_us, "
            "file_size, mtime_ns, fast_fingerprint, full_sha256 FROM media "
            "WHERE canonical_uri = ?",
            (uri,),
        ).fetchone()
        if row:
            return self._row_to_media(row)
        row = self._conn.execute(
            "SELECT m.id, m.canonical_uri, m.filename, m.title, m.artist, m.album, "
            "m.duration_us, m.file_size, m.mtime_ns, m.fast_fingerprint, m.full_sha256 "
            "FROM media m JOIN media_uri_aliases a ON a.media_id = m.id WHERE a.uri = ?",
            (uri,),
        ).fetchone()
        return self._row_to_media(row) if row else None

    def resolve_by_fingerprint(self, fast_fingerprint: str) -> Media | None:
        row = self._conn.execute(
            "SELECT id, canonical_uri, filename, title, artist, album, duration_us, "
            "file_size, mtime_ns, fast_fingerprint, full_sha256 FROM media "
            "WHERE fast_fingerprint = ?",
            (fast_fingerprint,),
        ).fetchone()
        return self._row_to_media(row) if row else None

    def insert(self, media: Media) -> Media:
        now = _now()
        self._conn.execute(
            "INSERT INTO media (id, canonical_uri, filename, title, artist, album, "
            "duration_us, file_size, mtime_ns, fast_fingerprint, full_sha256, "
            "created_at, updated_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(media.id),
                media.canonical_uri,
                media.filename,
                media.title,
                media.artist,
                media.album,
                media.duration_us,
                media.file_size,
                media.mtime_ns,
                media.fast_fingerprint,
                media.full_sha256,
                now,
                now,
                now,
            ),
        )
        self._conn.commit()
        return media

    def update(self, media: Media) -> None:
        now = _now()
        self._conn.execute(
            "UPDATE media SET canonical_uri = ?, filename = ?, title = ?, artist = ?, "
            "album = ?, duration_us = ?, file_size = ?, mtime_ns = ?, fast_fingerprint = ?, "
            "full_sha256 = ?, updated_at = ?, last_seen_at = ? WHERE id = ?",
            (
                media.canonical_uri,
                media.filename,
                media.title,
                media.artist,
                media.album,
                media.duration_us,
                media.file_size,
                media.mtime_ns,
                media.fast_fingerprint,
                media.full_sha256,
                now,
                now,
                str(media.id),
            ),
        )
        self._conn.commit()

    def add_uri_alias(self, media_id: UUID, uri: str) -> None:
        """Records an old URI so a moved/renamed file can still resolve (spec #103)."""
        self._conn.execute(
            "INSERT INTO media_uri_aliases (media_id, uri, created_at) VALUES (?, ?, ?)",
            (str(media_id), uri, _now()),
        )
        self._conn.commit()

    def relocate(self, media_id: UUID, new_canonical_uri: str) -> None:
        """Moves the old canonical_uri to aliases and installs the new one (spec #103)."""
        media = self.get(media_id)
        if media is None:
            raise ValueError(f"unknown media_id {media_id}")
        if media.canonical_uri and media.canonical_uri != new_canonical_uri:
            self.add_uri_alias(media_id, media.canonical_uri)
        self.update(
            Media(
                id=media.id,
                canonical_uri=new_canonical_uri,
                filename=media.filename,
                title=media.title,
                artist=media.artist,
                album=media.album,
                duration_us=media.duration_us,
                file_size=media.file_size,
                mtime_ns=media.mtime_ns,
                fast_fingerprint=media.fast_fingerprint,
                full_sha256=media.full_sha256,
            )
        )

    @staticmethod
    def _row_to_media(row: sqlite3.Row | tuple) -> Media:
        (
            media_id,
            canonical_uri,
            filename,
            title,
            artist,
            album,
            duration_us,
            file_size,
            mtime_ns,
            fast_fingerprint,
            full_sha256,
        ) = row
        return Media(
            id=UUID(media_id),
            canonical_uri=canonical_uri,
            filename=filename,
            title=title,
            artist=artist,
            album=album,
            duration_us=duration_us,
            file_size=file_size,
            mtime_ns=mtime_ns,
            fast_fingerprint=fast_fingerprint,
            full_sha256=full_sha256,
        )
