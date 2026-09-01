"""WaveformItem: single custom-painted item selecting the nearest pyramid level (spec #112)."""
from __future__ import annotations


def time_us_to_scene_x(time_us: int) -> float:
    """1 scene X unit = 1 millisecond (spec #39)."""
    return time_us / 1000.0


def scene_x_to_time_us(x: float) -> int:
    return max(0, round(x * 1000))
