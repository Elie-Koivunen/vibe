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
    # 0-256 (VLC's built-in scale, 256 = 100%) -- needed so a fade (spec: "add options
    # to fade in and fade out when playing back") can duck down to 0 and back up to
    # whatever the user actually had it set to, not some arbitrary guessed level.
    volume: int = 256


@dataclass(frozen=True, slots=True)
class VlcPlaylistItem:
    vlc_id: int
    uri: str
    name: str
    duration_s: float | None
