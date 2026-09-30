"""Domain events fired across the app (spec #115)."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.playlist import Playlist
from bookmark_studio.domain.selection import Selection
from bookmark_studio.playback.status import PlaybackStatus


@dataclass(frozen=True, slots=True)
class VlcConnected:
    pass


@dataclass(frozen=True, slots=True)
class VlcDisconnected:
    reason: str


@dataclass(frozen=True, slots=True)
class PlaybackStateChanged:
    status: PlaybackStatus


@dataclass(frozen=True, slots=True)
class CurrentVlcItemChanged:
    vlc_id: int
    uri: str


@dataclass(frozen=True, slots=True)
class PlaylistContextChanged:
    playlist: Playlist


@dataclass(frozen=True, slots=True)
class SelectionChanged:
    selection: Selection | None


@dataclass(frozen=True, slots=True)
class BookmarkCreated:
    bookmark: Bookmark


@dataclass(frozen=True, slots=True)
class BookmarkUpdated:
    bookmark: Bookmark


@dataclass(frozen=True, slots=True)
class BookmarkDeleted:
    bookmark_id: UUID
