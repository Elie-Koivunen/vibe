"""Multi-resolution peak pyramid + best-level selection for the current zoom (spec #61)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from bookmark_studio.waveform.peaks import compute_peaks

BASE_BLOCK_SIZE = 64
LEVEL_FACTOR = 4
MAX_OVERVIEW_POINTS = 4000


@dataclass(frozen=True, slots=True)
class PyramidLevel:
    block_size: int
    peaks: np.ndarray

    def us_per_peak(self, sample_rate: int) -> float:
        return self.block_size / sample_rate * 1_000_000

    def slice(self, start_us: int, end_us: int, sample_rate: int) -> np.ndarray:
        us_per_peak = self.us_per_peak(sample_rate)
        start_index = max(0, int(start_us / us_per_peak))
        end_index = min(self.peaks.shape[0], int(end_us / us_per_peak) + 1)
        if end_index <= start_index:
            return self.peaks[0:0]
        return self.peaks[start_index:end_index]


@dataclass(frozen=True, slots=True)
class WaveformPyramid:
    levels: tuple[PyramidLevel, ...]
    sample_rate: int

    def best_level(self, visible_duration_us: int, pixel_width: int) -> PyramidLevel:
        """Selects the coarsest level giving ~1-2 peak columns per screen pixel (spec #112)."""
        if pixel_width <= 0 or visible_duration_us <= 0:
            return self.levels[0]
        target_us_per_pixel = visible_duration_us / pixel_width
        fine_enough = [
            level for level in self.levels if level.us_per_peak(self.sample_rate) <= target_us_per_pixel
        ]
        if fine_enough:
            return max(fine_enough, key=lambda level: level.block_size)
        return self.levels[0]


def build_pyramid(
    samples: np.ndarray,
    sample_rate: int,
    *,
    base_block_size: int = BASE_BLOCK_SIZE,
    factor: int = LEVEL_FACTOR,
    max_overview_points: int = MAX_OVERVIEW_POINTS,
) -> WaveformPyramid:
    """Builds levels of doubling-ish coarseness (spec #61) until the overview is compact."""
    levels: list[PyramidLevel] = []
    block_size = base_block_size
    while True:
        peaks = compute_peaks(samples, block_size)
        levels.append(PyramidLevel(block_size=block_size, peaks=peaks))
        if peaks.shape[0] <= max_overview_points or peaks.shape[0] <= 1:
            break
        block_size *= factor
    return WaveformPyramid(levels=tuple(levels), sample_rate=sample_rate)
