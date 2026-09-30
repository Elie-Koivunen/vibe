"""Tracks playlist mutation vs. new-context detection during a live session (spec #13, #180)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from bookmark_studio.domain.playlist import Playlist
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.playlist.recognition import PlaylistRecognitionService, RecognitionAction, RecognitionResult
from bookmark_studio.playlist.signatures import strict_signature
from bookmark_studio.playlist.similarity import (
    FLOAT_EPSILON,
    SIMILARITY_ASK_USER_THRESHOLD,
    similarity_score,
)


class SyncAction:
    MUTATED = "mutated"
    MATCHED = "matched"
    ASK_USER = "ask_user"
    CREATED_AD_HOC = "created_ad_hoc"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class SyncResult:
    action: str
    playlist_id: UUID | None
    candidate_id: UUID | None = None
    candidate_score: float | None = None


def _playlist_name_from_source(source_uri: str | None) -> str | None:
    if not source_uri:
        return None
    from bookmark_studio.platform_support import uri_to_local_path

    path = uri_to_local_path(source_uri)
    return path.stem if path is not None and path.stem else None


class PlaylistSynchronizer:
    """Owns the "is this the same playlist, just edited?" decision from spec #13.

    While a playlist is actively tracked, a snapshot that's still similar enough to the
    last one is treated as an in-place mutation (item added/removed/reordered) and never
    re-triggers full recognition -- otherwise a single playlist edit could bounce the
    active bookmark context to an unrelated project mid-session (spec #13's stated risk).
    A snapshot too different from the tracked one falls through to full recognition
    (spec #180), which may match a different known playlist, ask the user, or create a
    new ad-hoc context (spec #14).

    Every matched/mutated/created snapshot also records the playlist's item order
    (PlaylistRepository.replace_items), which is what lets recognition find an edited
    playlist again in a later session.
    """

    def __init__(self, repository: PlaylistRepository, recognition: PlaylistRecognitionService) -> None:
        self._repository = repository
        self._recognition = recognition
        self._active_playlist_id: UUID | None = None
        self._active_items: list[UUID] = []

    @property
    def active_playlist_id(self) -> UUID | None:
        return self._active_playlist_id

    def reset(self) -> None:
        """Forces full recognition on the next snapshot (e.g. VLC restarted, spec #105)."""
        self._active_playlist_id = None
        self._active_items = []

    def on_snapshot(self, *, source_uri: str | None, ordered_media_ids: list[UUID]) -> SyncResult:
        if self._active_playlist_id is not None:
            if ordered_media_ids == self._active_items:
                return SyncResult(SyncAction.UNCHANGED, self._active_playlist_id)
            score = similarity_score(self._active_items, ordered_media_ids)
            if score >= SIMILARITY_ASK_USER_THRESHOLD - FLOAT_EPSILON:
                self._track(self._active_playlist_id, ordered_media_ids)
                return SyncResult(SyncAction.MUTATED, self._active_playlist_id)

        result: RecognitionResult = self._recognition.recognize(
            source_uri=source_uri, ordered_media_ids=ordered_media_ids
        )

        if result.action == RecognitionAction.MATCHED:
            assert result.playlist_id is not None
            self._track(result.playlist_id, ordered_media_ids)
            self._repository.touch(result.playlist_id)
            return SyncResult(SyncAction.MATCHED, result.playlist_id)

        if result.action == RecognitionAction.ASK_USER:
            return SyncResult(SyncAction.ASK_USER, None, result.candidate_id, result.candidate_score)

        # NEW_CONTEXT (spec #14): create an ad-hoc playlist automatically.
        playlist_id = self.create_ad_hoc(source_uri=source_uri, ordered_media_ids=ordered_media_ids)
        return SyncResult(SyncAction.CREATED_AD_HOC, playlist_id)

    def create_ad_hoc(self, *, source_uri: str | None, ordered_media_ids: list[UUID]) -> UUID:
        """A new playlist context -- named after the .m3u it was launched from, if any."""
        name = _playlist_name_from_source(source_uri) or (
            f"Unsaved VLC Playlist {datetime.now(timezone.utc):%d %B %Y %H:%M}"
        )
        playlist = Playlist(id=uuid4(), name=name, source_uri=source_uri, is_ad_hoc=source_uri is None)
        self._repository.insert(playlist)
        self._track(playlist.id, ordered_media_ids)
        return playlist.id

    def accept_ask_user_match(self, playlist_id: UUID, ordered_media_ids: list[UUID]) -> None:
        """Caller's answer when a prior on_snapshot() returned ASK_USER and the user confirmed."""
        self._track(playlist_id, ordered_media_ids)
        self._repository.touch(playlist_id)

    def _track(self, playlist_id: UUID, ordered_media_ids: list[UUID]) -> None:
        self._repository.add_signature(playlist_id, strict_signature(ordered_media_ids))
        self._repository.replace_items(playlist_id, ordered_media_ids)
        self._active_playlist_id = playlist_id
        self._active_items = list(ordered_media_ids)
