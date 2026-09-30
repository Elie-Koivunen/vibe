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


def test_multi_select_enables_delete_but_not_play_loop(qtbot) -> None:
    """Direct user request: "i should be able to multiple select and delete or
    move". Play/Loop only ever act on one bookmark, so they must stay disabled with
    more than one row selected even though Delete becomes available."""
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    first = _bookmark(media_id, "First", 1_000_000, end_us=2_000_000, bookmark_type=BookmarkType.SEGMENT)
    second = _bookmark(media_id, "Second", 3_000_000)
    panel.set_bookmarks([first, second], {media_id: "Song"})

    panel.select_bookmarks({first.id, second.id})

    assert panel._delete_bookmark_button.isEnabled() is True
    assert panel._play_bookmark_button.isEnabled() is False
    assert panel._loop_bookmark_button.isEnabled() is False


def test_delete_button_emits_every_selected_id(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    first = _bookmark(media_id, "First", 1_000_000)
    second = _bookmark(media_id, "Second", 2_000_000)
    third = _bookmark(media_id, "Third", 3_000_000)
    panel.set_bookmarks([first, second, third], {media_id: "Song"})
    panel.select_bookmarks({first.id, third.id})

    requests = []
    panel.delete_bookmark_requested.connect(requests.append)
    panel._delete_bookmark_button.click()

    assert requests == [[first.id, third.id]]


def test_move_selected_block_up_keeps_relative_order(qtbot) -> None:
    """Moving a multi-row selection up/down must move the whole block together,
    preserving the selected rows' order relative to each other."""
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    a = _bookmark(media_id, "A", 1_000_000)
    b = _bookmark(media_id, "B", 2_000_000)
    c = _bookmark(media_id, "C", 3_000_000)
    d = _bookmark(media_id, "D", 4_000_000)
    panel.set_bookmarks([a, b, c, d], {media_id: "Song"})

    # Select B and C (indices 1, 2) and move the block up -- expect B, C to swap
    # above A: A, B, C, D -> B, C, A, D.
    panel.select_bookmarks({b.id, c.id})
    assert panel._move_up_button.isEnabled() is True

    requests = []
    panel.reorder_requested.connect(requests.append)
    panel._move_up_button.click()

    assert requests == [[b.id, c.id, a.id, d.id]]


def test_move_up_disabled_when_selection_already_at_top(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    a = _bookmark(media_id, "A", 1_000_000)
    b = _bookmark(media_id, "B", 2_000_000)
    panel.set_bookmarks([a, b], {media_id: "Song"})

    panel.select_bookmarks({a.id, b.id})  # both rows, including the very top one

    assert panel._move_up_button.isEnabled() is False
    assert panel._move_down_button.isEnabled() is False  # also includes the bottom row


def test_loop_fade_columns_show_labels_and_are_dropdown_editable(qtbot) -> None:
    """Direct follow-up request: "the columns for loop, fade in/out etc, they
    should also be directly editable, e.g. clicking would give a drop menu
    options" -- Loop/Fade In/Fade Out are backed by a combo-box delegate; editing
    any other column (e.g. Name) must not open one at all."""
    from bookmark_studio.ui.bookmark_panel import FADE_IN_COLUMN, FADE_OUT_COLUMN, LOOP_COLUMN

    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    bookmark = _bookmark(
        media_id, "Chorus", 1_000_000, end_us=2_000_000, bookmark_type=BookmarkType.SEGMENT,
        loop_enabled=True, repeat_count=3, fade_in_ms=500, fade_out_ms=0,
    )
    panel.set_bookmarks([bookmark], {media_id: "Song"})

    row = panel._tree.topLevelItem(0)
    assert row.text(LOOP_COLUMN) == "×3"
    assert row.text(FADE_IN_COLUMN) == "500 ms"
    assert row.text(FADE_OUT_COLUMN) == "Off"

    delegate = panel._tree.itemDelegate()
    assert delegate.createEditor(None, None, panel._tree.model().index(0, LOOP_COLUMN)) is not None
    assert delegate.createEditor(None, None, panel._tree.model().index(0, 1)) is None  # Name column


def test_editing_loop_column_emits_loop_edited(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    bookmark = _bookmark(
        media_id, "Chorus", 1_000_000, end_us=2_000_000, bookmark_type=BookmarkType.SEGMENT, loop_enabled=False,
    )
    panel.set_bookmarks([bookmark], {media_id: "Song"})

    requests = []
    panel.loop_edited.connect(lambda *args: requests.append(args))
    row = panel._tree.topLevelItem(0)
    from bookmark_studio.ui.bookmark_panel import LOOP_COLUMN

    row.setText(LOOP_COLUMN, "∞")  # simulates the combo delegate's setModelData commit

    assert requests == [(bookmark.id, True, None)]


def test_editing_loop_column_on_a_point_bookmark_is_rejected(qtbot) -> None:
    """A point bookmark has no end_us, so it has nothing to loop over -- an edit
    there must be rejected and the cell reverted, not silently accepted."""
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    bookmark = _bookmark(media_id, "Point", 1_000_000)  # bookmark_type=POINT by default, end_us=None
    panel.set_bookmarks([bookmark], {media_id: "Song"})

    requests = []
    panel.loop_edited.connect(lambda *args: requests.append(args))
    row = panel._tree.topLevelItem(0)
    from bookmark_studio.ui.bookmark_panel import LOOP_COLUMN

    row.setText(LOOP_COLUMN, "∞")

    assert requests == []
    assert row.text(LOOP_COLUMN) == "Off"


def test_editing_fade_columns_emits_fade_edited(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    bookmark = _bookmark(media_id, "Chorus", 1_000_000)
    panel.set_bookmarks([bookmark], {media_id: "Song"})

    fade_in_requests = []
    fade_out_requests = []
    panel.fade_in_edited.connect(lambda *args: fade_in_requests.append(args))
    panel.fade_out_edited.connect(lambda *args: fade_out_requests.append(args))

    row = panel._tree.topLevelItem(0)
    from bookmark_studio.ui.bookmark_panel import FADE_IN_COLUMN, FADE_OUT_COLUMN

    row.setText(FADE_IN_COLUMN, "1000 ms")
    row.setText(FADE_OUT_COLUMN, "250 ms")

    assert fade_in_requests == [(bookmark.id, 1000)]
    assert fade_out_requests == [(bookmark.id, 250)]


def test_editing_gap_column_emits_gap_edited(qtbot) -> None:
    """Direct follow-up request: "the selection drop box is still missing for
    loop/fade in/fade out/gap" -- Gap is a fourth dropdown-editable column,
    alongside Loop/Fade In/Fade Out."""
    from bookmark_studio.ui.bookmark_panel import GAP_COLUMN

    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    bookmark = _bookmark(media_id, "Chorus", 1_000_000)
    panel.set_bookmarks([bookmark], {media_id: "Song"})

    row = panel._tree.topLevelItem(0)
    assert row.text(GAP_COLUMN) == "Off"

    requests = []
    panel.gap_edited.connect(lambda *args: requests.append(args))
    row.setText(GAP_COLUMN, "500 ms")

    assert requests == [(bookmark.id, 500)]


def test_set_bookmarks_preserves_selection_across_refresh(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    bookmark = _bookmark(media_id, "Chorus", 1_000_000)
    panel.set_bookmarks([bookmark], {media_id: "Song"})
    panel.select_bookmark(bookmark.id)

    panel.set_bookmarks([bookmark], {media_id: "Song"})  # e.g. a refresh from a live poll

    assert panel._selected_bookmark_id() == bookmark.id
