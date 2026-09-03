from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from bookmark_studio.ui.transport import TransportBar


def test_transport_starts_disconnected_and_disabled(qtbot) -> None:
    transport = TransportBar()
    qtbot.addWidget(transport)
    assert transport.play_pause_button.isEnabled() is False
    assert transport.previous_bookmark_button.isEnabled() is True  # not gated by connection


def test_set_transport_enabled_true_enables_playback_buttons(qtbot) -> None:
    """The connection indicator itself now lives in PlaylistPanel, right above the
    Launch VLC button (direct follow-up request: "move the connection status to
    above the launch vlc button") -- see test_playlist_panel.py for that. This is
    just the button-enable half, still owned by TransportBar."""
    transport = TransportBar()
    qtbot.addWidget(transport)

    transport.set_transport_enabled(True)
    assert transport.play_pause_button.isEnabled() is True
    assert transport.stop_button.isEnabled() is True

    transport.set_transport_enabled(False)
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

    # TimecodeEdit is a row of numeric spin boxes now, not a blankable text field --
    # with nothing loaded it just shows the zero value, disabled/greyed out, same
    # convention as any other disabled QSpinBox in this app.
    assert transport._bookmark_start_edit.text() == "00:00:00.000"
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
    assert transport._bookmark_end_edit.text() == "00:00:00.000"
    assert transport._bookmark_end_edit.isEnabled() is False

    transport.set_bookmark_times(None, None)
    assert transport._bookmark_start_edit.text() == "00:00:00.000"
    assert transport._bookmark_start_edit.isEnabled() is False


def test_editing_bookmark_start_end_fields_emits_commit_signals(qtbot) -> None:
    """TimecodeEdit is a composite of four per-unit spin boxes (HH/MM/SS/mmm), each
    its own QLineEdit-shaped sub-widget -- typing into one and pressing Enter (or
    losing focus) commits, same as any other field in this app."""
    transport = TransportBar()
    qtbot.addWidget(transport)
    transport.show()
    transport.set_bookmark_times(5_000_000, 10_000_000)  # "00:00:05.000" / "00:00:10.000"

    start_requests = []
    end_requests = []
    transport.bookmark_start_committed.connect(start_requests.append)
    transport.bookmark_end_committed.connect(end_requests.append)

    start_seconds = transport._bookmark_start_edit._seconds
    start_seconds.setFocus()
    start_seconds.selectAll()
    QTest.keyClicks(start_seconds, "06")
    QTest.keyClick(start_seconds, Qt.Key_Return)
    assert start_requests == [6_000_000]

    end_seconds = transport._bookmark_end_edit._seconds
    end_seconds.setFocus()
    end_seconds.selectAll()
    QTest.keyClicks(end_seconds, "12")
    QTest.keyClick(end_seconds, Qt.Key_Return)
    assert end_requests == [12_000_000]


def test_bookmark_field_each_unit_has_its_own_arrow_buttons(qtbot) -> None:
    """Direct follow-up request: "i would assume that each time unit has its own
    control arrows, just as in audacity" -- TimecodeEdit is a row of four real
    QSpinBoxes (hours/minutes/seconds/millis), each with its own spinner arrows
    (and Up/Down while focused), rather than one shared pair that stepped whichever
    section the text cursor happened to be sitting in. Each step commits
    immediately (no separate Enter needed)."""
    transport = TransportBar()
    qtbot.addWidget(transport)
    transport.show()
    transport.set_bookmark_times(5_000_000, 10_000_000)  # "00:00:05.000"

    start_requests = []
    transport.bookmark_start_committed.connect(start_requests.append)

    edit = transport._bookmark_start_edit

    edit._seconds.setFocus()
    QTest.keyClick(edit._seconds, Qt.Key_Up)
    assert edit.text() == "00:00:06.000"
    assert start_requests[-1] == 6_000_000

    edit._hours.setFocus()
    QTest.keyClick(edit._hours, Qt.Key_Up)
    assert edit.text() == "01:00:06.000"
    assert start_requests[-1] == 3_606_000_000

    edit._minutes.setFocus()
    QTest.keyClick(edit._minutes, Qt.Key_Down)
    assert edit.text() == "00:59:06.000"
    assert start_requests[-1] == 3_546_000_000


def test_bookmark_field_arrow_key_does_not_go_negative(qtbot) -> None:
    """A unit stepping below its own range (e.g. seconds going below 0 from
    00:00:00) must clamp the WHOLE value at zero, not wrap+carry into the higher
    units (which would silently jump to something like 00:59:59)."""
    transport = TransportBar()
    qtbot.addWidget(transport)
    transport.show()
    transport.set_bookmark_times(500_000, None)  # 0.5s -- one second-step would go negative

    edit = transport._bookmark_start_edit
    edit._seconds.setFocus()
    QTest.keyClick(edit._seconds, Qt.Key_Down)

    assert edit.text() == "00:00:00.000"
