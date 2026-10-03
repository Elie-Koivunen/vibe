"""A song's tempo (BPM), estimated from its waveform -- the finest peaks the waveform
already has (one per 8 ms), so no second decode.

The loudness envelope's rises (onsets) are autocorrelated; the lag that repeats best,
weighed towards the common 80-160 BPM so a beat isn't read at half or double speed, is the
beat. Good to a BPM or so on music with a pulse; None when there isn't one (silence, a
few seconds of audio). The Volume & EQ tab lets the user put it right."""
from __future__ import annotations

import numpy as np

from bookmark_studio.waveform.pyramid import WaveformPyramid

MIN_BPM = 60.0
MAX_BPM = 200.0
_PREFERRED_BPM = 120.0
_MIN_SECONDS = 8.0
_MAX_SECONDS = 180.0  # the middle of a long song is enough (and skips intros and outros)
_MIN_PULSE = 0.05  # the best lag's correlation, relative to the envelope's own energy
_MIN_ONSET = 0.01  # how much the (log) loudness rises per frame, on average, at the least


def estimate_bpm(pyramid: WaveformPyramid) -> float | None:
    if not pyramid.levels or pyramid.sample_rate <= 0:
        return None
    finest = min(pyramid.levels, key=lambda level: level.block_size)
    frame_rate = pyramid.sample_rate / finest.block_size
    peaks = finest.peaks
    if peaks.shape[0] < frame_rate * _MIN_SECONDS:
        return None
    envelope = np.maximum(np.abs(peaks[:, 0]), np.abs(peaks[:, 1])).astype(np.float64)
    take = int(min(envelope.size, frame_rate * _MAX_SECONDS))
    start = (envelope.size - take) // 2
    envelope = envelope[start:start + take]
    top = float(envelope.max())
    if top <= 0:
        return None
    loudness = np.log1p(100.0 * envelope / top)
    onset = np.diff(loudness, prepend=loudness[0])
    onset[onset < 0] = 0.0
    window = max(1, int(frame_rate * 0.5))
    onset = onset - np.convolve(onset, np.ones(window) / window, mode="same")  # slow swells out
    onset[onset < 0] = 0.0
    if onset.mean() < _MIN_ONSET:
        return None  # a steady sound: no beat to speak of
    # A beat rarely lands on whole frames (128 BPM = every 58.6): spread each onset over
    # a few frames, so the correlation peaks between frames too.
    kernel = np.exp(-0.5 * (np.arange(-4, 5) / 1.5) ** 2)
    onset = np.convolve(onset, kernel / kernel.sum(), mode="same")
    onset -= onset.mean()
    size = 1 << int(np.ceil(np.log2(2 * onset.size)))
    spectrum = np.fft.rfft(onset, size)
    acf = np.fft.irfft(spectrum * np.conj(spectrum), size)[:onset.size]
    if acf[0] <= 0:
        return None
    acf = acf / acf[0]
    lag_min = int(np.floor(frame_rate * 60.0 / MAX_BPM))
    lag_max = int(np.ceil(frame_rate * 60.0 / MIN_BPM))
    lags = np.arange(max(1, lag_min), min(lag_max, acf.size // 2 - 1) + 1)
    if lags.size == 0:
        return None
    # A beat repeats every 2, 3... beats as well: the prior leans towards the usual tempos.
    pulse = acf[lags]
    bpms = 60.0 * frame_rate / lags
    prior = np.exp(-0.5 * (np.log2(bpms / _PREFERRED_BPM) / 0.9) ** 2)
    scores = pulse * prior
    best = int(np.argmax(scores))
    if acf[lags[best]] < _MIN_PULSE:
        return None
    lag = float(lags[best])
    if 0 < lags[best] < acf.size - 1:  # between frames: the parabola through the peak
        a, b, c = acf[lags[best] - 1], acf[lags[best]], acf[lags[best] + 1]
        denominator = a - 2 * b + c
        if denominator < 0:
            lag += 0.5 * (a - c) / denominator
    return round(60.0 * frame_rate / lag, 1)


def tempo_rate(song_bpm: float, change_bpm: float) -> float:
    """The playback rate that turns `song_bpm` into `song_bpm + change_bpm` (VLC keeps the
    pitch), within what VLC plays (0.25-4x)."""
    if song_bpm <= 0:
        return 1.0
    return max(0.25, min(4.0, (song_bpm + change_bpm) / song_bpm))
