"""Decodes media to mono f32le PCM via FFmpeg subprocess, argv-array only (spec #58)."""
from __future__ import annotations

FFMPEG_ANALYSIS_SAMPLE_RATE = 8000


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
