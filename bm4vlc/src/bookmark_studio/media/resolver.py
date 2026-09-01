"""MediaResolver: resolves a VLC-reported item to a stable Media identity (spec #69)."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname
from uuid import uuid4

from bookmark_studio.domain.media import Media
from bookmark_studio.media.fingerprint import fast_fingerprint
from bookmark_studio.persistence.media_repository import MediaRepository


def uri_to_local_path(uri: str) -> Path | None:
    """Converts a file:// URI to a local Path, or None for non-file URIs (spec #70)."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(url2pathname(parsed.path))


def path_to_uri(path: Path) -> str:
    """Canonical file:// URI, resolved but not lower-cased (spec #70)."""
    return path.resolve(strict=False).as_uri()


class MediaResolver:
    """Implements the matching order from spec #69: URI, alias, fingerprint, then new record."""

    def __init__(self, repository: MediaRepository) -> None:
        self._repository = repository

    def resolve(self, uri: str, *, title: str | None = None, artist: str | None = None,
                album: str | None = None, duration_us: int | None = None) -> Media:
        existing = self._repository.resolve_by_uri(uri)
        if existing is not None:
            return existing

        local_path = uri_to_local_path(uri)
        if local_path is not None and local_path.is_file():
            fingerprint = fast_fingerprint(local_path)
            by_fingerprint = self._repository.resolve_by_fingerprint(fingerprint)
            if by_fingerprint is not None:
                # File moved/renamed: keep the same Media ID, update canonical URI (spec #103).
                self._repository.relocate(by_fingerprint.id, uri)
                return self._repository.get(by_fingerprint.id) or by_fingerprint

            stat = local_path.stat()
            media = Media(
                id=uuid4(),
                canonical_uri=uri,
                filename=local_path.name,
                title=title,
                artist=artist,
                album=album,
                duration_us=duration_us,
                file_size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                fast_fingerprint=fingerprint,
            )
            return self._repository.insert(media)

        # Non-local or missing file: identity by URI only, no fingerprint available yet.
        media = Media(
            id=uuid4(),
            canonical_uri=uri,
            filename=local_path.name if local_path else uri.rsplit("/", 1)[-1],
            title=title,
            artist=artist,
            album=album,
            duration_us=duration_us,
            file_size=None,
            mtime_ns=None,
            fast_fingerprint=None,
        )
        return self._repository.insert(media)
