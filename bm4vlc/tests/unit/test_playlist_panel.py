from __future__ import annotations

from bookmark_studio.playback.status import VlcPlaylistItem
from bookmark_studio.ui.playlist_panel import PlaylistPanel


def _panel(qtbot) -> PlaylistPanel:
    panel = PlaylistPanel()
    qtbot.addWidget(panel)
    panel.set_playlist(
        [
            VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0),
            VlcPlaylistItem(vlc_id=2, uri="file:///b.mp3", name="Song B", duration_s=20.0),
        ]
    )
    return panel


def test_set_connected_updates_label_above_launch_button(qtbot) -> None:
    """Direct follow-up request: "move the connection status to above the launch vlc
    button and enlarge the text" -- the indicator moved here from TransportBar."""
    panel = _panel(qtbot)
    assert panel._connection_label.text() == "● Offline"

    panel.set_connected(True)
    assert panel._connection_label.text() == "● Connected"

    panel.set_connected(False)
    assert panel._connection_label.text() == "● Offline"


def test_set_current_playing_does_not_recreate_tree_items(qtbot) -> None:
    """Regression, reported live as "i still cant automatically play a song by double
    clicking the tittle": set_current_playing() used to call the full _tree.clear() +
    rebuild on every ~400ms status poll, destroying and recreating every row object --
    including mid-gesture during a real double-click, whose two clicks must land on
    the SAME item within Qt's double-click interval. It must now update the existing
    item objects in place.
    """
    panel = _panel(qtbot)
    row_before = panel._tree.topLevelItem(0)

    panel.set_current_playing(1)

    row_after = panel._tree.topLevelItem(0)
    assert row_after is row_before  # same object, not recreated
    # Direct follow-up request: "instead of having a playing column, reflect in
    # traffic color by highlighting the song that is being played e.g. in green" --
    # the ▶-in-a-column marker is gone; the whole row's background is tinted instead.
    assert row_after.background(0).color().name() == "#8fd98f"
    other_row = panel._tree.topLevelItem(1)
    assert other_row.background(0).style().name == "NoBrush"


def test_set_current_playing_preserves_selection(qtbot) -> None:
    panel = _panel(qtbot)
    panel._tree.setCurrentItem(panel._tree.topLevelItem(1))
    assert panel._tree.selectedItems()

    panel.set_current_playing(1)

    assert panel._tree.selectedItems()
    assert panel._tree.selectedItems()[0].data(0, 32) == 2


def test_set_current_playing_is_a_noop_when_unchanged(qtbot) -> None:
    panel = _panel(qtbot)
    panel.set_current_playing(1)
    row = panel._tree.topLevelItem(0)

    panel.set_current_playing(1)  # same id again

    assert panel._tree.topLevelItem(0) is row


def test_set_playlist_is_a_noop_when_items_are_unchanged(qtbot) -> None:
    panel = _panel(qtbot)
    row_before = panel._tree.topLevelItem(0)

    panel.set_playlist(
        [
            VlcPlaylistItem(vlc_id=1, uri="file:///a.mp3", name="Song A", duration_s=10.0),
            VlcPlaylistItem(vlc_id=2, uri="file:///b.mp3", name="Song B", duration_s=20.0),
        ]
    )

    assert panel._tree.topLevelItem(0) is row_before


def test_select_item_highlights_the_matching_row_and_fires_item_selected(qtbot) -> None:
    """Direct user request: selecting a bookmark should "automatically select the
    song from the playlist above" -- Application drives this via select_item()."""
    panel = _panel(qtbot)
    selected = []
    panel.item_selected.connect(selected.append)

    panel.select_item(2)

    assert [item.data(0, 32) for item in panel._tree.selectedItems()] == [2]
    assert selected == [2]


def test_select_item_none_clears_selection(qtbot) -> None:
    panel = _panel(qtbot)
    panel.select_item(1)
    assert panel._tree.selectedItems()

    panel.select_item(None)

    assert panel._tree.selectedItems() == []
