"""Monotonic-clock interpolation between VLC status polls for smooth 60Hz playhead (spec #31)."""
from __future__ import annotations

import time
from dataclasses import dataclass

from bookmark_studio.playback.status import PlaybackStatus


@dataclass(frozen=True, slots=True)
class _Sample:
    time_us: int
    state: str
    rate: float
    sampled_at_ns: int


class PlaybackClock:
    """Estimates the current playback position between polls without spamming HTTP requests.

    `estimated_position_us` accepts an explicit `now_ns` so it's deterministically
    testable without real sleeps or timers.
    """

    def __init__(self) -> None:
        self._sample: _Sample | None = None

    def update(self, status: PlaybackStatus, *, now_ns: int | None = None) -> None:
        self._sample = _Sample(
            time_us=status.time_us,
            state=status.state,
            rate=status.rate,
            sampled_at_ns=now_ns if now_ns is not None else time.monotonic_ns(),
        )

    def estimated_position_us(self, *, now_ns: int | None = None) -> int:
        if self._sample is None:
            return 0
        if self._sample.state != "playing":
            return self._sample.time_us
        current_ns = now_ns if now_ns is not None else time.monotonic_ns()
        elapsed_us = (current_ns - self._sample.sampled_at_ns) / 1000.0
        return int(self._sample.time_us + elapsed_us * self._sample.rate)
