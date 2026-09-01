from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest
from PySide6.QtGui import QUndoStack

from bookmark_studio.app.commands import (
    ChangeLoopCommand,
    CreateBookmarkCommand,
    DeleteBookmarkCommand,
    MoveBookmarkCommand,
    RenameBookmarkCommand,
    ResizeBookmarkCommand,
)
from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.domain.media import Media
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.migrations import migrate


@pytest.fixture()
def repo() -> BookmarkRepository:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    media = Media(
        id=uuid4(), canonical_uri=None, filename=None, title=None, artist=None,
        album=None, duration_us=None, file_size=None, mtime_ns=None, fast_fingerprint=None,
    )
    MediaRepository(conn).insert(media)
    repo = BookmarkRepository(conn)
    repo.media_id = media.id  # stash for test convenience
    return repo


def _bookmark(repo: BookmarkRepository, **overrides) -> Bookmark:
    defaults = dict(
        id=uuid4(), playlist_id=None, media_id=repo.media_id, scope=BookmarkScope.GLOBAL_MEDIA,
        lane_id=None, bookmark_type=BookmarkType.SEGMENT, name="Chorus", start_us=1000, end_us=2000,
        loop_enabled=False, repeat_count=None, loop_gap_ms=0, completion_action=CompletionAction.CONTINUE,
    )
    defaults.update(overrides)
    return Bookmark(**defaults)


def test_create_and_undo_removes_it(repo: BookmarkRepository) -> None:
    stack = QUndoStack()
    bookmark = _bookmark(repo)
    stack.push(CreateBookmarkCommand(repo, bookmark))
    assert repo.get(bookmark.id) is not None

    stack.undo()
    assert repo.get(bookmark.id) is None

    stack.redo()
    assert repo.get(bookmark.id) is not None


def test_delete_and_undo_restores_full_bookmark(repo: BookmarkRepository) -> None:
    bookmark = _bookmark(repo, tags=("solo", "practice"))
    repo.insert(bookmark)

    stack = QUndoStack()
    stack.push(DeleteBookmarkCommand(repo, bookmark))
    assert repo.get(bookmark.id) is None

    stack.undo()
    restored = repo.get(bookmark.id)
    assert restored is not None
    assert set(restored.tags) == {"solo", "practice"}  # repo returns tags in ORDER BY tag


def test_consecutive_move_commands_merge_into_one_undo_step(repo: BookmarkRepository) -> None:
    """spec #83: dragging continuously should compress to a single undo, not one per step."""
    bookmark = _bookmark(repo, start_us=0, end_us=1000)
    repo.insert(bookmark)
    stack = QUndoStack()

    # Simulate a drag as many incremental MoveBookmarkCommand pushes.
    positions = [(100, 1100), (200, 1200), (300, 1300), (400, 1400)]
    previous = (0, 1000)
    for start, end in positions:
        stack.push(MoveBookmarkCommand(repo, bookmark.id, previous[0], previous[1], start, end))
        previous = (start, end)

    assert repo.get(bookmark.id).start_us == 400
    assert stack.count() == 1  # all four merged into the first pushed command

    stack.undo()
    restored = repo.get(bookmark.id)
    assert (restored.start_us, restored.end_us) == (0, 1000)


def test_move_commands_for_different_bookmarks_do_not_merge(repo: BookmarkRepository) -> None:
    a = _bookmark(repo, start_us=0, end_us=1000)
    b = _bookmark(repo, start_us=5000, end_us=6000)
    repo.insert(a)
    repo.insert(b)
    stack = QUndoStack()

    stack.push(MoveBookmarkCommand(repo, a.id, 0, 1000, 100, 1100))
    stack.push(MoveBookmarkCommand(repo, b.id, 5000, 6000, 5100, 6100))

    assert stack.count() == 2
    stack.undo()  # undoes only b's move
    assert repo.get(a.id).start_us == 100
    assert repo.get(b.id).start_us == 5000


def test_resize_start_and_end_do_not_merge_with_each_other(repo: BookmarkRepository) -> None:
    bookmark = _bookmark(repo, start_us=0, end_us=1000)
    repo.insert(bookmark)
    stack = QUndoStack()

    stack.push(ResizeBookmarkCommand(repo, bookmark.id, "start", 0, 100))
    stack.push(ResizeBookmarkCommand(repo, bookmark.id, "end", 1000, 1500))
    assert stack.count() == 2
    assert repo.get(bookmark.id).start_us == 100
    assert repo.get(bookmark.id).end_us == 1500

    stack.undo()
    assert repo.get(bookmark.id).end_us == 1000
    assert repo.get(bookmark.id).start_us == 100  # the start-resize is untouched


def test_rename_undo_redo(repo: BookmarkRepository) -> None:
    bookmark = _bookmark(repo, name="Verse")
    repo.insert(bookmark)
    stack = QUndoStack()
    stack.push(RenameBookmarkCommand(repo, bookmark.id, "Verse", "Chorus"))
    assert repo.get(bookmark.id).name == "Chorus"
    stack.undo()
    assert repo.get(bookmark.id).name == "Verse"


def test_change_loop_undo_redo(repo: BookmarkRepository) -> None:
    bookmark = _bookmark(repo, loop_enabled=False, repeat_count=None, loop_gap_ms=0,
                          completion_action=CompletionAction.CONTINUE)
    repo.insert(bookmark)
    stack = QUndoStack()
    stack.push(
        ChangeLoopCommand(
            repo, bookmark.id,
            old=(False, None, 0, CompletionAction.CONTINUE),
            new=(True, 5, 250, CompletionAction.PAUSE),
        )
    )
    updated = repo.get(bookmark.id)
    assert updated.loop_enabled is True
    assert updated.repeat_count == 5
    assert updated.completion_action == CompletionAction.PAUSE

    stack.undo()
    reverted = repo.get(bookmark.id)
    assert reverted.loop_enabled is False
    assert reverted.completion_action == CompletionAction.CONTINUE
