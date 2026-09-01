"""JSON schema/manifest validation, plus dict<->domain-object conversion, for the
.vlcbmk project format (spec #90-#91, #127)."""
from __future__ import annotations

from uuid import UUID

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.domain.lane import Lane
from bookmark_studio.domain.media import Media
from bookmark_studio.domain.playlist import Playlist

FORMAT_NAME = "vlc-bookmark-studio"
FORMAT_VERSION = 1


class ProjectFormatUnsupported(Exception):
    """Raised when a .vlcbmk archive's manifest declares an unsupported major version."""


def validate_manifest(manifest: dict) -> None:
    """Unknown fields are ignored for forward compatibility (spec #91); only `format`
    and a supported `format_version` are required."""
    if manifest.get("format") != FORMAT_NAME:
        raise ProjectFormatUnsupported(f"unrecognized project format: {manifest.get('format')!r}")
    version = manifest.get("format_version")
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise ProjectFormatUnsupported(
            f"project format_version {version!r} is newer than this app supports "
            f"(max {FORMAT_VERSION})"
        )


def validate_bookmark_dict(entry: dict) -> None:
    required = {"id", "media_id", "scope", "bookmark_type", "name", "start_us"}
    missing = required - entry.keys()
    if missing:
        raise ValueError(f"bookmark entry missing required fields: {sorted(missing)}")
    if entry["start_us"] < 0:
        raise ValueError(f"bookmark {entry['id']!r} has negative start_us")
    end_us = entry.get("end_us")
    if end_us is not None and end_us <= entry["start_us"]:
        raise ValueError(f"bookmark {entry['id']!r} has end_us <= start_us")


def bookmark_to_dict(bookmark: Bookmark) -> dict:
    return {
        "id": str(bookmark.id),
        "playlist_id": str(bookmark.playlist_id) if bookmark.playlist_id else None,
        "media_id": str(bookmark.media_id),
        "scope": bookmark.scope.value,
        "lane_id": str(bookmark.lane_id) if bookmark.lane_id else None,
        "bookmark_type": bookmark.bookmark_type.value,
        "name": bookmark.name,
        "start_us": bookmark.start_us,
        "end_us": bookmark.end_us,
        "loop_enabled": bookmark.loop_enabled,
        "repeat_count": bookmark.repeat_count,
        "loop_gap_ms": bookmark.loop_gap_ms,
        "completion_action": bookmark.completion_action.value,
        "color_key": bookmark.color_key,
        "notes": bookmark.notes,
        "tags": list(bookmark.tags),
    }


def bookmark_from_dict(entry: dict) -> Bookmark:
    validate_bookmark_dict(entry)
    return Bookmark(
        id=UUID(entry["id"]),
        playlist_id=UUID(entry["playlist_id"]) if entry.get("playlist_id") else None,
        media_id=UUID(entry["media_id"]),
        scope=BookmarkScope(entry["scope"]),
        lane_id=UUID(entry["lane_id"]) if entry.get("lane_id") else None,
        bookmark_type=BookmarkType(entry["bookmark_type"]),
        name=entry["name"],
        start_us=entry["start_us"],
        end_us=entry.get("end_us"),
        loop_enabled=bool(entry.get("loop_enabled", False)),
        repeat_count=entry.get("repeat_count"),
        loop_gap_ms=entry.get("loop_gap_ms", 0),
        completion_action=CompletionAction(entry.get("completion_action", "continue")),
        color_key=entry.get("color_key"),
        notes=entry.get("notes"),
        tags=tuple(entry.get("tags", [])),
    )


def playlist_to_dict(playlist: Playlist) -> dict:
    return {
        "id": str(playlist.id),
        "name": playlist.name,
        "source_uri": playlist.source_uri,
        "is_ad_hoc": playlist.is_ad_hoc,
    }


def playlist_from_dict(entry: dict) -> Playlist:
    return Playlist(
        id=UUID(entry["id"]),
        name=entry["name"],
        source_uri=entry.get("source_uri"),
        is_ad_hoc=bool(entry.get("is_ad_hoc", False)),
    )


def media_to_dict(media: Media) -> dict:
    return {
        "id": str(media.id),
        "canonical_uri": media.canonical_uri,
        "filename": media.filename,
        "title": media.title,
        "artist": media.artist,
        "album": media.album,
        "duration_us": media.duration_us,
        "file_size": media.file_size,
        "mtime_ns": media.mtime_ns,
        "fast_fingerprint": media.fast_fingerprint,
        "full_sha256": media.full_sha256,
    }


def media_from_dict(entry: dict) -> Media:
    return Media(
        id=UUID(entry["id"]),
        canonical_uri=entry.get("canonical_uri"),
        filename=entry.get("filename"),
        title=entry.get("title"),
        artist=entry.get("artist"),
        album=entry.get("album"),
        duration_us=entry.get("duration_us"),
        file_size=entry.get("file_size"),
        mtime_ns=entry.get("mtime_ns"),
        fast_fingerprint=entry.get("fast_fingerprint"),
        full_sha256=entry.get("full_sha256"),
    )


def lane_to_dict(lane: Lane) -> dict:
    return {
        "id": str(lane.id),
        "playlist_id": str(lane.playlist_id),
        "name": lane.name,
        "order_index": lane.order_index,
        "visible": lane.visible,
        "locked": lane.locked,
        "color_key": lane.color_key,
    }


def lane_from_dict(entry: dict) -> Lane:
    return Lane(
        id=UUID(entry["id"]),
        playlist_id=UUID(entry["playlist_id"]),
        name=entry["name"],
        order_index=entry["order_index"],
        visible=bool(entry.get("visible", True)),
        locked=bool(entry.get("locked", False)),
        color_key=entry.get("color_key"),
    )
