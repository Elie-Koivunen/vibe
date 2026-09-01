"""WaveformService: dedupes and dispatches WaveformGenerationJob work (spec #64-#66)."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from bookmark_studio.waveform.cache import ALGORITHM_VERSION, cache_key, save_pyramid
from bookmark_studio.waveform.ffmpeg_decoder import CancellationToken, decode_media_to_pcm
from bookmark_studio.waveform.peaks import decode_pcm_f32le
from bookmark_studio.waveform.pyramid import WaveformPyramid, build_pyramid


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
        self._inflight: dict[WaveformKey, threading.Event] = {}
        self._results: dict[WaveformKey, GeneratedWaveform | Exception] = {}

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
    ) -> GeneratedWaveform:
        with self._lock:
            existing_event = self._inflight.get(key)
            if existing_event is not None:
                is_owner = False
            else:
                existing_event = threading.Event()
                self._inflight[key] = existing_event
                is_owner = True

        if not is_owner:
            existing_event.wait()
            result = self._results[key]
            if isinstance(result, Exception):
                raise result
            return result

        try:
            raw = decode_media_to_pcm(self._ffmpeg_path, media_path, cancellation=cancellation)
            samples = decode_pcm_f32le(raw)
            pyramid = build_pyramid(samples, sample_rate=sample_rate)

            cache_id = self.compute_cache_key(key.fast_fingerprint, sample_rate, channel_mode)
            cache_path = self._cache_dir / f"{cache_id}.npz"
            save_pyramid(cache_path, pyramid)

            generated = GeneratedWaveform(
                cache_key=cache_id,
                sample_rate=sample_rate,
                channel_mode=channel_mode,
                file_path=cache_path,
                pyramid=pyramid,
            )
            self._results[key] = generated
            return generated
        except Exception as exc:  # noqa: BLE001 - re-raised to every waiting caller
            self._results[key] = exc
            raise
        finally:
            with self._lock:
                del self._inflight[key]
            existing_event.set()
