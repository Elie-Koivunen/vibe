from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from bookmark_studio.ui.transport import TimecodeEdit, TransportBar


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


def test_editing_timecode_unit_boxes_emits_editing_finished(qtbot) -> None:
    """TimecodeEdit is a composite of four per-unit spin boxes (HH/MM/SS/mmm), each
    its own QLineEdit-shaped sub-widget -- typing into one and pressing Enter (or
    losing focus) commits, same as any other field in this app. (The transport bar's
    own mirrored Bookmark Start/End fields that used to host this were removed per
    direct follow-up request -- "this is now duplicate, you can remove", since the
    Inspector's and the bookmark list's own Start/End already show the same thing --
    this now exercises TimecodeEdit directly instead.)
    """
    edit = TimecodeEdit()
    qtbot.addWidget(edit)
    edit.show()
    edit.setText("00:00:05.000")

    commits = []
    edit.editingFinished.connect(lambda: commits.append(edit.text()))

    edit._seconds.setFocus()
    edit._seconds.selectAll()
    QTest.keyClicks(edit._seconds, "06")
    QTest.keyClick(edit._seconds, Qt.Key_Return)
    assert commits == ["00:00:06.000"]


def test_timecode_edit_each_unit_has_its_own_arrow_buttons(qtbot) -> None:
    """Direct follow-up request: "i would assume that each time unit has its own
    control arrows, just as in audacity" -- TimecodeEdit is a row of four real
    QSpinBoxes (hours/minutes/seconds/millis), each with its own spinner arrows
    (and Up/Down while focused), rather than one shared pair that stepped whichever
    section the text cursor happened to be sitting in. Each step commits
    immediately (no separate Enter needed)."""
    edit = TimecodeEdit()
    qtbot.addWidget(edit)
    edit.show()
    edit.setText("00:00:05.000")

    commits = []
    edit.editingFinished.connect(lambda: commits.append(edit.text()))

    edit._seconds.setFocus()
    QTest.keyClick(edit._seconds, Qt.Key_Up)
    assert edit.text() == "00:00:06.000"
    assert commits[-1] == "00:00:06.000"

    edit._hours.setFocus()
    QTest.keyClick(edit._hours, Qt.Key_Up)
    assert edit.text() == "01:00:06.000"

    edit._minutes.setFocus()
    QTest.keyClick(edit._minutes, Qt.Key_Down)
    assert edit.text() == "00:59:06.000"


def test_timecode_edit_arrow_key_does_not_go_negative(qtbot) -> None:
    """A unit stepping below its own range (e.g. seconds going below 0 from
    00:00:00) must clamp the WHOLE value at zero, not wrap+carry into the higher
    units (which would silently jump to something like 00:59:59)."""
    edit = TimecodeEdit()
    qtbot.addWidget(edit)
    edit.show()
    edit.setText("00:00:00.500")

    edit._seconds.setFocus()
    QTest.keyClick(edit._seconds, Qt.Key_Down)

    assert edit.text() == "00:00:00.000"
