from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Playlist:
    """A recognized or ad-hoc VLC playlist context (spec #9-#14, #73)."""

    id: UUID
    name: str
    source_uri: str | None
    is_ad_hoc: bool


@dataclass(frozen=True, slots=True)
class PlaylistItem:
    """One occurrence of a media item within a playlist, order preserved (spec #76)."""

    id: UUID
    playlist_id: UUID
    media_id: UUID
    ordinal: int
    occurrence_index: int = 0
