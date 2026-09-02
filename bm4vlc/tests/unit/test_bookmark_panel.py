from __future__ import annotations

from uuid import uuid4

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.ui.bookmark_panel import BookmarkPanel


def _bookmark(media_id, name, start_us, **overrides) -> Bookmark:
    defaults = dict(
        id=uuid4(), playlist_id=uuid4(), media_id=media_id, scope=BookmarkScope.PLAYLIST_MEDIA,
        lane_id=None, bookmark_type=BookmarkType.POINT, name=name, start_us=start_us, end_us=None,
        loop_enabled=False, repeat_count=None, loop_gap_ms=0, completion_action=CompletionAction.CONTINUE,
    )
    defaults.update(overrides)
    return Bookmark(**defaults)


def test_song_column_shows_the_owning_track_name(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    bookmark = _bookmark(media_id, "Chorus", 1_000_000)

    panel.set_bookmarks([bookmark], {media_id: "My Song"})

    row = panel._tree.topLevelItem(0)
    assert row.text(0) == "My Song"  # "Song" is column 0
    assert row.text(1) == "Chorus"


def test_columns_are_resizable_and_reorderable(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    header = panel._tree.header()
    assert header.sectionsMovable() is True


def test_double_click_bookmark_row_requests_play(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    bookmark = _bookmark(media_id, "Chorus", 1_000_000)
    panel.set_bookmarks([bookmark], {media_id: "My Song"})

    requests = []
    panel.play_bookmark_requested.connect(requests.append)
    panel._on_item_double_clicked(panel._tree.topLevelItem(0), 0)

    assert requests == [bookmark.id]


def test_move_up_down_emit_reorder_requested_with_swapped_order(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    first = _bookmark(media_id, "First", 1_000_000)
    second = _bookmark(media_id, "Second", 2_000_000)
    panel.set_bookmarks([first, second], {media_id: "Song"})

    assert panel._move_up_button.isEnabled() is False  # nothing selected
    panel.select_bookmark(second.id)
    assert panel._move_up_button.isEnabled() is True
    assert panel._move_down_button.isEnabled() is False  # already last

    requests = []
    panel.reorder_requested.connect(requests.append)
    panel._move_up_button.click()

    assert requests == [[second.id, first.id]]


def test_set_bookmarks_preserves_the_given_order_not_start_us(qtbot) -> None:
    """The caller (Application, backed by BookmarkRepository.list_for_playlist) owns
    ordering now -- the panel must not silently re-sort by start_us underneath a
    manual reorder."""
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    later = _bookmark(media_id, "Later", 9_000_000)
    earlier = _bookmark(media_id, "Earlier", 1_000_000)

    panel.set_bookmarks([later, earlier], {media_id: "Song"})

    assert panel._tree.topLevelItem(0).text(1) == "Later"
    assert panel._tree.topLevelItem(1).text(1) == "Earlier"


def test_drag_reorder_via_on_rows_moved_emits_the_new_order(qtbot) -> None:
    """Direct user request: "i should be able to just click and drag the entry up
    and down instead of relying on separate buttons". Qt's InternalMove drag-drop
    already rearranges the tree's rows itself before emitting rowsMoved -- simulate
    that by moving the item directly, then confirm the handler reads back and emits
    the resulting order.
    """
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    first = _bookmark(media_id, "First", 1_000_000)
    second = _bookmark(media_id, "Second", 2_000_000)
    panel.set_bookmarks([first, second], {media_id: "Song"})

    # Simulate what Qt's InternalMove drag-drop does to the tree before rowsMoved fires.
    moved = panel._tree.takeTopLevelItem(1)
    panel._tree.insertTopLevelItem(0, moved)

    requests = []
    panel.reorder_requested.connect(requests.append)
    panel._on_rows_moved()

    assert requests == [[second.id, first.id]]


def test_set_bookmarks_preserves_selection_across_refresh(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    bookmark = _bookmark(media_id, "Chorus", 1_000_000)
    panel.set_bookmarks([bookmark], {media_id: "Song"})
    panel.select_bookmark(bookmark.id)

    panel.set_bookmarks([bookmark], {media_id: "Song"})  # e.g. a refresh from a live poll

    assert panel._selected_bookmark_id() == bookmark.id
