"""Undo-able commands (QUndoCommand subclasses) per spec #82-#83.

Each mutating command re-reads the current row via the repository in redo()/undo()
rather than caching a stale copy, so commands stay correct even if something else
touched the same bookmark between push() and an undo/redo call.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

from PySide6.QtGui import QUndoCommand

from bookmark_studio.domain.bookmark import Bookmark, with_range
from bookmark_studio.domain.enums import CompletionAction
from bookmark_studio.logging.setup import get_logger
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository

_log = get_logger("BOOKMARK")

# Distinct per command *type* (spec #83): QUndoStack only attempts mergeWith() between
# consecutive commands whose id() matches, and mergeWith() itself still checks that
# they target the same bookmark before actually merging.
_ID_MOVE = 1
_ID_RESIZE = 2
_ID_LOOP = 3
_ID_FIELDS = 4


class _SafeCommand(QUndoCommand):
    """Base of every command. QUndoStack calls redo()/undo() from C++, and a Python
    exception escaping from there leaves PySide6 in a broken state that crashes the
    process a few calls later (seen with a bad value from a combo box). Errors are
    logged instead, and the app keeps running."""

    def redo(self) -> None:
        try:
            self._redo()
        except Exception:  # noqa: BLE001 - must not propagate into Qt
            _log.exception("%s failed", self.text())

    def undo(self) -> None:
        try:
            self._undo()
        except Exception:  # noqa: BLE001 - must not propagate into Qt
            _log.exception("undo of %s failed", self.text())

    def _redo(self) -> None:
        raise NotImplementedError

    def _undo(self) -> None:
        raise NotImplementedError


class CreateBookmarkCommand(_SafeCommand):
    def __init__(self, repository: BookmarkRepository, bookmark: Bookmark) -> None:
        super().__init__(f"Create bookmark '{bookmark.name}'")
        self._repository = repository
        self._bookmark = bookmark

    def _redo(self) -> None:
        self._repository.insert(self._bookmark)

    def _undo(self) -> None:
        self._repository.delete(self._bookmark.id)


class DeleteBookmarkCommand(_SafeCommand):
    def __init__(self, repository: BookmarkRepository, bookmark: Bookmark) -> None:
        super().__init__(f"Delete bookmark '{bookmark.name}'")
        self._repository = repository
        self._bookmark = bookmark  # full snapshot, needed to undo (re-insert)

    def _redo(self) -> None:
        self._repository.delete(self._bookmark.id)

    def _undo(self) -> None:
        self._repository.insert(self._bookmark)


class MoveBookmarkCommand(_SafeCommand):
    """Shifts both start and end by the same delta, preserving duration (spec #49)."""

    def __init__(
        self,
        repository: BookmarkRepository,
        bookmark_id: UUID,
        old_start_us: int,
        old_end_us: int | None,
        new_start_us: int,
        new_end_us: int | None,
    ) -> None:
        super().__init__("Move bookmark")
        self._repository = repository
        self._bookmark_id = bookmark_id
        self._old = (old_start_us, old_end_us)
        self._new = (new_start_us, new_end_us)

    def id(self) -> int:  # noqa: A003 - QUndoCommand's actual API name
        return _ID_MOVE

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802 - Qt override
        if not isinstance(other, MoveBookmarkCommand) or other._bookmark_id != self._bookmark_id:
            return False
        self._new = other._new
        return True

    def _redo(self) -> None:
        self._apply(self._new)

    def _undo(self) -> None:
        self._apply(self._old)

    def _apply(self, times: tuple[int, int | None]) -> None:
        bookmark = self._repository.get(self._bookmark_id)
        if bookmark is None:
            return
        self._repository.update(with_range(bookmark, times[0], times[1]))


class ResizeBookmarkCommand(_SafeCommand):
    """Changes exactly one boundary (spec #50): pass handle='start' or 'end'."""

    def __init__(
        self,
        repository: BookmarkRepository,
        bookmark_id: UUID,
        handle: str,
        old_value_us: int,
        new_value_us: int,
    ) -> None:
        if handle not in ("start", "end"):
            raise ValueError("handle must be 'start' or 'end'")
        super().__init__(f"Resize bookmark ({handle})")
        self._repository = repository
        self._bookmark_id = bookmark_id
        self._handle = handle
        self._old_value = old_value_us
        self._new_value = new_value_us

    def id(self) -> int:  # noqa: A003
        return _ID_RESIZE

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802
        if (
            not isinstance(other, ResizeBookmarkCommand)
            or other._bookmark_id != self._bookmark_id
            or other._handle != self._handle
        ):
            return False
        self._new_value = other._new_value
        return True

    def _redo(self) -> None:
        self._apply(self._new_value)

    def _undo(self) -> None:
        self._apply(self._old_value)

    def _apply(self, value_us: int) -> None:
        bookmark = self._repository.get(self._bookmark_id)
        if bookmark is None:
            return
        if self._handle == "start":
            self._repository.update(with_range(bookmark, value_us, bookmark.end_us))
        else:
            self._repository.update(with_range(bookmark, bookmark.start_us, value_us))


class RenameBookmarkCommand(_SafeCommand):
    def __init__(self, repository: BookmarkRepository, bookmark_id: UUID, old_name: str, new_name: str) -> None:
        super().__init__(f"Rename bookmark to '{new_name}'")
        self._repository = repository
        self._bookmark_id = bookmark_id
        self._old_name = old_name
        self._new_name = new_name

    def _redo(self) -> None:
        self._apply(self._new_name)

    def _undo(self) -> None:
        self._apply(self._old_name)

    def _apply(self, name: str) -> None:
        bookmark = self._repository.get(self._bookmark_id)
        if bookmark is None:
            return
        self._repository.update(replace(bookmark, name=name))


class ChangeLoopCommand(_SafeCommand):
    def __init__(
        self,
        repository: BookmarkRepository,
        bookmark_id: UUID,
        *,
        old: tuple[bool, int | None, int, CompletionAction, int, int],
        new: tuple[bool, int | None, int, CompletionAction, int, int],
    ) -> None:
        super().__init__("Change loop settings")
        self._repository = repository
        self._bookmark_id = bookmark_id
        self._old = old
        self._new = new

    def _redo(self) -> None:
        self._apply(self._new)

    def _undo(self) -> None:
        self._apply(self._old)

    def id(self) -> int:  # noqa: A003
        return _ID_LOOP

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802
        """Consecutive loop-setting edits to one bookmark (e.g. every arrow-key step in
        the Gap spinbox) collapse into a single undo step."""
        if not isinstance(other, ChangeLoopCommand) or other._bookmark_id != self._bookmark_id:
            return False
        self._new = other._new
        return True

    def _apply(self, values: tuple[bool, int | None, int, CompletionAction, int, int]) -> None:
        bookmark = self._repository.get(self._bookmark_id)
        if bookmark is None:
            return
        loop_enabled, repeat_count, loop_gap_ms, completion_action, fade_in_ms, fade_out_ms = values
        self._repository.update(
            replace(
                bookmark,
                loop_enabled=loop_enabled,
                repeat_count=repeat_count,
                loop_gap_ms=loop_gap_ms,
                completion_action=completion_action,
                fade_in_ms=fade_in_ms,
                fade_out_ms=fade_out_ms,
            )
        )


class EditBookmarkFieldsCommand(_SafeCommand):
    """Sets arbitrary Bookmark fields (e.g. tags, notes). `old`/`new` map field name ->
    value. Consecutive edits of the same fields on the same bookmark merge into one
    undo step (typing notes shouldn't create one step per commit)."""

    def __init__(self, repository: BookmarkRepository, bookmark_id: UUID, text: str,
                 *, old: dict[str, object], new: dict[str, object]) -> None:
        super().__init__(text)
        if set(old) != set(new):
            raise ValueError("old and new must name the same fields")
        self._repository = repository
        self._bookmark_id = bookmark_id
        self._old = dict(old)
        self._new = dict(new)

    def id(self) -> int:  # noqa: A003
        return _ID_FIELDS

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802
        if (
            not isinstance(other, EditBookmarkFieldsCommand)
            or other._bookmark_id != self._bookmark_id
            or set(other._new) != set(self._new)
        ):
            return False
        self._new = dict(other._new)
        return True

    def _redo(self) -> None:
        self._apply(self._new)

    def _undo(self) -> None:
        self._apply(self._old)

    def _apply(self, values: dict[str, Any]) -> None:
        bookmark = self._repository.get(self._bookmark_id)
        if bookmark is None:
            return
        self._repository.update(replace(bookmark, **values))


class MoveBookmarkLaneCommand(_SafeCommand):
    def __init__(
        self, repository: BookmarkRepository, bookmark_id: UUID, old_lane_id: UUID | None, new_lane_id: UUID | None
    ) -> None:
        super().__init__("Move bookmark to lane")
        self._repository = repository
        self._bookmark_id = bookmark_id
        self._old_lane_id = old_lane_id
        self._new_lane_id = new_lane_id

    def _redo(self) -> None:
        self._apply(self._new_lane_id)

    def _undo(self) -> None:
        self._apply(self._old_lane_id)

    def _apply(self, lane_id: UUID | None) -> None:
        bookmark = self._repository.get(self._bookmark_id)
        if bookmark is None:
            return
        self._repository.update(replace(bookmark, lane_id=lane_id))
