"""WaveformService: dedupes and dispatches WaveformGenerationJob work (spec #64-#66)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from uuid import UUID

from bookmark_studio.waveform.cache import ALGORITHM_VERSION, cache_key, save_pyramid
from bookmark_studio.waveform.ffmpeg_decoder import CancellationToken, stream_media_pcm
from bookmark_studio.waveform.peaks import PeakAccumulator, decode_pcm_f32le
from bookmark_studio.waveform.pyramid import BASE_BLOCK_SIZE, WaveformPyramid, build_pyramid_from_peaks

PROGRESS_INTERVAL_S = 1.0

__all__ = ["ALGORITHM_VERSION", "GeneratedWaveform", "WaveformKey", "WaveformService"]


@dataclass(frozen=True, slots=True)
class WaveformKey:
    media_id: UUID
    fast_fingerprint: str


@dataclass(frozen=True, slots=True)
class GeneratedWaveform:
    """Everything the caller needs to persist a WaveformCacheEntry, on its own thread."""

    cache_key: str
    sample_rate: int
    channel_mode: str
    file_path: Path
    pyramid: WaveformPyramid


@dataclass
class _Pending:
    """One in-flight decode. Waiters hold a reference to this object, not to a shared
    results dict -- the old dict kept every decoded pyramid (and every error) alive for
    the whole session, so memory grew with every song ever decoded."""

    done: threading.Event = field(default_factory=threading.Event)
    result: GeneratedWaveform | None = None
    error: BaseException | None = None


class WaveformService:
    """Decodes media to a WaveformPyramid and writes it to the on-disk .npz cache.

    Deliberately touches only the filesystem, never SQLite (spec #186-#187: "never
    access ... state from worker threads" / "never share one SQLite connection
    concurrently across arbitrary threads"). This class is safe to run on a background
    QThreadPool worker; the caller is responsible for persisting the returned
    GeneratedWaveform's metadata via WaveformCacheRepository back on the thread that
    owns that repository's connection (normally the main/application thread).

    Request deduplication (spec #66): concurrent callers for the same WaveformKey share
    one decode job instead of racing multiple ffmpeg processes.
    """

    def __init__(self, *, ffmpeg_path: str, cache_dir: Path) -> None:
        self._ffmpeg_path = ffmpeg_path
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        self._inflight: dict[WaveformKey, _Pending] = {}

    @staticmethod
    def compute_cache_key(fast_fingerprint: str, sample_rate: int, channel_mode: str) -> str:
        return cache_key(fast_fingerprint, sample_rate, channel_mode)

    def generate(
        self,
        key: WaveformKey,
        media_path: str,
        *,
        sample_rate: int = 8000,
        channel_mode: str = "mono",
        cancellation: CancellationToken | None = None,
        on_progress: Callable[[WaveformPyramid], None] | None = None,
    ) -> GeneratedWaveform:
        """Decodes (or waits for a concurrent decode of) one file. `on_progress` receives
        partial pyramids about once a second while a long file decodes (owner only)."""
        with self._lock:
            pending = self._inflight.get(key)
            is_owner = pending is None
            if pending is None:
                pending = _Pending()
                self._inflight[key] = pending

        if not is_owner:
            pending.done.wait()
            if pending.error is not None:
                raise pending.error
            assert pending.result is not None
            return pending.result

        try:
            # Streamed: peaks are reduced as the audio arrives, so memory no longer grows
            # with the length of the file, and a long file can be shown while it decodes.
            accumulator = PeakAccumulator(BASE_BLOCK_SIZE)
            last_progress = time.monotonic()
            for chunk in stream_media_pcm(self._ffmpeg_path, media_path, cancellation=cancellation):
                accumulator.feed(decode_pcm_f32le(chunk))
                if on_progress is not None and time.monotonic() - last_progress >= PROGRESS_INTERVAL_S:
                    last_progress = time.monotonic()
                    on_progress(build_pyramid_from_peaks(accumulator.peaks(), sample_rate))
            pyramid = build_pyramid_from_peaks(accumulator.peaks(final=True), sample_rate)

            cache_id = self.compute_cache_key(key.fast_fingerprint, sample_rate, channel_mode)
            cache_path = self._cache_dir / f"{cache_id}.npz"
            save_pyramid(cache_path, pyramid)

            pending.result = GeneratedWaveform(
                cache_key=cache_id,
                sample_rate=sample_rate,
                channel_mode=channel_mode,
                file_path=cache_path,
                pyramid=pyramid,
            )
            return pending.result
        except BaseException as exc:  # noqa: BLE001 - re-raised to every waiting caller
            pending.error = exc
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            pending.done.set()
