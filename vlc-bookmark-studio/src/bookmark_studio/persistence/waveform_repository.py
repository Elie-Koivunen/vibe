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
    duration_us: int | None = None  # exact decoded length (migration 004)


class WaveformCacheRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def lookup(self, cache_key: str) -> WaveformCacheEntry | None:
        row = self._conn.execute(
            "SELECT cache_key, media_id, algorithm_version, sample_rate, channel_mode, "
            "file_path, duration_us FROM waveform_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        key, media_id, algo_version, sample_rate, channel_mode, file_path, duration_us = row
        return WaveformCacheEntry(
            cache_key=key,
            media_id=UUID(media_id),
            algorithm_version=algo_version,
            sample_rate=sample_rate,
            channel_mode=channel_mode,
            file_path=file_path,
            duration_us=duration_us,
        )

    def put(self, entry: WaveformCacheEntry) -> None:
        self._conn.execute(
            "INSERT INTO waveform_cache "
            "(cache_key, media_id, algorithm_version, sample_rate, channel_mode, "
            "file_path, created_at, duration_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET media_id = excluded.media_id, "
            "algorithm_version = excluded.algorithm_version, sample_rate = excluded.sample_rate, "
            "channel_mode = excluded.channel_mode, file_path = excluded.file_path, "
            "duration_us = COALESCE(excluded.duration_us, waveform_cache.duration_us)",
            (
                entry.cache_key,
                str(entry.media_id),
                entry.algorithm_version,
                entry.sample_rate,
                entry.channel_mode,
                entry.file_path,
                datetime.now(timezone.utc).isoformat(),
                entry.duration_us,
            ),
        )
        self._conn.commit()

    def set_duration(self, cache_key: str, duration_us: int) -> None:
        self._conn.execute(
            "UPDATE waveform_cache SET duration_us = ? WHERE cache_key = ?", (duration_us, cache_key)
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
