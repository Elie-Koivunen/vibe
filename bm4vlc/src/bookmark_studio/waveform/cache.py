"""On-disk .npz waveform cache keyed by fingerprint+algorithm version (spec #62-#63)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from bookmark_studio.waveform.pyramid import PyramidLevel, WaveformPyramid

ALGORITHM_VERSION = 1


def cache_key(
    fast_fingerprint: str,
    sample_rate: int,
    channel_mode: str,
    algorithm_version: int = ALGORITHM_VERSION,
) -> str:
    """sha256(fingerprint + algorithm version + sample rate + channel mode) (spec #62)."""
    payload = f"{fast_fingerprint}|{algorithm_version}|{sample_rate}|{channel_mode}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def save_pyramid(path: Path, pyramid: WaveformPyramid) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {f"level_{i}_peaks": level.peaks for i, level in enumerate(pyramid.levels)}
    block_sizes = np.array([level.block_size for level in pyramid.levels], dtype=np.int64)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    # np.savez_compressed silently appends ".npz" to any path that doesn't already end
    # in it -- passing a path like "cache.npz.tmp" would actually write
    # "cache.npz.tmp.npz" and the rename below would then fail to find it. Passing an
    # already-open file handle avoids that renaming behavior entirely.
    with tmp_path.open("wb") as handle:
        np.savez_compressed(
            handle, block_sizes=block_sizes, sample_rate=np.array([pyramid.sample_rate]), **arrays
        )
    tmp_path.replace(path)


def load_pyramid(path: Path) -> WaveformPyramid:
    with np.load(path) as data:
        block_sizes = data["block_sizes"]
        sample_rate = int(data["sample_rate"][0])
        levels = [
            PyramidLevel(block_size=int(block_size), peaks=data[f"level_{i}_peaks"])
            for i, block_size in enumerate(block_sizes)
        ]
    return WaveformPyramid(levels=tuple(levels), sample_rate=sample_rate)
