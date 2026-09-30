from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Selection:
    """A temporary, non-persistent waveform range (spec #36) — not yet a Bookmark."""

    start_us: int
    end_us: int

    def __post_init__(self) -> None:
        if self.start_us < 0:
            raise ValueError("start_us must be >= 0")
        if self.end_us <= self.start_us:
            raise ValueError("end_us must be greater than start_us")

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us
