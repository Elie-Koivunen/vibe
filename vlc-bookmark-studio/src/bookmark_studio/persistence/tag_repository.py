"""The tag catalog (0.7.0): the tags the Bookmark tab offers, and the rules that keep every
bookmark consistent with it.

* Renaming a tag renames it on every bookmark (any song, any playlist); renaming it to a
  tag that already exists merges the two. Removing a tag takes it off every bookmark.
  Those bookmarks count as changed, so folder sync carries the change to other machines.
* ``tag_changes`` remembers each rename (old -> new) and removal (old -> NULL). Every tag
  written to a bookmark goes through :meth:`TagRepository.canonical`, which follows those
  records -- so a stale name arriving later (undo of an older edit, a sync file from a
  machine that hasn't heard of the rename yet, an old project file) becomes the current
  name, or disappears if the tag was removed, instead of coming back.
* A tag the catalog doesn't know (from an import or a sync) is added to it, so no tag a
  bookmark carries is ever missing from the list.
* Names are matched without regard to case; the catalog's spelling wins.

The catalog and its changes travel in sync and project files (``tags.json``); see
:meth:`export_state` and :meth:`merge_state`.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

MAX_TAG_LENGTH = 40
_MAX_CHAIN = 32  # rename chains are collapsed when recorded; this only guards against loops


class InvalidTagName(ValueError):
    """The message is shown to the user as it is."""


def clean_tag_name(name: str) -> str:
    cleaned = " ".join(str(name).split())
    if not cleaned:
        raise InvalidTagName("A tag needs a name.")
    if "," in cleaned:
        raise InvalidTagName("A tag can't contain a comma.")
    if len(cleaned) > MAX_TAG_LENGTH:
        raise InvalidTagName(f"A tag can be at most {MAX_TAG_LENGTH} characters long.")
    return cleaned


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TagRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- reading --

    def names(self) -> list[str]:
        rows = self._conn.execute("SELECT name FROM tag_catalog ORDER BY sort_index, name COLLATE NOCASE")
        return [row[0] for row in rows]

    def usage_counts(self) -> dict[str, int]:
        """Lower-cased tag -> number of bookmarks carrying it."""
        counts: dict[str, int] = {}
        for tag, count in self._conn.execute("SELECT tag, COUNT(*) FROM bookmark_tags GROUP BY tag"):
            counts[tag.casefold()] = counts.get(tag.casefold(), 0) + count
        return counts

    def _catalog_spelling(self, name: str) -> str | None:
        row = self._conn.execute("SELECT name FROM tag_catalog WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    # -- editing (the "Edit tags" window); each call is one transaction --

    def add(self, name: str) -> str:
        name = clean_tag_name(name)
        with self._conn:
            existing = self._catalog_spelling(name)
            if existing is not None:
                raise InvalidTagName(f"“{existing}” is already in the list.")
            self._insert(name, _now())
        return name

    def rename(self, old: str, new: str) -> int:
        """Renames `old` everywhere; into an existing tag, the two are merged. Returns the
        number of bookmarks changed."""
        new = clean_tag_name(new)
        with self._conn:
            old_spelling = self._catalog_spelling(old)
            if old_spelling is None:
                raise InvalidTagName(f"“{old}” is not in the list.")
            return self._apply_rename(old_spelling, new, _now(), touch_bookmarks=True)

    def remove(self, name: str) -> int:
        """Removes `name` from the list and from every bookmark. Returns the number of
        bookmarks changed."""
        with self._conn:
            spelling = self._catalog_spelling(name)
            if spelling is None:
                raise InvalidTagName(f"“{name}” is not in the list.")
            return self._apply_removal(spelling, _now(), touch_bookmarks=True)

    def move(self, name: str, delta: int) -> None:
        """Moves `name` up (negative) or down the list."""
        names = self.names()
        lowered = [n.casefold() for n in names]
        if name.casefold() not in lowered:
            return
        index = lowered.index(name.casefold())
        target = max(0, min(len(names) - 1, index + delta))
        if target == index:
            return
        names.insert(target, names.pop(index))
        with self._conn:
            self._conn.executemany(
                "UPDATE tag_catalog SET sort_index = ? WHERE name = ?", list(enumerate(names)),
            )

    # -- keeping bookmarks consistent (no commit: runs inside the caller's write) --

    def canonical(self, tags: Iterable[str]) -> tuple[str, ...]:
        """The tags as they should be stored: renamed ones under their current name,
        removed ones dropped, the catalog's spelling, no duplicates. Unknown tags join the
        catalog."""
        result: list[str] = []
        seen: set[str] = set()
        for raw in tags:
            try:
                name = clean_tag_name(raw)
            except InvalidTagName:
                continue
            resolved = self._resolve(name)
            if resolved is None:
                continue
            spelling = self._catalog_spelling(resolved)
            if spelling is None:
                self._insert(resolved, _now())
                spelling = resolved
            if spelling.casefold() not in seen:
                seen.add(spelling.casefold())
                result.append(spelling)
        return tuple(result)

    def _resolve(self, name: str) -> str | None:
        """Follows recorded renames to the current name; None if the tag was removed. A
        name in the catalog is current, whatever the records say."""
        for _ in range(_MAX_CHAIN):
            if self._catalog_spelling(name) is not None:
                return name
            row = self._conn.execute("SELECT new_name FROM tag_changes WHERE old_name = ?", (name,)).fetchone()
            if row is None:
                return name
            if row[0] is None:
                return None
            name = row[0]
        return name

    def _insert(self, name: str, stamp: str) -> None:
        position = self._conn.execute("SELECT COALESCE(MAX(sort_index), -1) + 1 FROM tag_catalog").fetchone()[0]
        self._conn.execute(
            "INSERT INTO tag_catalog (name, sort_index, updated_at) VALUES (?, ?, ?)", (name, position, stamp),
        )
        # A name that is in use again is no longer a stale one.
        self._conn.execute("DELETE FROM tag_changes WHERE old_name = ?", (name,))

    def _bookmarks_with(self, name: str) -> list[str]:
        rows = self._conn.execute("SELECT DISTINCT bookmark_id FROM bookmark_tags WHERE tag = ? COLLATE NOCASE", (name,))
        return [row[0] for row in rows]

    def _touch(self, bookmark_ids: list[str], stamp: str) -> None:
        self._conn.executemany("UPDATE bookmarks SET updated_at = ? WHERE id = ?", [(stamp, b) for b in bookmark_ids])

    def _apply_rename(self, old: str, new: str, stamp: str, *, touch_bookmarks: bool) -> int:
        target = self._catalog_spelling(new)
        same_tag = new.casefold() == old.casefold()
        if same_tag:  # a new spelling of the same tag
            self._conn.execute("UPDATE tag_catalog SET name = ?, updated_at = ? WHERE name = ?", (new, stamp, old))
            target = new
        elif target is None:
            self._conn.execute("UPDATE tag_catalog SET name = ?, updated_at = ? WHERE name = ?", (new, stamp, old))
            target = new
        else:  # merge into the existing tag
            self._conn.execute("DELETE FROM tag_catalog WHERE name = ?", (old,))
        affected = self._bookmarks_with(old)
        for bookmark_id in affected:
            self._conn.execute(
                "DELETE FROM bookmark_tags WHERE bookmark_id = ? AND (tag = ? COLLATE NOCASE OR tag = ? COLLATE NOCASE)",
                (bookmark_id, old, target),
            )
            self._conn.execute("INSERT INTO bookmark_tags (bookmark_id, tag) VALUES (?, ?)", (bookmark_id, target))
        if touch_bookmarks:
            self._touch(affected, stamp)
        if not same_tag:
            self._record_change(old, target, stamp)
        return len(affected)

    def _apply_removal(self, name: str, stamp: str, *, touch_bookmarks: bool) -> int:
        self._conn.execute("DELETE FROM tag_catalog WHERE name = ?", (name,))
        affected = self._bookmarks_with(name)
        self._conn.execute("DELETE FROM bookmark_tags WHERE tag = ? COLLATE NOCASE", (name,))
        if touch_bookmarks:
            self._touch(affected, stamp)
        self._record_change(name, None, stamp)
        return len(affected)

    def _record_change(self, old: str, new: str | None, stamp: str) -> None:
        self._conn.execute(
            "INSERT INTO tag_changes (old_name, new_name, changed_at) VALUES (?, ?, ?) "
            "ON CONFLICT(old_name) DO UPDATE SET new_name = excluded.new_name, changed_at = excluded.changed_at",
            (old, new, stamp),
        )
        # Collapse chains (a -> b, then b -> c: a -> c), and the new name is live again.
        self._conn.execute(
            "UPDATE tag_changes SET new_name = ?, changed_at = ? WHERE new_name = ? COLLATE NOCASE",
            (new, stamp, old),
        )
        if new is not None:
            self._conn.execute("DELETE FROM tag_changes WHERE old_name = ?", (new,))

    # -- sync / project files --

    def export_state(self) -> dict[str, Any]:
        catalog = [
            {"name": name, "sort_index": index, "updated_at": stamp}
            for name, index, stamp in self._conn.execute(
                "SELECT name, sort_index, updated_at FROM tag_catalog ORDER BY sort_index, name COLLATE NOCASE"
            )
        ]
        changes = [
            {"old_name": old, "new_name": new, "changed_at": stamp}
            for old, new, stamp in self._conn.execute(
                "SELECT old_name, new_name, changed_at FROM tag_changes ORDER BY old_name COLLATE NOCASE"
            )
        ]
        return {"catalog": catalog, "changes": changes}

    def merge_state(self, state: dict[str, Any]) -> None:
        """Merges another installation's catalog (no commit: part of an import). The most
        recent decision about a name wins: a rename or removal made there after anything
        done here is applied here too (to the catalog and the bookmarks), and a tag added
        there joins the list unless it was renamed or removed here later."""
        changes = sorted(
            (c for c in state.get("changes", []) if isinstance(c, dict) and c.get("old_name")),
            key=lambda c: str(c.get("changed_at", "")),
        )
        for change in changes:
            try:
                old = clean_tag_name(change["old_name"])
                new = clean_tag_name(change["new_name"]) if change.get("new_name") else None
            except InvalidTagName:
                continue
            stamp = str(change.get("changed_at", ""))
            local = self._conn.execute("SELECT changed_at FROM tag_changes WHERE old_name = ?", (old,)).fetchone()
            if local is not None and local[0] >= stamp:
                continue
            spelling = self._catalog_spelling(old)
            if spelling is not None:
                added_here = self._conn.execute(
                    "SELECT updated_at FROM tag_catalog WHERE name = ?", (spelling,)
                ).fetchone()[0]
                if added_here > stamp:
                    continue  # (re)added here after it was changed there
            if new is None:
                if spelling is not None or self._bookmarks_with(old):
                    self._apply_removal(spelling or old, stamp, touch_bookmarks=False)
                else:
                    self._record_change(old, None, stamp)
            elif spelling is not None or self._bookmarks_with(old):
                if spelling is None:  # only on bookmarks: let the rename find it in the catalog
                    self._insert(old, stamp)
                    spelling = old
                self._apply_rename(spelling, new, stamp, touch_bookmarks=False)
            else:
                self._record_change(old, new, stamp)
        for entry in state.get("catalog", []):
            if not isinstance(entry, dict):
                continue
            try:
                name = clean_tag_name(entry.get("name", ""))
            except InvalidTagName:
                continue
            stamp = str(entry.get("updated_at", ""))
            if self._catalog_spelling(name) is not None:
                continue
            local = self._conn.execute("SELECT changed_at FROM tag_changes WHERE old_name = ?", (name,)).fetchone()
            if local is not None and local[0] >= stamp:
                continue  # renamed or removed here after it was added there
            self._insert(name, stamp)


__all__ = ["MAX_TAG_LENGTH", "InvalidTagName", "TagRepository", "clean_tag_name"]
