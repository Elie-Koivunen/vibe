from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Lane:
    """A named horizontal grouping of bookmarks, e.g. 'Song Structure' (spec #47)."""

    id: UUID
    playlist_id: UUID
    name: str
    order_index: int
    visible: bool = True
    locked: bool = False
    color_key: str | None = None
