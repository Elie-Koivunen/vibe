"""BookmarkRepository: bookmarks table, playlist+media scoped queries (spec #77, #79, #184)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from bookmark_studio.domain.bookmark import Bookmark, default_bookmark_name
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction

LEGACY_DEFAULT_NAME = "New bookmark"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BookmarkRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        """Exposed for callers (e.g. project export) that need the raw connection to
        query other repositories sharing the same database, not just bookmarks."""
        return self._conn

    def get(self, bookmark_id: UUID) -> Bookmark | None:
        row = self._conn.execute(self._select_sql() + " WHERE id = ?", (str(bookmark_id),)).fetchone()
        return self._row_to_bookmark(row) if row else None

    def list_for_playlist_media(
        self, playlist_id: UUID, media_id: UUID, *, include_global: bool = False
    ) -> list[Bookmark]:
        """Playlist-specific bookmarks, optionally combined with global ones (spec #184)."""
        if include_global:
            rows = self._conn.execute(
                self._select_sql() + " WHERE "
                "(media_id = ? AND playlist_id = ? AND scope = ?) OR "
                "(media_id = ? AND playlist_id IS NULL AND scope = ?) "
                "ORDER BY start_us",
                (
                    str(media_id),
                    str(playlist_id),
                    BookmarkScope.PLAYLIST_MEDIA.value,
                    str(media_id),
                    BookmarkScope.GLOBAL_MEDIA.value,
                ),
            ).fetchall()
        else:
            rows = self._conn.execute(
                self._select_sql() + " WHERE media_id = ? AND playlist_id = ? AND scope = ? "
                "ORDER BY start_us",
                (str(media_id), str(playlist_id), BookmarkScope.PLAYLIST_MEDIA.value),
            ).fetchall()
        return self._rows_to_bookmarks(rows)

    def list_for_context(self, playlist_id: UUID | None, media_id: UUID) -> list[Bookmark]:
        """The bookmarks shown for one song: that playlist's own bookmarks when a playlist
        context is active, the song's global bookmarks otherwise. (This decision was
        repeated inline in five places before.)"""
        if playlist_id is not None:
            return self.list_for_playlist_media(playlist_id, media_id)
        return self.list_global_for_media(media_id)

    def list_global_for_media(self, media_id: UUID) -> list[Bookmark]:
        rows = self._conn.execute(
            self._select_sql() + " WHERE media_id = ? AND scope = ? ORDER BY start_us",
            (str(media_id), BookmarkScope.GLOBAL_MEDIA.value),
        ).fetchall()
        return self._rows_to_bookmarks(rows)

    def list_for_playlist(self, playlist_id: UUID) -> list[Bookmark]:
        """Every playlist-scoped bookmark across every song in the playlist -- direct
        user request: "the bookmarks should all be listed for all songs", for the
        bookmark panel's cross-song view (the waveform itself still only ever shows
        the one song currently displayed, via list_for_playlist_media). Ordered by
        sort_index first so a manual reorder (see reorder()) sticks; falls back to
        start_us for anything never manually reordered (sort_index 0 for everyone).
        Global-media bookmarks aren't included -- they were never tied to this
        playlist to begin with.
        """
        rows = self._conn.execute(
            self._select_sql() + " WHERE playlist_id = ? AND scope = ? ORDER BY sort_index, start_us",
            (str(playlist_id), BookmarkScope.PLAYLIST_MEDIA.value),
        ).fetchall()
        return self._rows_to_bookmarks(rows)

    def reorder(self, ordered_bookmark_ids: list[UUID]) -> None:
        """Assigns sort_index = position for each id, in the given order -- direct
        user request for manual up/down bookmark reordering. Renumbers the WHOLE list
        passed in, not just the two rows being swapped, so ties from bookmarks that
        have never been manually reordered (all default sort_index=0) resolve into a
        real, stable order the first time this is called.
        """
        now = _now()
        for index, bookmark_id in enumerate(ordered_bookmark_ids):
            # updated_at moves only for rows whose position changed, so a sync carries
            # the new order without touching everything else.
            self._conn.execute(
                "UPDATE bookmarks SET sort_index = ?, updated_at = ? WHERE id = ? AND sort_index != ?",
                (index, now, str(bookmark_id), index),
            )
        self._conn.commit()

    def insert(self, bookmark: Bookmark) -> Bookmark:
        now = _now()
        self._conn.execute(
            "INSERT INTO bookmarks (id, playlist_id, media_id, scope, lane_id, "
            "bookmark_type, name, start_us, end_us, loop_enabled, repeat_count, "
            "loop_gap_ms, completion_action, color_key, notes, sort_index, "
            "fade_in_ms, fade_out_ms, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._bookmark_params(bookmark, now, now),
        )
        self._set_tags(bookmark.id, bookmark.tags)
        # Restoring a deleted bookmark (undo) must win over its own deletion in a sync.
        self._conn.execute("DELETE FROM bookmark_tombstones WHERE bookmark_id = ?", (str(bookmark.id),))
        self._conn.commit()
        return bookmark

    def list_all(self) -> list[Bookmark]:
        rows = self._conn.execute(self._select_sql() + " ORDER BY media_id, start_us").fetchall()
        return self._rows_to_bookmarks(rows)

    def updated_at_by_id(self) -> dict[str, str]:
        return {row[0]: row[1] for row in self._conn.execute("SELECT id, updated_at FROM bookmarks")}

    def tombstones(self) -> dict[str, str]:
        """bookmark id -> ISO time of deletion, for folder sync."""
        return {row[0]: row[1] for row in self._conn.execute("SELECT bookmark_id, deleted_at FROM bookmark_tombstones")}

    def update(self, bookmark: Bookmark) -> None:
        self._conn.execute(
            "UPDATE bookmarks SET playlist_id = ?, media_id = ?, scope = ?, lane_id = ?, "
            "bookmark_type = ?, name = ?, start_us = ?, end_us = ?, loop_enabled = ?, "
            "repeat_count = ?, loop_gap_ms = ?, completion_action = ?, color_key = ?, "
            "notes = ?, sort_index = ?, fade_in_ms = ?, fade_out_ms = ?, updated_at = ? WHERE id = ?",
            (
                str(bookmark.playlist_id) if bookmark.playlist_id else None,
                str(bookmark.media_id),
                bookmark.scope.value,
                str(bookmark.lane_id) if bookmark.lane_id else None,
                bookmark.bookmark_type.value,
                bookmark.name,
                bookmark.start_us,
                bookmark.end_us,
                int(bookmark.loop_enabled),
                bookmark.repeat_count,
                bookmark.loop_gap_ms,
                bookmark.completion_action.value,
                bookmark.color_key,
                bookmark.notes,
                bookmark.sort_index,
                bookmark.fade_in_ms,
                bookmark.fade_out_ms,
                _now(),
                str(bookmark.id),
            ),
        )
        self._set_tags(bookmark.id, bookmark.tags)
        self._conn.commit()

    def rename_legacy_default_names(self) -> int:
        """One-time backfill: gives every bookmark still carrying the old flat "New
        bookmark" default (created before default_bookmark_name() existed) a fresh
        <date>-<random>-<start>[-<end>] name instead (new bookmarks get one already;
        this covers the ones created earlier). Idempotent and cheap: after the first
        run, no row will match.
        Returns the number of rows renamed.
        """
        rows = self._conn.execute(
            "SELECT id, start_us, end_us FROM bookmarks WHERE name = ?", (LEGACY_DEFAULT_NAME,)
        ).fetchall()
        for bookmark_id, start_us, end_us in rows:
            self._conn.execute(
                "UPDATE bookmarks SET name = ? WHERE id = ?", (default_bookmark_name(start_us, end_us), bookmark_id)
            )
        if rows:
            self._conn.commit()
        return len(rows)

    def delete(self, bookmark_id: UUID) -> None:
        self._conn.execute("DELETE FROM bookmarks WHERE id = ?", (str(bookmark_id),))
        # A tombstone lets folder sync carry the deletion to the other machines.
        self._conn.execute(
            "INSERT INTO bookmark_tombstones (bookmark_id, deleted_at) VALUES (?, ?) "
            "ON CONFLICT(bookmark_id) DO UPDATE SET deleted_at = excluded.deleted_at",
            (str(bookmark_id), _now()),
        )
        self._conn.commit()

    def rename_playlist_references(self, playlist_id: UUID, new_name: str) -> None:
        """No-op placeholder: bookmarks reference playlist_id (UUID), not name (spec #15 note).

        Kept as an explicit method so callers renaming a playlist have a single place to
        look; unlike the GUI-side mapping strings in buzz2vlc, this schema stores a
        stable UUID foreign key, so renaming a playlist requires no bookmark rewrite.
        """
        return None

    def _set_tags(self, bookmark_id: UUID, tags: tuple[str, ...]) -> None:
        self._conn.execute("DELETE FROM bookmark_tags WHERE bookmark_id = ?", (str(bookmark_id),))
        self._conn.executemany(
            "INSERT INTO bookmark_tags (bookmark_id, tag) VALUES (?, ?)",
            [(str(bookmark_id), tag) for tag in tags],
        )

    def _get_tags(self, bookmark_id: UUID) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT tag FROM bookmark_tags WHERE bookmark_id = ? ORDER BY tag",
            (str(bookmark_id),),
        ).fetchall()
        return tuple(row[0] for row in rows)

    @staticmethod
    def _bookmark_params(bookmark: Bookmark, created_at: str, updated_at: str) -> tuple[Any, ...]:
        return (
            str(bookmark.id),
            str(bookmark.playlist_id) if bookmark.playlist_id else None,
            str(bookmark.media_id),
            bookmark.scope.value,
            str(bookmark.lane_id) if bookmark.lane_id else None,
            bookmark.bookmark_type.value,
            bookmark.name,
            bookmark.start_us,
            bookmark.end_us,
            int(bookmark.loop_enabled),
            bookmark.repeat_count,
            bookmark.loop_gap_ms,
            bookmark.completion_action.value,
            bookmark.color_key,
            bookmark.notes,
            bookmark.sort_index,
            bookmark.fade_in_ms,
            bookmark.fade_out_ms,
            created_at,
            updated_at,
        )

    @staticmethod
    def _select_sql() -> str:
        return (
            "SELECT id, playlist_id, media_id, scope, lane_id, bookmark_type, name, "
            "start_us, end_us, loop_enabled, repeat_count, loop_gap_ms, "
            "completion_action, color_key, notes, sort_index, fade_in_ms, fade_out_ms FROM bookmarks"
        )

    def _rows_to_bookmarks(self, rows: list[Any]) -> list[Bookmark]:
        """Builds many bookmarks with ONE tag query (chunked under SQLite's parameter
        limit) instead of one tag query per bookmark."""
        ids = [row[0] for row in rows]
        tags_by_id: dict[str, list[str]] = {bookmark_id: [] for bookmark_id in ids}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            for bookmark_id, tag in self._conn.execute(
                f"SELECT bookmark_id, tag FROM bookmark_tags WHERE bookmark_id IN ({placeholders}) "
                "ORDER BY bookmark_id, tag",
                chunk,
            ):
                tags_by_id[bookmark_id].append(tag)
        return [self._row_to_bookmark(row, tags=tuple(tags_by_id[row[0]])) for row in rows]

    def _row_to_bookmark(self, row: sqlite3.Row | tuple[Any, ...], *, tags: tuple[str, ...] | None = None) -> Bookmark:
        (
            bookmark_id,
            playlist_id,
            media_id,
            scope,
            lane_id,
            bookmark_type,
            name,
            start_us,
            end_us,
            loop_enabled,
            repeat_count,
            loop_gap_ms,
            completion_action,
            color_key,
            notes,
            sort_index,
            fade_in_ms,
            fade_out_ms,
        ) = row
        bid = UUID(bookmark_id)
        return Bookmark(
            id=bid,
            playlist_id=UUID(playlist_id) if playlist_id else None,
            media_id=UUID(media_id),
            scope=BookmarkScope(scope),
            lane_id=UUID(lane_id) if lane_id else None,
            bookmark_type=BookmarkType(bookmark_type),
            name=name,
            start_us=start_us,
            end_us=end_us,
            loop_enabled=bool(loop_enabled),
            repeat_count=repeat_count,
            loop_gap_ms=loop_gap_ms,
            completion_action=CompletionAction(completion_action),
            color_key=color_key,
            notes=notes,
            tags=tags if tags is not None else self._get_tags(bid),
            sort_index=sort_index,
            fade_in_ms=fade_in_ms,
            fade_out_ms=fade_out_ms,
        )
