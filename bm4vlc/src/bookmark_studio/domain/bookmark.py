from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction


class InvalidBookmarkRange(ValueError):
    """Raised when a bookmark's start/end times violate domain rules (spec #78)."""


MIN_SEGMENT_DURATION_US = 50_000

_NAME_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


def default_bookmark_name() -> str:
    """"bookmark-<date>-<6 alphanumeric random unique string>" -- direct user request
    to replace a flat, indistinguishable "New bookmark" default (every bookmark in a
    session ended up with the identical name until manually renamed). secrets.choice
    (not random) since this only needs to not collide within one person's bookmark
    list, not be cryptographically unguessable -- but the module's already the right
    tool and avoids seeding concerns.
    """
    date_part = datetime.now().strftime("%Y%m%d")
    suffix = "".join(secrets.choice(_NAME_SUFFIX_ALPHABET) for _ in range(6))
    return f"bookmark-{date_part}-{suffix}"


@dataclass(frozen=True, slots=True)
class Bookmark:
    """A point or segment marker scoped to a playlist+media pair (spec #77, #118)."""

    id: UUID
    playlist_id: UUID | None
    media_id: UUID
    scope: BookmarkScope
    lane_id: UUID | None
    bookmark_type: BookmarkType
    name: str
    start_us: int
    end_us: int | None
    loop_enabled: bool
    repeat_count: int | None
    loop_gap_ms: int
    completion_action: CompletionAction
    color_key: str | None = None
    notes: str | None = None
    tags: tuple[str, ...] = ()
    # Manual ordering in the bookmark list panel (spec: "row entries should also be
    # possible to manually reorder them moving up/down") -- independent of start_us,
    # which stays the sole ordering for the waveform's own markers. 0 for every
    # bookmark that's never been manually reordered, which combined with
    # BookmarkRepository.list_for_playlist()'s `ORDER BY sort_index, start_us` just
    # falls back to chronological order until the user actually reorders something.
    sort_index: int = 0

    def __post_init__(self) -> None:
        validate_bookmark_range(
            bookmark_type=self.bookmark_type,
            start_us=self.start_us,
            end_us=self.end_us,
            loop_enabled=self.loop_enabled,
            repeat_count=self.repeat_count,
            loop_gap_ms=self.loop_gap_ms,
        )


def validate_bookmark_range(
    *,
    bookmark_type: BookmarkType,
    start_us: int,
    end_us: int | None,
    loop_enabled: bool,
    repeat_count: int | None,
    loop_gap_ms: int,
    duration_us: int | None = None,
) -> None:
    """Domain-layer validation per spec #78 — must not rely solely on UI validation."""
    if start_us < 0:
        raise InvalidBookmarkRange("start_us must be >= 0")
    if duration_us is not None and start_us > duration_us:
        raise InvalidBookmarkRange("start_us exceeds media duration")

    if bookmark_type is BookmarkType.POINT:
        if end_us is not None:
            raise InvalidBookmarkRange("point bookmarks must not have end_us")
    else:
        if end_us is None:
            raise InvalidBookmarkRange("segment bookmarks require end_us")
        if end_us <= start_us:
            raise InvalidBookmarkRange("end_us must be greater than start_us")
        if duration_us is not None and end_us > duration_us:
            raise InvalidBookmarkRange("end_us exceeds media duration")

    if loop_enabled and end_us is None:
        raise InvalidBookmarkRange("loop requires end_us (a segment)")
    if repeat_count is not None and repeat_count < 1:
        raise InvalidBookmarkRange("repeat_count must be None (forever) or >= 1")
    if loop_gap_ms < 0:
        raise InvalidBookmarkRange("loop_gap_ms must be >= 0")
