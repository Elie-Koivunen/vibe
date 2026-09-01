from __future__ import annotations

import shutil
import sqlite3
import wave
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from bookmark_studio.app.waveform_orchestrator import WaveformOrchestrator
from bookmark_studio.domain.media import Media
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.persistence.waveform_repository import WaveformCacheRepository
from bookmark_studio.waveform.service import WaveformService


def _insert_media(conn, media_id) -> None:
    MediaRepository(conn).insert(
        Media(
            id=media_id, canonical_uri=None, filename=None, title=None, artist=None,
            album=None, duration_us=None, file_size=None, mtime_ns=None, fast_fingerprint=None,
        )
    )

FFMPEG_PATH = shutil.which("ffmpeg") or r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"
_HAS_FFMPEG = Path(FFMPEG_PATH).exists()


def _write_test_wav(path: Path, seconds: float = 1.0, sample_rate: int = 22050) -> None:
    n = int(seconds * sample_rate)
    t = np.arange(n) / sample_rate
    samples = (np.sin(2 * np.pi * 300 * t) * 0.4 * 32767).astype(np.int16)
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(samples.tobytes())


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not found on this machine")
def test_orchestrator_persists_on_main_thread_and_emits_ready(tmp_path, qtbot) -> None:
    wav_path = tmp_path / "tone.wav"
    _write_test_wav(wav_path)

    conn = sqlite3.connect(":memory:")
    migrate(conn)
    repo = WaveformCacheRepository(conn)
    service = WaveformService(ffmpeg_path=FFMPEG_PATH, cache_dir=tmp_path / "cache")
    orchestrator = WaveformOrchestrator(service=service, repository=repo)

    media_id = uuid4()
    _insert_media(conn, media_id)
    with qtbot.waitSignal(orchestrator.waveform_ready, timeout=15000) as blocker:
        orchestrator.request(media_id, "fp-orchestrator", str(wav_path))

    got_media_id, pyramid = blocker.args
    assert got_media_id == media_id
    assert pyramid.levels[0].peaks.shape[0] > 0

    # The DB write happened on the thread that owns `conn` (the test/main thread) --
    # this would raise sqlite3.ProgrammingError immediately if it had happened on the
    # QThreadPool worker instead (see the bug this class was written to avoid).
    cache_id = WaveformService.compute_cache_key("fp-orchestrator", 8000, "mono")
    assert repo.lookup(cache_id) is not None


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not found on this machine")
def test_orchestrator_second_request_hits_cache_without_a_job(tmp_path, qtbot) -> None:
    wav_path = tmp_path / "tone.wav"
    _write_test_wav(wav_path)

    conn = sqlite3.connect(":memory:")
    migrate(conn)
    repo = WaveformCacheRepository(conn)
    service = WaveformService(ffmpeg_path=FFMPEG_PATH, cache_dir=tmp_path / "cache")
    orchestrator = WaveformOrchestrator(service=service, repository=repo)

    media_id = uuid4()
    _insert_media(conn, media_id)
    with qtbot.waitSignal(orchestrator.waveform_ready, timeout=15000):
        orchestrator.request(media_id, "fp-cache-hit", str(wav_path))

    # Second request for the same fingerprint should resolve synchronously (cache hit),
    # even with a broken ffmpeg path -- proving it never dispatches a new job.
    broken_service = WaveformService(ffmpeg_path="not-a-real-binary.exe", cache_dir=tmp_path / "cache")
    orchestrator2 = WaveformOrchestrator(service=broken_service, repository=repo)
    received = []
    orchestrator2.waveform_ready.connect(lambda mid, pyr: received.append((mid, pyr)))
    orchestrator2.request(media_id, "fp-cache-hit", str(wav_path))
    assert len(received) == 1
