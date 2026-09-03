"""Decodes media to mono f32le PCM via FFmpeg subprocess, argv-array only (spec #58)."""
from __future__ import annotations

import subprocess
import sys
import threading

FFMPEG_ANALYSIS_SAMPLE_RATE = 8000
_READ_CHUNK_BYTES = 1 << 20

# Direct user request: "weird behavior, when i launch vlc with a playlist, it started
# opening many cmd windows" -- ffmpeg.exe is a console app; spawning one from a
# windowless pythonw.exe process without this flag makes Windows pop up a real,
# visible console window per subprocess. Playlist-wide waveform preloading
# (_preload_playlist_waveforms) can have several of these running at once, so it
# showed up as a stack of console windows rather than just one easy-to-miss flash.
# CREATE_NO_WINDOW only exists on Windows, so this is guarded to that platform.
_POPEN_KWARGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class WaveformCancelled(Exception):
    """Raised when decoding is aborted via a CancellationToken (spec #65)."""


class CancellationToken:
    """Cooperative cancellation flag shared between a caller and a background job (spec #65)."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        self.cancelled.set()


def build_ffmpeg_args(ffmpeg_path: str, media_path: str) -> list[str]:
    return [
        ffmpeg_path,
        "-v", "error",
        "-i", media_path,
        "-map", "0:a:0",
        "-ac", "1",
        "-ar", str(FFMPEG_ANALYSIS_SAMPLE_RATE),
        "-f", "f32le",
        "pipe:1",
    ]


def decode_media_to_pcm(
    ffmpeg_path: str,
    media_path: str,
    *,
    cancellation: CancellationToken | None = None,
) -> bytes:
    """Runs ffmpeg and returns raw f32le PCM bytes. Never uses shell=True (spec #58)."""
    args = build_ffmpeg_args(ffmpeg_path, media_path)
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_POPEN_KWARGS)
    chunks: list[bytes] = []
    try:
        assert process.stdout is not None
        while True:
            if cancellation is not None and cancellation.cancelled.is_set():
                process.kill()
                process.wait()
                raise WaveformCancelled(f"decoding cancelled: {media_path}")
            chunk = process.stdout.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
        process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()

    if process.returncode != 0:
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        raise RuntimeError(f"ffmpeg exited {process.returncode} for {media_path}: {stderr.strip()}")

    return b"".join(chunks)
