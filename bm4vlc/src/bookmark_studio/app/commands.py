"""Undo-able commands (QUndoCommand subclasses) per spec #82-#83.

Each mutating command re-reads the current row via the repository in redo()/undo()
rather than caching a stale copy, so commands stay correct even if something else
touched the same bookmark between push() and an undo/redo call.
"""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from PySide6.QtGui import QUndoCommand

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import CompletionAction
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository

# Distinct per command *type* (spec #83): QUndoStack only attempts mergeWith() between
# consecutive commands whose id() matches, and mergeWith() itself still checks that
# they target the same bookmark before actually merging.
_ID_MOVE = 1
_ID_RESIZE = 2


class CreateBookmarkCommand(QUndoCommand):
    def __init__(self, repository: BookmarkRepository, bookmark: Bookmark) -> None:
        super().__init__(f"Create bookmark '{bookmark.name}'")
        self._repository = repository
        self._bookmark = bookmark

    def redo(self) -> None:
        self._repository.insert(self._bookmark)

    def undo(self) -> None:
        self._repository.delete(self._bookmark.id)


class DeleteBookmarkCommand(QUndoCommand):
    def __init__(self, repository: BookmarkRepository, bookmark: Bookmark) -> None:
        super().__init__(f"Delete bookmark '{bookmark.name}'")
        self._repository = repository
        self._bookmark = bookmark  # full snapshot, needed to undo (re-insert)

    def redo(self) -> None:
        self._repository.delete(self._bookmark.id)

    def undo(self) -> None:
        self._repository.insert(self._bookmark)


class MoveBookmarkCommand(QUndoCommand):
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

    def redo(self) -> None:
        self._apply(self._new)

    def undo(self) -> None:
        self._apply(self._old)

    def _apply(self, times: tuple[int, int | None]) -> None:
        bookmark = self._repository.get(self._bookmark_id)
        if bookmark is None:
            return
        self._repository.update(replace(bookmark, start_us=times[0], end_us=times[1]))


class ResizeBookmarkCommand(QUndoCommand):
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

    def redo(self) -> None:
        self._apply(self._new_value)

    def undo(self) -> None:
        self._apply(self._old_value)

    def _apply(self, value_us: int) -> None:
        bookmark = self._repository.get(self._bookmark_id)
        if bookmark is None:
            return
        field = "start_us" if self._handle == "start" else "end_us"
        self._repository.update(replace(bookmark, **{field: value_us}))


class RenameBookmarkCommand(QUndoCommand):
    def __init__(self, repository: BookmarkRepository, bookmark_id: UUID, old_name: str, new_name: str) -> None:
        super().__init__(f"Rename bookmark to '{new_name}'")
        self._repository = repository
        self._bookmark_id = bookmark_id
        self._old_name = old_name
        self._new_name = new_name

    def redo(self) -> None:
        self._apply(self._new_name)

    def undo(self) -> None:
        self._apply(self._old_name)

    def _apply(self, name: str) -> None:
        bookmark = self._repository.get(self._bookmark_id)
        if bookmark is None:
            return
        self._repository.update(replace(bookmark, name=name))


class ChangeLoopCommand(QUndoCommand):
    def __init__(
        self,
        repository: BookmarkRepository,
        bookmark_id: UUID,
        *,
        old: tuple[bool, int | None, int, CompletionAction],
        new: tuple[bool, int | None, int, CompletionAction],
    ) -> None:
        super().__init__("Change loop settings")
        self._repository = repository
        self._bookmark_id = bookmark_id
        self._old = old
        self._new = new

    def redo(self) -> None:
        self._apply(self._new)

    def undo(self) -> None:
        self._apply(self._old)

    def _apply(self, values: tuple[bool, int | None, int, CompletionAction]) -> None:
        bookmark = self._repository.get(self._bookmark_id)
        if bookmark is None:
            return
        loop_enabled, repeat_count, loop_gap_ms, completion_action = values
        self._repository.update(
            replace(
                bookmark,
                loop_enabled=loop_enabled,
                repeat_count=repeat_count,
                loop_gap_ms=loop_gap_ms,
                completion_action=completion_action,
            )
        )


class MoveBookmarkLaneCommand(QUndoCommand):
    def __init__(
        self, repository: BookmarkRepository, bookmark_id: UUID, old_lane_id: UUID | None, new_lane_id: UUID | None
    ) -> None:
        super().__init__("Move bookmark to lane")
        self._repository = repository
        self._bookmark_id = bookmark_id
        self._old_lane_id = old_lane_id
        self._new_lane_id = new_lane_id

    def redo(self) -> None:
        self._apply(self._new_lane_id)

    def undo(self) -> None:
        self._apply(self._old_lane_id)

    def _apply(self, lane_id: UUID | None) -> None:
        bookmark = self._repository.get(self._bookmark_id)
        if bookmark is None:
            return
        self._repository.update(replace(bookmark, lane_id=lane_id))
