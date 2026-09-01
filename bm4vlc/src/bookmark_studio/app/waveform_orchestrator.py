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
from bookmark_studio.waveform.pyramid import WaveformPyramid
from bookmark_studio.waveform.service import GeneratedWaveform, WaveformKey, WaveformService


class _WaveformSignals(QObject):
    finished = Signal(object, object)  # (WaveformKey, WaveformPyramid)
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
    and persisting the result. Callers connect to `waveform_ready` / `waveform_failed`.
    """

    waveform_ready = Signal(object, object)  # (media_id: UUID, pyramid: WaveformPyramid)
    waveform_failed = Signal(object, str)  # (media_id: UUID, message)

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
        self._thread_pool = thread_pool or QThreadPool.globalInstance()
        self._cancellations: dict[WaveformKey, CancellationToken] = {}

    def request(
        self,
        media_id: UUID,
        fast_fingerprint: str,
        media_path: str,
        *,
        sample_rate: int = 8000,
        channel_mode: str = "mono",
    ) -> None:
        key = WaveformKey(media_id=media_id, fast_fingerprint=fast_fingerprint)
        cache_id = WaveformService.compute_cache_key(fast_fingerprint, sample_rate, channel_mode)

        cached = self._repository.lookup(cache_id)
        if cached is not None and Path(cached.file_path).exists():
            self.waveform_ready.emit(media_id, load_pyramid(Path(cached.file_path)))
            return

        cancellation = CancellationToken()
        self._cancellations[key] = cancellation

        signals = _WaveformSignals()
        signals.finished.connect(self._on_finished)
        signals.failed.connect(self._on_failed)

        job = _WaveformJob(self._service, key, media_path, cancellation, signals)
        self._thread_pool.start(job)

    def cancel(self, media_id: UUID, fast_fingerprint: str) -> None:
        """Cancels a switched-away-from request (spec #65: don't leave stale decoders running)."""
        key = WaveformKey(media_id=media_id, fast_fingerprint=fast_fingerprint)
        token = self._cancellations.get(key)
        if token is not None:
            token.cancel()

    def _on_finished(self, key: WaveformKey, generated: GeneratedWaveform) -> None:
        # Runs on the main thread (Qt::AutoConnection queues cross-thread signals).
        self._repository.put(
            WaveformCacheEntry(
                cache_key=generated.cache_key,
                media_id=key.media_id,
                algorithm_version=ALGORITHM_VERSION,
                sample_rate=generated.sample_rate,
                channel_mode=generated.channel_mode,
                file_path=str(generated.file_path),
            )
        )
        self._cancellations.pop(key, None)
        self.waveform_ready.emit(key.media_id, generated.pyramid)

    def _on_failed(self, key: WaveformKey, message: str) -> None:
        self._cancellations.pop(key, None)
        self.waveform_failed.emit(key.media_id, message)
