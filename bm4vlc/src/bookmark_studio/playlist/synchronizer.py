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


@dataclass(frozen=True, slots=True)
class SyncResult:
    action: str
    playlist_id: UUID | None
    candidate_id: UUID | None = None
    candidate_score: float | None = None


class PlaylistSynchronizer:
    """Owns the "is this the same playlist, just edited?" decision from spec #13.

    While a playlist is actively tracked, a snapshot that's still similar enough to the
    last one is treated as an in-place mutation (item added/removed/reordered) and never
    re-triggers full recognition -- otherwise a single playlist edit could bounce the
    active bookmark context to an unrelated project mid-session (spec #13's stated risk).
    A snapshot too different from the tracked one falls through to full recognition
    (spec #180), which may match a different known playlist, ask the user, or create a
    new ad-hoc context (spec #14).
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
            score = similarity_score(self._active_items, ordered_media_ids)
            if score >= SIMILARITY_ASK_USER_THRESHOLD - FLOAT_EPSILON:
                self._repository.add_signature(
                    self._active_playlist_id, strict_signature(ordered_media_ids)
                )
                self._active_items = ordered_media_ids
                return SyncResult(SyncAction.MUTATED, self._active_playlist_id)

        result: RecognitionResult = self._recognition.recognize(
            source_uri=source_uri, ordered_media_ids=ordered_media_ids
        )

        if result.action == RecognitionAction.MATCHED:
            self._active_playlist_id = result.playlist_id
            self._active_items = ordered_media_ids
            return SyncResult(SyncAction.MATCHED, result.playlist_id)

        if result.action == RecognitionAction.ASK_USER:
            return SyncResult(SyncAction.ASK_USER, None, result.candidate_id, result.candidate_score)

        # NEW_CONTEXT (spec #14): create an ad-hoc playlist automatically.
        playlist = Playlist(
            id=uuid4(),
            name=f"Unsaved VLC Playlist {datetime.now(timezone.utc):%d %B %Y %H:%M}",
            source_uri=source_uri,
            is_ad_hoc=True,
        )
        self._repository.insert(playlist)
        self._repository.add_signature(playlist.id, strict_signature(ordered_media_ids))
        self._active_playlist_id = playlist.id
        self._active_items = ordered_media_ids
        return SyncResult(SyncAction.CREATED_AD_HOC, playlist.id)

    def accept_ask_user_match(self, playlist_id: UUID, ordered_media_ids: list[UUID]) -> None:
        """Caller's answer when a prior on_snapshot() returned ASK_USER and the user confirmed."""
        self._repository.add_signature(playlist_id, strict_signature(ordered_media_ids))
        self._active_playlist_id = playlist_id
        self._active_items = ordered_media_ids
