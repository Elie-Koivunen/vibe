from __future__ import annotations

import re

import pytest

from bookmark_studio.domain.bookmark import InvalidBookmarkRange, default_bookmark_name, validate_bookmark_range
from bookmark_studio.domain.enums import BookmarkType


def test_default_bookmark_name_matches_bookmark_date_random_format() -> None:
    """Direct user request: default name is "bookmark-<date>-<6 alphanumeric random
    unique string>", not a flat "New bookmark" identical across every bookmark."""
    name = default_bookmark_name()
    assert re.fullmatch(r"bookmark-\d{8}-[a-z0-9]{6}", name)


def test_default_bookmark_name_is_unique_across_calls() -> None:
    names = {default_bookmark_name() for _ in range(50)}
    assert len(names) == 50


def test_point_bookmark_allows_no_end() -> None:
    validate_bookmark_range(
        bookmark_type=BookmarkType.POINT,
        start_us=1_000,
        end_us=None,
        loop_enabled=False,
        repeat_count=None,
        loop_gap_ms=0,
    )


def test_point_bookmark_rejects_end() -> None:
    with pytest.raises(InvalidBookmarkRange):
        validate_bookmark_range(
            bookmark_type=BookmarkType.POINT,
            start_us=1_000,
            end_us=2_000,
            loop_enabled=False,
            repeat_count=None,
            loop_gap_ms=0,
        )


def test_segment_requires_end_greater_than_start() -> None:
    with pytest.raises(InvalidBookmarkRange):
        validate_bookmark_range(
            bookmark_type=BookmarkType.SEGMENT,
            start_us=2_000,
            end_us=1_000,
            loop_enabled=False,
            repeat_count=None,
            loop_gap_ms=0,
        )


def test_loop_requires_segment() -> None:
    with pytest.raises(InvalidBookmarkRange):
        validate_bookmark_range(
            bookmark_type=BookmarkType.POINT,
            start_us=1_000,
            end_us=None,
            loop_enabled=True,
            repeat_count=None,
            loop_gap_ms=0,
        )


def test_repeat_count_zero_is_invalid() -> None:
    with pytest.raises(InvalidBookmarkRange):
        validate_bookmark_range(
            bookmark_type=BookmarkType.SEGMENT,
            start_us=0,
            end_us=1_000,
            loop_enabled=True,
            repeat_count=0,
            loop_gap_ms=0,
        )


def test_negative_gap_is_invalid() -> None:
    with pytest.raises(InvalidBookmarkRange):
        validate_bookmark_range(
            bookmark_type=BookmarkType.SEGMENT,
            start_us=0,
            end_us=1_000,
            loop_enabled=False,
            repeat_count=None,
            loop_gap_ms=-1,
        )
