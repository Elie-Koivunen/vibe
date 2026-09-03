from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from bookmark_studio.ui.transport import TransportBar


def test_transport_starts_disconnected_and_disabled(qtbot) -> None:
    transport = TransportBar()
    qtbot.addWidget(transport)
    assert transport._connection_label.text() == "● Offline"
    assert transport.play_pause_button.isEnabled() is False
    assert transport.previous_bookmark_button.isEnabled() is True  # not gated by connection


def test_set_connected_true_enables_transport_and_updates_label(qtbot) -> None:
    transport = TransportBar()
    qtbot.addWidget(transport)

    transport.set_connected(True)
    assert transport._connection_label.text() == "● Connected"
    assert transport.play_pause_button.isEnabled() is True
    assert transport.stop_button.isEnabled() is True

    transport.set_connected(False)
    assert transport._connection_label.text() == "● Offline"
    assert transport.play_pause_button.isEnabled() is False


def test_set_time_updates_position_and_duration(qtbot) -> None:
    """Direct follow-up request: "the song start end doesnt need to be editable,
    only the bookmark fields" -- position/duration are a plain read-only display."""
    transport = TransportBar()
    qtbot.addWidget(transport)

    transport.set_time(7_000_000, 30_000_000)

    assert transport._position_label.text() == "00:00:07.000"
    assert transport._duration_label.text() == "/ 00:00:30.000"


def test_set_bookmark_times_updates_and_disables_appropriately(qtbot) -> None:
    """Direct follow-up request: "the fields should reflect the bookmark start time
    and end time, a different timer should be adjacent for the duration of the whole
    song" -- added a mirrored, editable Start/End pair for the currently selected
    bookmark, next to the existing live position/duration."""
    transport = TransportBar()
    qtbot.addWidget(transport)

    assert transport._bookmark_start_edit.text() == ""
    assert transport._bookmark_start_edit.isEnabled() is False
    assert transport._bookmark_end_edit.isEnabled() is False

    transport.set_bookmark_times(5_000_000, 10_000_000)
    assert transport._bookmark_start_edit.text() == "00:00:05.000"
    assert transport._bookmark_end_edit.text() == "00:00:10.000"
    assert transport._bookmark_start_edit.isEnabled() is True
    assert transport._bookmark_end_edit.isEnabled() is True

    # A point bookmark has a start but no end.
    transport.set_bookmark_times(2_000_000, None)
    assert transport._bookmark_start_edit.text() == "00:00:02.000"
    assert transport._bookmark_end_edit.text() == ""
    assert transport._bookmark_end_edit.isEnabled() is False

    transport.set_bookmark_times(None, None)
    assert transport._bookmark_start_edit.text() == ""
    assert transport._bookmark_start_edit.isEnabled() is False


def test_editing_bookmark_start_end_fields_emits_commit_signals(qtbot) -> None:
    transport = TransportBar()
    qtbot.addWidget(transport)
    transport.show()
    transport.set_bookmark_times(5_000_000, 10_000_000)

    start_requests = []
    end_requests = []
    transport.bookmark_start_committed.connect(start_requests.append)
    transport.bookmark_end_committed.connect(end_requests.append)

    start_edit = transport._bookmark_start_edit
    start_edit.setFocus()
    start_edit.selectAll()
    QTest.keyClicks(start_edit, "00:00:06.000")
    QTest.keyClick(start_edit, Qt.Key_Return)
    assert start_requests == [6_000_000]

    end_edit = transport._bookmark_end_edit
    end_edit.setFocus()
    end_edit.selectAll()
    QTest.keyClicks(end_edit, "00:00:12.000")
    QTest.keyClick(end_edit, Qt.Key_Return)
    assert end_requests == [12_000_000]


def test_bookmark_field_arrow_keys_step_the_value(qtbot) -> None:
    """Direct user request: "the bookmark fields can also have arrow keys to
    increment the numerals" -- plain Up/Down step by 100ms, Shift+Up/Down by 1s, and
    an arrow press commits immediately (no separate Enter needed)."""
    transport = TransportBar()
    qtbot.addWidget(transport)
    transport.show()
    transport.set_bookmark_times(5_000_000, 10_000_000)

    start_requests = []
    transport.bookmark_start_committed.connect(start_requests.append)

    edit = transport._bookmark_start_edit
    edit.setFocus()

    QTest.keyClick(edit, Qt.Key_Up)
    assert edit.text() == "00:00:05.100"
    assert start_requests[-1] == 5_100_000

    QTest.keyClick(edit, Qt.Key_Down)
    QTest.keyClick(edit, Qt.Key_Down)
    assert edit.text() == "00:00:04.900"
    assert start_requests[-1] == 4_900_000

    QTest.keyClick(edit, Qt.Key_Up, Qt.ShiftModifier)
    assert edit.text() == "00:00:05.900"
    assert start_requests[-1] == 5_900_000


def test_bookmark_field_arrow_key_does_not_go_negative(qtbot) -> None:
    transport = TransportBar()
    qtbot.addWidget(transport)
    transport.show()
    transport.set_bookmark_times(50_000, None)  # 0.05s -- one coarse step would go negative

    edit = transport._bookmark_start_edit
    edit.setFocus()
    QTest.keyClick(edit, Qt.Key_Down, Qt.ShiftModifier)

    assert edit.text() == "00:00:00.000"
