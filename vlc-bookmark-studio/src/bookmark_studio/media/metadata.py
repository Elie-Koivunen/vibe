"""VLC-metadata-first, filename-fallback title/artist/album resolution (spec #136)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResolvedMetadata:
    title: str
    artist: str | None
    album: str | None


def resolve_metadata(
    *,
    vlc_title: str | None,
    vlc_artist: str | None,
    vlc_album: str | None,
    filename: str,
) -> ResolvedMetadata:
    """VLC-reported metadata wins; an empty/whitespace-only title falls back to the filename."""
    title = vlc_title.strip() if vlc_title and vlc_title.strip() else _title_from_filename(filename)
    artist = vlc_artist.strip() if vlc_artist and vlc_artist.strip() else None
    album = vlc_album.strip() if vlc_album and vlc_album.strip() else None
    return ResolvedMetadata(title=title, artist=artist, album=album)


def _title_from_filename(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return stem or filename
