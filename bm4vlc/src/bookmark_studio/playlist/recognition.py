"""PlaylistRecognitionService: resolves a VLC snapshot to a known/ad-hoc Playlist (spec #10)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.playlist.signatures import strict_signature
from bookmark_studio.playlist.similarity import SimilarityDecision, decide, similarity_score


class RecognitionAction:
    MATCHED = "matched"
    ASK_USER = "ask_user"
    NEW_CONTEXT = "new_context"


@dataclass(frozen=True, slots=True)
class RecognitionResult:
    action: str
    playlist_id: UUID | None
    candidate_id: UUID | None = None
    candidate_score: float | None = None


class PlaylistRecognitionService:
    """Priority order from spec #10: source URI, strict signature, similarity, new context."""

    def __init__(
        self,
        repository: PlaylistRepository,
        list_ordered_media_ids: Callable[[UUID], list[UUID]],
    ) -> None:
        self._repository = repository
        self._list_ordered_media_ids = list_ordered_media_ids

    def recognize(
        self, *, source_uri: str | None, ordered_media_ids: list[UUID]
    ) -> RecognitionResult:
        signature = strict_signature(ordered_media_ids)

        if source_uri:
            record = self._repository.find_by_source_uri(source_uri)
            if record is not None:
                self._repository.add_signature(record.playlist.id, signature)
                return RecognitionResult(RecognitionAction.MATCHED, record.playlist.id, record.playlist.id, 1.0)

        record = self._repository.find_by_signature(signature)
        if record is not None:
            return RecognitionResult(RecognitionAction.MATCHED, record.playlist.id, record.playlist.id, 1.0)

        scores: list[tuple[UUID, float]] = []
        for candidate in self._repository.list_recent():
            candidate_items = self._list_ordered_media_ids(candidate.playlist.id)
            scores.append((candidate.playlist.id, similarity_score(candidate_items, ordered_media_ids)))

        decision, candidate_id = decide(scores)
        candidate_score = dict(scores).get(candidate_id) if candidate_id else None

        if decision == SimilarityDecision.AUTO_MATCH:
            self._repository.add_signature(candidate_id, signature)
            return RecognitionResult(RecognitionAction.MATCHED, candidate_id, candidate_id, candidate_score)
        if decision == SimilarityDecision.ASK_USER:
            return RecognitionResult(RecognitionAction.ASK_USER, None, candidate_id, candidate_score)
        return RecognitionResult(RecognitionAction.NEW_CONTEXT, None, None, None)
