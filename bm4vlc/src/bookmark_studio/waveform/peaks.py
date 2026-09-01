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
