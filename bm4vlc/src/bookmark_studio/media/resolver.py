"""MediaResolver: resolves a VLC-reported item to a stable Media identity (spec #69)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from bookmark_studio import platform_support
from bookmark_studio.domain.media import Media
from bookmark_studio.media.fingerprint import fast_fingerprint
from bookmark_studio.persistence.media_repository import MediaRepository


def uri_to_local_path(uri: str) -> Path | None:
    """Converts a file:// URI to a local Path, or None for non-file URIs (spec #70).

    Platform-aware: a Windows VLC's ``file:///C:/...`` URIs map to ``/mnt/c/...`` when
    this app runs under WSL, UNC shares work on Windows (see platform_support)."""
    return platform_support.uri_to_local_path(uri)


def path_to_uri(path: Path) -> str:
    """Canonical file:// URI, resolved but not lower-cased (spec #70)."""
    return platform_support.path_to_uri(path)


class MediaResolver:
    """Implements the matching order from spec #69: URI, alias, fingerprint, then new record."""

    def __init__(self, repository: MediaRepository) -> None:
        self._repository = repository

    def resolve(self, uri: str, *, title: str | None = None, artist: str | None = None,
                album: str | None = None, duration_us: int | None = None) -> Media:
        existing = self._repository.resolve_by_uri(uri)
        if existing is not None:
            return self._revalidated(existing, uri)

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
                duration_us=duration_us if duration_us and duration_us > 0 else None,
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
            duration_us=duration_us if duration_us and duration_us > 0 else None,
            file_size=None,
            mtime_ns=None,
            fast_fingerprint=None,
        )
        return self._repository.insert(media)

    def _revalidated(self, media: Media, uri: str) -> Media:
        """A URI match alone can't be trusted forever: replacing the file at that path
        (a re-encode, or a different song saved under the same name) would keep showing
        the OLD waveform, because the cache is keyed by the fingerprint recorded the
        first time. When size or mtime changed, re-fingerprint and record the new content.
        Bookmarks stay attached to the same media id -- there's no way to know whether
        they still make sense, and silently hiding them would be worse."""
        if media.file_size is None or media.mtime_ns is None:
            return media
        local_path = uri_to_local_path(uri)
        if local_path is None:
            return media
        try:
            stat = local_path.stat()
        except OSError:
            return media
        if stat.st_size == media.file_size and stat.st_mtime_ns == media.mtime_ns:
            return media
        try:
            fingerprint = fast_fingerprint(local_path)
        except OSError:
            return media
        updated = replace(media, file_size=stat.st_size, mtime_ns=stat.st_mtime_ns, fast_fingerprint=fingerprint)
        self._repository.update(updated)
        return updated
