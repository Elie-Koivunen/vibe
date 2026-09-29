"""Qt-thread orchestration for WaveformService: dispatch on QThreadPool, persist on the
main thread (spec #64, #186-#187). Not part of the spec's file layout table verbatim,
but required to satisfy those two threading rules without duplicating WaveformService's
dedup logic in the UI layer.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from bookmark_studio.persistence.waveform_repository import WaveformCacheEntry, WaveformCacheRepository
from bookmark_studio.waveform.cache import ALGORITHM_VERSION, load_pyramid
from bookmark_studio.waveform.ffmpeg_decoder import CancellationToken
from bookmark_studio.waveform.service import GeneratedWaveform, WaveformKey, WaveformService

# Decoding is CPU- and disk-heavy; preloading a whole playlist used to start one ffmpeg
# per CPU core at once. Two at a time keeps the machine responsive, and the track the
# user is actually looking at jumps the queue (see request()).
MAX_CONCURRENT_DECODES = 2
_PRIORITY_CURRENT = 10
_PRIORITY_PREFETCH = 0


class _WaveformSignals(QObject):
    finished = Signal(object, object)  # (WaveformKey, GeneratedWaveform)
    failed = Signal(object, str)  # (WaveformKey, error message)


class _WaveformJob(QRunnable):
    def __init__(
        self,
        service: WaveformService,
        key: WaveformKey,
        media_path: str,
        cancellation: CancellationToken,
        signals: _WaveformSignals,
    ) -> None:
        super().__init__()
        self._service = service
        self._key = key
        self._media_path = media_path
        self._cancellation = cancellation
        self._signals = signals

    def run(self) -> None:  # runs on a QThreadPool worker thread
        try:
            generated = self._service.generate(
                self._key, self._media_path, cancellation=self._cancellation
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the main thread via signal
            self._signals.failed.emit(self._key, str(exc))
            return
        self._signals.finished.emit(self._key, generated)


class WaveformOrchestrator(QObject):
    """Owns the main-thread side of waveform generation: cache lookup, dispatch,
    and persisting the result. Callers connect to `waveform_ready` / `waveform_failed`,
    and to `duration_known` for the exact decoded length of any track (including
    prefetched ones, without loading their waveform).
    """

    waveform_ready = Signal(object, object)  # (media_id: UUID, pyramid: WaveformPyramid)
    waveform_failed = Signal(object, str)  # (media_id: UUID, message)
    duration_known = Signal(object, object)  # (media_id: UUID, duration_us: int)

    def __init__(
        self,
        *,
        service: WaveformService,
        repository: WaveformCacheRepository,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._repository = repository
        if thread_pool is None:
            thread_pool = QThreadPool(self)
            thread_pool.setMaxThreadCount(MAX_CONCURRENT_DECODES)
        self._thread_pool = thread_pool
        self._cancellations: dict[WaveformKey, CancellationToken] = {}
        self._jobs: dict[WaveformKey, _WaveformJob] = {}
        self._signals: dict[WaveformKey, _WaveformSignals] = {}

    def request(
        self,
        media_id: UUID,
        fast_fingerprint: str,
        media_path: str,
        *,
        sample_rate: int = 8000,
        channel_mode: str = "mono",
    ) -> None:
        """The waveform to display now: served from the cache synchronously if present,
        otherwise decoded ahead of any queued prefetches."""
        key = WaveformKey(media_id=media_id, fast_fingerprint=fast_fingerprint)
        cache_id = WaveformService.compute_cache_key(fast_fingerprint, sample_rate, channel_mode)

        cached = self._repository.lookup(cache_id)
        if cached is not None and Path(cached.file_path).exists():
            pyramid = load_pyramid(Path(cached.file_path))
            duration_us = pyramid.duration_us
            if cached.duration_us is None and duration_us:
                self._repository.set_duration(cache_id, duration_us)  # backfill older cache rows
            self.waveform_ready.emit(media_id, pyramid)
            if duration_us:
                self.duration_known.emit(media_id, duration_us)
            return

        job = self._jobs.get(key)
        if job is not None:
            # Already queued or decoding (e.g. a playlist prefetch). If it hasn't started
            # yet, move it to the front instead of paying for a second decode.
            try:
                taken = self._thread_pool.tryTake(job)
            except RuntimeError:  # already ran and was auto-deleted by the pool
                taken = False
            if taken:
                self._thread_pool.start(job, _PRIORITY_CURRENT)
            return
        self._dispatch(key, media_path, _PRIORITY_CURRENT)

    def prefetch(
        self,
        media_id: UUID,
        fast_fingerprint: str,
        media_path: str,
        *,
        sample_rate: int = 8000,
        channel_mode: str = "mono",
    ) -> None:
        """Makes sure a waveform will be cached, without loading it: preloading a whole
        playlist used to read every cached .npz from disk on the UI thread just to throw
        it away. Emits duration_known when the cache already records the length."""
        key = WaveformKey(media_id=media_id, fast_fingerprint=fast_fingerprint)
        cache_id = WaveformService.compute_cache_key(fast_fingerprint, sample_rate, channel_mode)
        cached = self._repository.lookup(cache_id)
        if cached is not None and Path(cached.file_path).exists():
            if cached.duration_us:
                self.duration_known.emit(media_id, cached.duration_us)
            return
        if key in self._jobs:
            return
        self._dispatch(key, media_path, _PRIORITY_PREFETCH)

    def cancel(self, media_id: UUID, fast_fingerprint: str) -> None:
        """Cancels a switched-away-from request (spec #65: don't leave stale decoders running)."""
        key = WaveformKey(media_id=media_id, fast_fingerprint=fast_fingerprint)
        token = self._cancellations.get(key)
        if token is not None:
            token.cancel()

    def wait_for_idle(self, timeout_ms: int = 5000) -> bool:
        return self._thread_pool.waitForDone(timeout_ms)

    def cancel_all(self) -> None:
        for token in self._cancellations.values():
            token.cancel()

    def _dispatch(self, key: WaveformKey, media_path: str, priority: int) -> None:
        cancellation = CancellationToken()
        signals = _WaveformSignals()
        signals.finished.connect(self._on_finished)
        signals.failed.connect(self._on_failed)
        job = _WaveformJob(self._service, key, media_path, cancellation, signals)
        self._cancellations[key] = cancellation
        self._jobs[key] = job
        self._signals[key] = signals
        self._thread_pool.start(job, priority)

    def _forget(self, key: WaveformKey) -> None:
        self._cancellations.pop(key, None)
        self._jobs.pop(key, None)
        self._signals.pop(key, None)

    def _on_finished(self, key: WaveformKey, generated: GeneratedWaveform) -> None:
        # Runs on the main thread (Qt::AutoConnection queues cross-thread signals).
        duration_us = generated.pyramid.duration_us
        self._repository.put(
            WaveformCacheEntry(
                cache_key=generated.cache_key,
                media_id=key.media_id,
                algorithm_version=ALGORITHM_VERSION,
                sample_rate=generated.sample_rate,
                channel_mode=generated.channel_mode,
                file_path=str(generated.file_path),
                duration_us=duration_us,
            )
        )
        self._forget(key)
        self.waveform_ready.emit(key.media_id, generated.pyramid)
        if duration_us:
            self.duration_known.emit(key.media_id, duration_us)

    def _on_failed(self, key: WaveformKey, message: str) -> None:
        self._forget(key)
        self.waveform_failed.emit(key.media_id, message)
