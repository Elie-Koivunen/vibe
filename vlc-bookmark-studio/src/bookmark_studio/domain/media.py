from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Media:
    """Identity record for a media file, resolvable across renames/moves (spec #67-#70)."""

    id: UUID
    canonical_uri: str | None
    filename: str | None
    title: str | None
    artist: str | None
    album: str | None
    duration_us: int | None
    file_size: int | None
    mtime_ns: int | None
    fast_fingerprint: str | None
    full_sha256: str | None = None
