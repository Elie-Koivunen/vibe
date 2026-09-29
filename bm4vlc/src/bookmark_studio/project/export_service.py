"""Writes a .vlcbmk archive atomically via a .tmp file + rename (spec #90-#91, #128)."""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from bookmark_studio import __version__
from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.lane import Lane
from bookmark_studio.domain.media import Media
from bookmark_studio.domain.playlist import Playlist
from bookmark_studio.project.schema import (
    FORMAT_NAME,
    FORMAT_VERSION,
    bookmark_to_dict,
    lane_to_dict,
    media_to_dict,
    playlist_to_dict,
)

APPLICATION_VERSION = __version__


@dataclass(frozen=True, slots=True)
class ProjectData:
    playlists: list[Playlist]
    media: list[Media]
    bookmarks: list[Bookmark]
    lanes: list[Lane]
    # (playlist_id, ordered media ids) and (playlist_id, signature): what VLC-side
    # recognition needs to map an imported playlist onto a live VLC playlist.
    playlist_items: list[tuple[UUID, list[UUID]]] = field(default_factory=list)
    playlist_signatures: list[tuple[UUID, str]] = field(default_factory=list)


def export_project(path: Path, data: ProjectData) -> None:
    """Writes to `<path>.tmp`, finishes the archive, then renames into place (spec #128)
    so a crash mid-write never leaves a good project file overwritten by a partial one.
    """
    manifest = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "application_version": APPLICATION_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
    }

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        archive.writestr(
            "bookmarks.json", json.dumps([bookmark_to_dict(b) for b in data.bookmarks], indent=2)
        )
        archive.writestr(
            "playlists.json", json.dumps([playlist_to_dict(p) for p in data.playlists], indent=2)
        )
        archive.writestr("media.json", json.dumps([media_to_dict(m) for m in data.media], indent=2))
        archive.writestr("lanes.json", json.dumps([lane_to_dict(l) for l in data.lanes], indent=2))
        archive.writestr(
            "playlist_items.json",
            json.dumps(
                [{"playlist_id": str(pid), "media_ids": [str(m) for m in media_ids]}
                 for pid, media_ids in data.playlist_items],
                indent=2,
            ),
        )
        archive.writestr(
            "playlist_signatures.json",
            json.dumps([{"playlist_id": str(pid), "signature": sig} for pid, sig in data.playlist_signatures],
                       indent=2),
        )
    tmp_path.replace(path)
