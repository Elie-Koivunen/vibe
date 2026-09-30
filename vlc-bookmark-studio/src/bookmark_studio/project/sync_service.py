"""Keeps bookmark databases in step through a shared folder.

Every installation (Windows, WSL, another PC) writes its whole database to its own file
``vlc-bookmark-studio-sync-<machine>.vlcbmk`` in the folder and merges the other installations' files
into its own database (import mode ``sync``: the most recent change of each bookmark
wins, deletions travel as tombstones). Nothing is ever written to another installation's
file, so two machines can't clobber each other, and any tool that copies folders
(OneDrive, Dropbox, Syncthing, a network share, or just ``/mnt/c/...`` seen from WSL)
works as the transport.

Media files are matched by content fingerprint and playlists by their song order, so a
song at ``C:\\Music\\a.mp3`` on Windows and ``/mnt/c/Music/a.mp3`` in WSL are the same.
Lanes and playlist orders carry no timestamps: they are added when missing but never
overwritten by another machine.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

from bookmark_studio import platform_support
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.lane_repository import LaneRepository
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.project.export_service import ProjectData, export_project
from bookmark_studio.project.import_service import ImportStats, apply_import_plan, read_import_plan
from bookmark_studio.project.schema import bookmark_to_dict, lane_to_dict, media_to_dict, playlist_to_dict

SYNC_FILE_PREFIX = "vlc-bookmark-studio-sync-"
# Files written before 0.4.0 (when the app was called bm4vlc) are still merged.
LEGACY_SYNC_FILE_PREFIX = "bm4vlc-sync-"
SYNC_FILE_SUFFIX = ".vlcbmk"
_MACHINE_KEY = "sync_machine_id"

_log = logging.getLogger(__name__)


def _platform_tag() -> str:
    if platform_support.is_wsl():
        return "wsl"
    if platform_support.IS_WINDOWS:
        return "win"
    return "linux"


def machine_id(conn: sqlite3.Connection) -> str:
    """This database's sync identity, created on first use. Stored in the database (not in
    QSettings) because Windows and WSL on the same PC share a host name but not a database."""
    row = conn.execute("SELECT value FROM settings_metadata WHERE key = ?", (_MACHINE_KEY,)).fetchone()
    if row and row[0]:
        return str(row[0])
    host = re.sub(r"[^A-Za-z0-9_-]+", "-", socket.gethostname()).strip("-")[:24] or "host"
    value = f"{host}-{_platform_tag()}-{uuid4().hex[:8]}"
    with conn:
        conn.execute(
            "INSERT INTO settings_metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_MACHINE_KEY, value),
        )
    return value


class SyncService:
    def __init__(self, conn: sqlite3.Connection, sync_dir: Path | str) -> None:
        self._conn = conn
        self.sync_dir = Path(sync_dir)
        self.machine_id = machine_id(conn)
        self._imported: dict[str, str] = {}  # file name -> sha256 of the last merged version
        self._exported_digest: str | None = None

    @property
    def own_path(self) -> Path:
        return self.sync_dir / f"{SYNC_FILE_PREFIX}{self.machine_id}{SYNC_FILE_SUFFIX}"

    # -- export --

    def snapshot(self) -> ProjectData:
        playlists_repo = PlaylistRepository(self._conn)
        bookmarks_repo = BookmarkRepository(self._conn)
        playlists = [record.playlist for record in playlists_repo.list_all()]
        items: list[tuple[UUID, list[UUID]]] = []
        signatures: list[tuple[UUID, str]] = []
        for playlist in playlists:
            media_ids = playlists_repo.list_item_media_ids(playlist.id)
            if media_ids:
                items.append((playlist.id, media_ids))
            signatures.extend((playlist.id, s) for s in playlists_repo.signatures_for(playlist.id))
        return ProjectData(
            playlists=playlists,
            media=MediaRepository(self._conn).list_all(),
            bookmarks=bookmarks_repo.list_all(),
            lanes=LaneRepository(self._conn).list_all(),
            playlist_items=items,
            playlist_signatures=signatures,
            bookmark_updated_at={UUID(k): v for k, v in bookmarks_repo.updated_at_by_id().items()},
            tombstones={UUID(k): v for k, v in bookmarks_repo.tombstones().items()},
            machine_id=self.machine_id,
        )

    @staticmethod
    def _digest(data: ProjectData) -> str:
        content = {
            "playlists": [playlist_to_dict(p) for p in data.playlists],
            "media": [media_to_dict(m) for m in data.media],
            "bookmarks": [bookmark_to_dict(b) for b in data.bookmarks],
            "lanes": [lane_to_dict(lane) for lane in data.lanes],
            "items": [[str(p), [str(m) for m in ids]] for p, ids in data.playlist_items],
            "signatures": sorted([str(p), s] for p, s in data.playlist_signatures),
            "updated": sorted((str(k), v) for k, v in data.bookmark_updated_at.items()),
            "tombstones": sorted((str(k), v) for k, v in data.tombstones.items()),
        }
        return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()

    def export_now(self, *, force: bool = False) -> bool:
        """Writes this database to its own sync file if it changed since the last export."""
        data = self.snapshot()
        digest = self._digest(data)
        if not force and digest == self._exported_digest and self.own_path.exists():
            return False
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        export_project(self.own_path, data)
        self._exported_digest = digest
        return True

    # -- import --

    def other_files(self) -> list[Path]:
        if not self.sync_dir.is_dir():
            return []
        own = {self.own_path.name, f"{LEGACY_SYNC_FILE_PREFIX}{self.machine_id}{SYNC_FILE_SUFFIX}"}
        found = {
            path for prefix in (SYNC_FILE_PREFIX, LEGACY_SYNC_FILE_PREFIX)
            for path in self.sync_dir.glob(f"{prefix}*{SYNC_FILE_SUFFIX}")
        }
        return sorted(path for path in found if path.name not in own and path.is_file())

    def import_others(self) -> ImportStats:
        total = ImportStats()
        for path in self.other_files():
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:  # being written or synced right now: next round
                _log.info("sync: can't read %s yet (%s)", path.name, exc)
                continue
            if self._imported.get(path.name) == digest:
                continue
            try:
                stats = apply_import_plan(self._conn, read_import_plan(path), mode="sync")
            except Exception:  # noqa: BLE001 - one bad file must not stop the others
                _log.exception("sync: could not merge %s", path.name)
                continue
            self._imported[path.name] = digest
            total.bookmarks_written += stats.bookmarks_written
            total.bookmarks_deleted += stats.bookmarks_deleted
            if stats.changed:
                _log.info(
                    "sync: %s -> %d bookmark(s) updated, %d deleted",
                    path.name, stats.bookmarks_written, stats.bookmarks_deleted,
                )
        return total

    def sync_now(self) -> bool:
        """Merges the other installations' files, then publishes the merged result.
        True if the local bookmarks changed."""
        stats = self.import_others()
        self.export_now()
        return stats.changed


__all__ = ["SYNC_FILE_PREFIX", "SYNC_FILE_SUFFIX", "SyncService", "machine_id"]
