from __future__ import annotations

import pytest

from bookmark_studio.ui.transport import format_timecode, parse_timecode


@pytest.mark.parametrize(
    "text,expected_us",
    [
        ("17", 17_000_000),
        ("17.450", 17_450_000),
        ("1:17", 77_000_000),
        ("1:17.450", 77_450_000),
        ("00:01:17.450", 77_450_000),
        ("0", 0),
    ],
)
def test_parse_timecode(text: str, expected_us: int) -> None:
    assert parse_timecode(text) == expected_us


def test_parse_timecode_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_timecode("not a timecode")


def test_format_timecode_matches_hh_mm_ss_mmm() -> None:
    assert format_timecode(77_450_000) == "00:01:17.450"
    assert format_timecode(0) == "00:00:00.000"
    assert format_timecode(3_661_000_000) == "01:01:01.000"


def test_format_and_parse_roundtrip() -> None:
    for time_us in (0, 1_000, 500_000, 77_450_000, 3_723_999_000):
        assert parse_timecode(format_timecode(time_us)) == time_us
