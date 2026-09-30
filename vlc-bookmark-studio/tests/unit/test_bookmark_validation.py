from __future__ import annotations

import re

import pytest

from bookmark_studio.domain.bookmark import InvalidBookmarkRange, default_bookmark_name, validate_bookmark_range
from bookmark_studio.domain.enums import BookmarkType


def test_default_bookmark_name_is_date_random_start_end() -> None:
    """<date>-<6 random characters>-<start>-<end>; a point bookmark has only a start."""
    segment = default_bookmark_name(83_456_000, 105_000_000)
    assert re.fullmatch(r"\d{8}-[a-z0-9]{6}-00:01:23\.456-00:01:45\.000", segment)
    point = default_bookmark_name(83_456_000)
    assert re.fullmatch(r"\d{8}-[a-z0-9]{6}-00:01:23\.456", point)


def test_default_bookmark_name_is_unique_across_calls() -> None:
    names = {default_bookmark_name(1_000_000, 2_000_000) for _ in range(50)}
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
