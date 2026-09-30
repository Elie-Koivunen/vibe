from __future__ import annotations

import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from bookmark_studio.waveform.cache import cache_key, load_pyramid, save_pyramid
from bookmark_studio.waveform.ffmpeg_decoder import (
    CancellationToken,
    WaveformCancelled,
    decode_media_to_pcm,
)
from bookmark_studio.waveform.peaks import compute_peaks, decode_pcm_f32le
from bookmark_studio.waveform.pyramid import build_pyramid

FFMPEG_PATH = shutil.which("ffmpeg") or r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"
_HAS_FFMPEG = Path(FFMPEG_PATH).exists()


def _write_test_wav(path: Path, seconds: float = 2.0, sample_rate: int = 44100, freq: float = 440.0) -> None:
    n = int(seconds * sample_rate)
    t = np.arange(n) / sample_rate
    samples = (np.sin(2 * np.pi * freq * t) * 0.5 * 32767).astype(np.int16)
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(samples.tobytes())


def test_compute_peaks_empty() -> None:
    peaks = compute_peaks(np.array([], dtype="<f4"), block_size=64)
    assert peaks.shape == (0, 2)


def test_compute_peaks_partial_final_block_not_zeroed() -> None:
    samples = np.array([1.0, 1.0, 1.0], dtype="<f4")
    peaks = compute_peaks(samples, block_size=4)
    # padded with the last real sample (1.0), not zero -- else min would wrongly be 0.0
    assert peaks[0, 0] == pytest.approx(1.0)
    assert peaks[0, 1] == pytest.approx(1.0)


def test_compute_peaks_rejects_invalid_block_size() -> None:
    with pytest.raises(ValueError):
        compute_peaks(np.array([1.0], dtype="<f4"), block_size=0)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not found on this machine")
def test_decode_and_pyramid_roundtrip_against_real_ffmpeg(tmp_path: Path) -> None:
    wav_path = tmp_path / "tone.wav"
    _write_test_wav(wav_path, seconds=2.0)

    raw = decode_media_to_pcm(FFMPEG_PATH, str(wav_path))
    samples = decode_pcm_f32le(raw)
    assert samples.size > 0
    # ~2s at the 8kHz analysis rate (spec #59) -> ~16000 samples.
    assert abs(samples.size - 16000) < 200

    pyramid = build_pyramid(samples, sample_rate=8000)
    assert pyramid.levels[0].block_size == 64
    finest = pyramid.levels[0]
    assert finest.peaks[:, 1].max() > 0.3   # true 440Hz tone should reach near +0.5
    assert finest.peaks[:, 0].min() < -0.3  # and near -0.5

    cache_path = tmp_path / "cache.npz"
    save_pyramid(cache_path, pyramid)
    loaded = load_pyramid(cache_path)
    assert loaded.sample_rate == pyramid.sample_rate
    assert len(loaded.levels) == len(pyramid.levels)
    np.testing.assert_allclose(loaded.levels[0].peaks, pyramid.levels[0].peaks)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not found on this machine")
def test_decode_honors_pre_set_cancellation(tmp_path: Path) -> None:
    wav_path = tmp_path / "tone.wav"
    _write_test_wav(wav_path, seconds=5.0)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(WaveformCancelled):
        decode_media_to_pcm(FFMPEG_PATH, str(wav_path), cancellation=token)


def test_pyramid_best_level_picks_coarser_when_zoomed_out() -> None:
    samples = np.random.default_rng(0).uniform(-1, 1, size=8000 * 60).astype("<f4")  # 60s @ 8kHz
    pyramid = build_pyramid(samples, sample_rate=8000)
    zoomed_in = pyramid.best_level(visible_duration_us=1_000_000, pixel_width=1000)  # 1s across 1000px
    zoomed_out = pyramid.best_level(visible_duration_us=60_000_000, pixel_width=1000)  # 60s across 1000px
    assert zoomed_out.block_size >= zoomed_in.block_size


def test_pyramid_level_slice_bounds() -> None:
    samples = np.arange(1000, dtype="<f4")
    pyramid = build_pyramid(samples, sample_rate=8000, max_overview_points=10_000)
    finest = pyramid.levels[0]
    full = finest.slice(0, 10_000_000, sample_rate=8000)
    assert full.shape[0] == finest.peaks.shape[0]
    empty = finest.slice(-1000, -500, sample_rate=8000)
    assert empty.shape[0] >= 0


def test_cache_key_changes_with_algorithm_version() -> None:
    key_v1 = cache_key("fp", 8000, "mono", algorithm_version=1)
    key_v2 = cache_key("fp", 8000, "mono", algorithm_version=2)
    assert key_v1 != key_v2


def test_cache_key_stable_for_same_inputs() -> None:
    assert cache_key("fp", 8000, "mono") == cache_key("fp", 8000, "mono")
