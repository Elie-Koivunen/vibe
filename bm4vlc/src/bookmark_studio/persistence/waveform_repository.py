"""WaveformCacheRepository: waveform_cache table metadata (spec #62, #79)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID


@dataclass(frozen=True, slots=True)
class WaveformCacheEntry:
    cache_key: str
    media_id: UUID
    algorithm_version: int
    sample_rate: int
    channel_mode: str
    file_path: str


class WaveformCacheRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def lookup(self, cache_key: str) -> WaveformCacheEntry | None:
        row = self._conn.execute(
            "SELECT cache_key, media_id, algorithm_version, sample_rate, channel_mode, "
            "file_path FROM waveform_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        key, media_id, algo_version, sample_rate, channel_mode, file_path = row
        return WaveformCacheEntry(
            cache_key=key,
            media_id=UUID(media_id),
            algorithm_version=algo_version,
            sample_rate=sample_rate,
            channel_mode=channel_mode,
            file_path=file_path,
        )

    def put(self, entry: WaveformCacheEntry) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO waveform_cache "
            "(cache_key, media_id, algorithm_version, sample_rate, channel_mode, "
            "file_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.cache_key,
                str(entry.media_id),
                entry.algorithm_version,
                entry.sample_rate,
                entry.channel_mode,
                entry.file_path,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def invalidate(self, cache_key: str) -> None:
        self._conn.execute("DELETE FROM waveform_cache WHERE cache_key = ?", (cache_key,))
        self._conn.commit()

    def invalidate_for_media(self, media_id: UUID) -> None:
        self._conn.execute(
            "DELETE FROM waveform_cache WHERE media_id = ?", (str(media_id),)
        )
        self._conn.commit()
