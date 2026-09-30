from __future__ import annotations

import shutil
import threading
import wave
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from bookmark_studio.waveform.service import WaveformKey, WaveformService

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
def test_generate_writes_disk_cache_only_no_db_access(tmp_path: Path) -> None:
    """WaveformService must stay SQLite-free -- see spec #186-#187 and app/waveform_orchestrator.py."""
    wav_path = tmp_path / "tone.wav"
    _write_test_wav(wav_path)

    service = WaveformService(ffmpeg_path=FFMPEG_PATH, cache_dir=tmp_path / "cache")
    key = WaveformKey(media_id=uuid4(), fast_fingerprint="fp-1")

    generated = service.generate(key, str(wav_path))
    assert generated.pyramid.levels[0].peaks.shape[0] > 0
    assert generated.file_path.exists()
    assert generated.file_path.parent == tmp_path / "cache"


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not found on this machine")
def test_concurrent_requests_for_same_key_dedupe(tmp_path: Path) -> None:
    """spec #66: three simultaneous requesters for the same waveform trigger only one job."""
    wav_path = tmp_path / "tone.wav"
    _write_test_wav(wav_path, seconds=2.0)

    service = WaveformService(ffmpeg_path=FFMPEG_PATH, cache_dir=tmp_path / "cache")
    key = WaveformKey(media_id=uuid4(), fast_fingerprint="fp-concurrent")

    results: list[object] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            results.append(service.generate(key, str(wav_path)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert len(results) == 3
    assert len(list((tmp_path / "cache").glob("*.npz"))) == 1
    # All three callers got the same generated cache_key.
    assert len({r.cache_key for r in results}) == 1
