"""Decodes media to mono f32le PCM via FFmpeg subprocess, argv-array only (spec #58)."""
from __future__ import annotations

import subprocess
import threading
from collections import deque

from bookmark_studio import platform_support

FFMPEG_ANALYSIS_SAMPLE_RATE = 8000
_READ_CHUNK_BYTES = 1 << 20
_STDERR_TAIL_LINES = 40

# Direct user request: "weird behavior, when i launch vlc with a playlist, it started
# opening many cmd windows" -- ffmpeg.exe is a console app; spawning one from a
# windowless pythonw.exe process without CREATE_NO_WINDOW makes Windows pop up a real,
# visible console window per subprocess (see platform_support.no_console_window_kwargs).


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
        "-nostdin",
        "-v", "error",
        # Under WSL a Windows ffmpeg.exe needs C:\... paths, not /mnt/c/...
        "-i", platform_support.path_for_program(media_path, ffmpeg_path),
        "-map", "0:a:0",
        "-ac", "1",
        "-ar", str(FFMPEG_ANALYSIS_SAMPLE_RATE),
        "-f", "f32le",
        "pipe:1",
    ]


def _drain(stream, tail: deque[bytes]) -> None:
    """Keeps reading ffmpeg's stderr so it can never fill its pipe buffer. Without this,
    a damaged file that makes ffmpeg log an error per frame blocked ffmpeg on a full
    stderr pipe while this process waited on stdout -- a permanent deadlock that
    silently hung the waveform for that song (and its worker thread) forever."""
    try:
        for line in iter(stream.readline, b""):
            tail.append(line)
    except (OSError, ValueError):
        pass


def decode_media_to_pcm(
    ffmpeg_path: str,
    media_path: str,
    *,
    cancellation: CancellationToken | None = None,
) -> bytes:
    """Runs ffmpeg and returns raw f32le PCM bytes. Never uses shell=True (spec #58)."""
    args = build_ffmpeg_args(ffmpeg_path, media_path)
    process = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **platform_support.no_console_window_kwargs(),
    )
    stderr_tail: deque[bytes] = deque(maxlen=_STDERR_TAIL_LINES)
    stderr_thread = threading.Thread(target=_drain, args=(process.stderr, stderr_tail), daemon=True)
    stderr_thread.start()
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
        stderr_thread.join(timeout=2)
        if process.stderr is not None:
            process.stderr.close()

    if process.returncode != 0:
        stderr = b"".join(stderr_tail).decode("utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg exited {process.returncode} for {media_path}: {stderr.strip()}")

    return b"".join(chunks)
