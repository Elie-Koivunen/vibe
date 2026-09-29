"""PlaylistContext: turns the player's playlist into known media and a bookmark
playlist (spec #10-#14, #179-#180).

Each changed snapshot is resolved item by item (MediaResolver), then matched to a stored
playlist (PlaylistSynchronizer): the same playlist, an edited one (matched automatically
when >=95% similar, or after asking the user when 75-95%), or a new one. Identical
snapshots cost nothing.
"""
from __future__ import annotations

from typing import Callable
from uuid import UUID

from bookmark_studio.domain.media import Media
from bookmark_studio.logging.setup import get_logger
from bookmark_studio.media.resolver import MediaResolver
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.playback.status import VlcPlaylistItem
from bookmark_studio.playlist.recognition import PlaylistRecognitionService
from bookmark_studio.playlist.synchronizer import PlaylistSynchronizer, SyncAction

AskPlaylistMatch = Callable[[str, float], bool]


class PlaylistContext:
    def __init__(self, media_repository: MediaRepository, playlist_repository: PlaylistRepository,
                 ask_playlist_match: AskPlaylistMatch) -> None:
        self._log = get_logger("PLAYLIST")
        self._playlists = playlist_repository
        self.resolver = MediaResolver(media_repository)
        recognition = PlaylistRecognitionService(playlist_repository, playlist_repository.list_item_media_ids)
        self.synchronizer = PlaylistSynchronizer(playlist_repository, recognition)
        self._ask = ask_playlist_match
        self.items: list[VlcPlaylistItem] = []  # latest snapshot, as the player reported it
        self.resolved: list[tuple[VlcPlaylistItem, Media]] = []  # items with a known media
        self.source_uri: str | None = None  # the .m3u this session was started from
        self.song_names: dict[UUID, str] = {}
        self.asking = False  # a "same playlist?" question is open
        self._last_key: tuple[object, ...] | None = None
        self._declined: set[tuple[UUID, tuple[UUID, ...]]] = set()

    @property
    def active_playlist_id(self) -> UUID | None:
        return self.synchronizer.active_playlist_id

    def reset(self, *, source_uri: str | None = None) -> None:
        """A different player/playlist: forget everything about the previous one."""
        self.synchronizer.reset()
        self.items = []
        self.resolved = []
        self.song_names = {}
        self.source_uri = source_uri
        self._last_key = None

    def force_refresh(self) -> None:
        """The next snapshot is processed even if unchanged."""
        self._last_key = None

    def apply_snapshot(self, items: list[VlcPlaylistItem]) -> bool:
        """Processes one playlist poll. True if anything changed (callers then refresh
        their views); False for an identical snapshot or while a question is open."""
        if self.asking:
            return False
        items = list(items)
        self.items = items
        key = tuple((i.vlc_id, i.uri, i.name, i.duration_s) for i in items)
        if key == self._last_key:
            return False
        resolved: list[tuple[VlcPlaylistItem, Media]] = []
        for item in items:
            if not item.uri:
                continue
            try:
                resolved.append((item, self.resolver.resolve(item.uri)))
            except Exception:  # noqa: BLE001 - one bad item must not hide the others
                self._log.exception("failed to resolve playlist item %r", item.uri)
        self.resolved = resolved
        if resolved:
            ordered = [media.id for _item, media in resolved]
            result = self.synchronizer.on_snapshot(source_uri=self.source_uri, ordered_media_ids=ordered)
            self._log.debug("playlist sync: %s -> %s", result.action, result.playlist_id)
            if result.action == SyncAction.ASK_USER:
                self._resolve_ask_user(result.candidate_id, result.candidate_score, ordered)
            for item, media in resolved:
                self.song_names[media.id] = media.title or media.filename or item.uri
        self._last_key = key
        return True

    def _resolve_ask_user(self, candidate_id: UUID | None, score: float | None, ordered: list[UUID]) -> None:
        key = (candidate_id, tuple(ordered)) if candidate_id is not None else None
        accept = False
        if candidate_id is not None and key not in self._declined:
            record = self._playlists.get(candidate_id)
            name = record.playlist.name if record is not None else "a previous playlist"
            self.asking = True
            try:
                accept = bool(self._ask(name, float(score or 0.0)))
            finally:
                self.asking = False
        if accept and candidate_id is not None:
            self.synchronizer.accept_ask_user_match(candidate_id, ordered)
        else:
            if key is not None:
                self._declined.add(key)
            self.synchronizer.create_ad_hoc(source_uri=self.source_uri, ordered_media_ids=ordered)

    # -- lookups --

    def item(self, vlc_id: int | None) -> VlcPlaylistItem | None:
        if vlc_id is None:
            return None
        return next((i for i in self.items if i.vlc_id == vlc_id), None)

    def media_for_item(self, vlc_id: int | None) -> Media | None:
        return next((m for i, m in self.resolved if i.vlc_id == vlc_id), None)

    def item_for_media(self, media_id: UUID, *, prefer: tuple[int | None, ...] = ()) -> VlcPlaylistItem | None:
        """The playlist item that plays `media_id`; when the same media appears more than
        once, one of the `prefer`red item ids (e.g. playing, displayed) wins."""
        matches = [item for item, media in self.resolved if media.id == media_id]
        if not matches:
            return None
        for preferred in prefer:
            for item in matches:
                if item.vlc_id == preferred:
                    return item
        return matches[0]
