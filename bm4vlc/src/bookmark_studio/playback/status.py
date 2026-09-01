"""PlaybackStatus / VlcPlaylistItem data objects returned by a PlaybackAdapter (spec #24-#25)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlaybackStatus:
    state: str
    time_us: int
    position: float
    rate: float
    current_playlist_item_id: int | None
    duration_us: int | None
    media_uri: str | None


@dataclass(frozen=True, slots=True)
class VlcPlaylistItem:
    vlc_id: int
    uri: str
    name: str
    duration_s: float | None
