"""LaneRepository: lanes table (spec #47, #72). Not enumerated among spec #79's listed
repository interfaces, but the `lanes` table and domain.Lane both exist, and bookmarks
carry an FK to it -- added for consistency with the rest of the persistence layer.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import UUID

from bookmark_studio.domain.lane import Lane


class LaneRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def list_for_playlist(self, playlist_id: UUID) -> list[Lane]:
        rows = self._conn.execute(
            "SELECT id, playlist_id, name, order_index, visible, locked, color_key "
            "FROM lanes WHERE playlist_id = ? ORDER BY order_index",
            (str(playlist_id),),
        ).fetchall()
        return [self._row_to_lane(row) for row in rows]

    def insert(self, lane: Lane) -> Lane:
        self._conn.execute(
            "INSERT INTO lanes (id, playlist_id, name, order_index, visible, locked, "
            "color_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(lane.id),
                str(lane.playlist_id),
                lane.name,
                lane.order_index,
                int(lane.visible),
                int(lane.locked),
                lane.color_key,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        return lane

    def update(self, lane: Lane) -> None:
        self._conn.execute(
            "UPDATE lanes SET name = ?, order_index = ?, visible = ?, locked = ?, "
            "color_key = ? WHERE id = ?",
            (lane.name, lane.order_index, int(lane.visible), int(lane.locked), lane.color_key, str(lane.id)),
        )
        self._conn.commit()

    def delete(self, lane_id: UUID) -> None:
        self._conn.execute("DELETE FROM lanes WHERE id = ?", (str(lane_id),))
        self._conn.commit()

    @staticmethod
    def _row_to_lane(row: sqlite3.Row | tuple) -> Lane:
        lane_id, playlist_id, name, order_index, visible, locked, color_key = row
        return Lane(
            id=UUID(lane_id),
            playlist_id=UUID(playlist_id),
            name=name,
            order_index=order_index,
            visible=bool(visible),
            locked=bool(locked),
            color_key=color_key,
        )
