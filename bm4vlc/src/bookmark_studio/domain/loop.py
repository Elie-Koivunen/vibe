from __future__ import annotations

from dataclasses import dataclass

from bookmark_studio.domain.enums import CompletionAction


@dataclass(frozen=True, slots=True)
class LoopSpec:
    """Configuration for a single loop run (spec #33). repeat_count=None means forever."""

    start_us: int
    end_us: int
    repeat_count: int | None
    gap_ms: int
    completion_action: CompletionAction

    def __post_init__(self) -> None:
        if self.start_us >= self.end_us:
            raise ValueError("start_us must be less than end_us")
        if self.repeat_count is not None and self.repeat_count < 1:
            raise ValueError("repeat_count must be None (forever) or >= 1")
        if self.gap_ms < 0:
            raise ValueError("gap_ms must be >= 0")
