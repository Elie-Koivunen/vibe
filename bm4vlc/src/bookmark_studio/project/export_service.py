"""Writes a .vlcbmk archive atomically via a .tmp file + rename (spec #90-#91, #128)."""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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

APPLICATION_VERSION = "0.1.0"


@dataclass(frozen=True, slots=True)
class ProjectData:
    playlists: list[Playlist]
    media: list[Media]
    bookmarks: list[Bookmark]
    lanes: list[Lane]


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
    tmp_path.replace(path)
