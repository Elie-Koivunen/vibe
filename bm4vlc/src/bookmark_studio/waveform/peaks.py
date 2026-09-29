"""NumPy min/max peak-block reduction from decoded PCM (spec #60)."""
from __future__ import annotations

import numpy as np


def decode_pcm_f32le(raw: bytes) -> np.ndarray:
    """Interprets raw little-endian float32 PCM bytes (as emitted by ffmpeg -f f32le)."""
    return np.frombuffer(raw, dtype="<f4")


def compute_peaks(samples: np.ndarray, block_size: int) -> np.ndarray:
    """Reduces `samples` to per-block [min, max] pairs (spec #60). Shape: (n_blocks, 2)."""
    if block_size < 1:
        raise ValueError("block_size must be >= 1")
    if samples.size == 0:
        return np.zeros((0, 2), dtype="<f4")

    n_blocks = -(-samples.size // block_size)  # ceil division
    padded_size = n_blocks * block_size
    if padded_size != samples.size:
        # Pad the final partial block with its own last value so it doesn't get zeroed out
        # (zero-padding would draw a false silence dip at the very end of the waveform).
        pad_value = samples[-1] if samples.size else 0.0
        samples = np.pad(samples, (0, padded_size - samples.size), constant_values=pad_value)

    blocks = samples.reshape(n_blocks, block_size)
    minimum = blocks.min(axis=1)
    maximum = blocks.max(axis=1)
    return np.stack([minimum, maximum], axis=1).astype("<f4")


def reduce_peaks(peaks: np.ndarray, factor: int) -> np.ndarray:
    """Merges every `factor` consecutive [min, max] rows into one -- identical to
    computing peaks with a `factor` times larger block from the samples themselves (a
    block's min/max is the min/max of its sub-blocks'), without needing the samples."""
    if factor < 1:
        raise ValueError("factor must be >= 1")
    if peaks.shape[0] == 0:
        return np.zeros((0, 2), dtype="<f4")
    n = -(-peaks.shape[0] // factor)
    padded = n * factor
    if padded != peaks.shape[0]:
        peaks = np.concatenate([peaks, np.repeat(peaks[-1:], padded - peaks.shape[0], axis=0)])
    groups = peaks.reshape(n, factor, 2)
    return np.stack([groups[:, :, 0].min(axis=1), groups[:, :, 1].max(axis=1)], axis=1).astype("<f4")


class PeakAccumulator:
    """Reduces a stream of samples to per-block [min, max] peaks as it arrives, keeping
    only the peaks (8 bytes per block) -- never the samples. Decoding a whole file into
    memory first cost ~115 MB per hour of audio at the 8 kHz analysis rate, twice over."""

    def __init__(self, block_size: int) -> None:
        if block_size < 1:
            raise ValueError("block_size must be >= 1")
        self._block = block_size
        self._carry = np.zeros(0, dtype="<f4")
        self._chunks: list[np.ndarray] = []
        self.samples_seen = 0

    def feed(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        self.samples_seen += int(samples.size)
        data = np.concatenate([self._carry, samples.astype("<f4", copy=False)]) if self._carry.size else samples
        whole = (data.size // self._block) * self._block
        if whole:
            blocks = data[:whole].reshape(-1, self._block)
            self._chunks.append(np.stack([blocks.min(axis=1), blocks.max(axis=1)], axis=1).astype("<f4"))
        self._carry = np.array(data[whole:], dtype="<f4")

    def peaks(self, *, final: bool = False) -> np.ndarray:
        """Peaks so far; with `final`, including the trailing partial block (same result
        as compute_peaks over all samples fed)."""
        if len(self._chunks) > 1:
            self._chunks = [np.concatenate(self._chunks)]
        whole = self._chunks[0] if self._chunks else np.zeros((0, 2), dtype="<f4")
        if final and self._carry.size:
            tail = np.array([[self._carry.min(), self._carry.max()]], dtype="<f4")
            return np.concatenate([whole, tail])
        return whole
