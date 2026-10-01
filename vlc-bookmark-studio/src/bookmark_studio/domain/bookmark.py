from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.domain.timecode import format_timecode


class InvalidBookmarkRange(ValueError):
    """Raised when a bookmark's start/end times violate domain rules (spec #78)."""


MIN_SEGMENT_DURATION_US = 50_000

_NAME_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


_TIMECODE = r"\d{2,}:\d{2}:\d{2}\.\d{3}"
_AUTO_NAME_RE = re.compile(rf"^(\d{{8}})-([a-z0-9]{{6}})-({_TIMECODE})(?:-({_TIMECODE}))?$")


def default_bookmark_name(*, suffix: str | None = None) -> str:
    """A new bookmark's name: 6 random characters, e.g. ``k3x9qa`` (0.7.0). The times
    are in the list's Start/End columns; the random string tells bookmarks apart.
    secrets (not random) only because it needs no seeding."""
    return suffix or "".join(secrets.choice(_NAME_SUFFIX_ALPHABET) for _ in range(6))


def dated_bookmark_name(
    start_us: int, end_us: int | None = None, *, created: datetime | None = None, suffix: str | None = None,
) -> str:
    """The automatic name of 0.4.0-0.6.0: "<date>-<6 random characters>-<start>[-<end>]",
    e.g. ``20260930-k3x9qa-00:01:23.456-00:01:45.000`` (a point bookmark has only a
    start). No longer given to new bookmarks; existing ones keep it and it still follows
    their moves (with_range)."""
    date_part = (created or datetime.now()).strftime("%Y%m%d")
    suffix = suffix or default_bookmark_name()
    name = f"{date_part}-{suffix}-{format_timecode(start_us)}"
    return name if end_us is None else f"{name}-{format_timecode(end_us)}"


def is_automatic_name(name: str, start_us: int, end_us: int | None) -> bool:
    """True if `name` is exactly the dated automatic name for this range (the user never
    renamed the bookmark). A plain random name has no times in it to keep up to date."""
    match = _AUTO_NAME_RE.match(name)
    if match is None:
        return False
    _date, _suffix, start_text, end_text = match.groups()
    expected_end = format_timecode(end_us) if end_us is not None else None
    return start_text == format_timecode(start_us) and end_text == expected_end


def with_range(bookmark: Bookmark, start_us: int, end_us: int | None) -> Bookmark:
    """`bookmark` moved/resized to [start_us, end_us]. A dated automatic name follows the
    new times (keeping its date and random part); any other name stays as it is."""
    name = bookmark.name
    if is_automatic_name(name, bookmark.start_us, bookmark.end_us):
        match = _AUTO_NAME_RE.match(name)
        assert match is not None
        date_part, suffix = match.group(1), match.group(2)
        name = dated_bookmark_name(
            start_us, end_us, created=datetime.strptime(date_part, "%Y%m%d"), suffix=suffix
        )
    return replace(bookmark, start_us=start_us, end_us=end_us, name=name)


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
    # Fade in/out when played; 0 disables (default), like loop_gap_ms.
    fade_in_ms: int = 0
    fade_out_ms: int = 0

    def __post_init__(self) -> None:
        # Enum fields also accept their string values (a Qt combo box hands those back),
        # so a Bookmark never carries a plain str where persistence needs `.value`.
        for name, enum_type in (
            ("scope", BookmarkScope), ("bookmark_type", BookmarkType), ("completion_action", CompletionAction),
        ):
            value = getattr(self, name)
            if not isinstance(value, enum_type):
                object.__setattr__(self, name, enum_type(value))
        validate_bookmark_range(
            bookmark_type=self.bookmark_type,
            start_us=self.start_us,
            end_us=self.end_us,
            loop_enabled=self.loop_enabled,
            repeat_count=self.repeat_count,
            loop_gap_ms=self.loop_gap_ms,
        )
        if self.fade_in_ms < 0 or self.fade_out_ms < 0:
            raise InvalidBookmarkRange("fade_in_ms and fade_out_ms must be >= 0")


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
