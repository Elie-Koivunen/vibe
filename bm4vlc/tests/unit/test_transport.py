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
    transport = TransportBar()
    qtbot.addWidget(transport)

    transport.set_time(7_000_000, 30_000_000)

    assert transport._position_edit.text() == "00:00:07.000"
    assert transport._duration_label.text() == "/ 00:00:30.000"


def test_editing_position_field_emits_position_seek_requested(qtbot) -> None:
    """Direct user request: "the timer are still not manually editable" -- the
    current-position half of the transport display is now a real editable field."""
    transport = TransportBar()
    qtbot.addWidget(transport)
    transport.show()
    transport.set_time(7_000_000, 30_000_000)

    requests = []
    transport.position_seek_requested.connect(requests.append)

    edit = transport._position_edit
    edit.setFocus()
    edit.selectAll()
    QTest.keyClicks(edit, "00:00:15.000")
    QTest.keyClick(edit, Qt.Key_Return)

    assert requests == [15_000_000]


def test_set_time_does_not_clobber_the_field_while_it_has_focus(qtbot) -> None:
    """A live status poll calls set_time() roughly every 400ms -- it must not stomp
    whatever the user is mid-typing into the position field. Real OS-level focus is
    unreliable under the offscreen Qt platform used for headless testing (see similar
    notes elsewhere in this test suite), so this drives the same hasFocus() branch
    set_time() checks by asking the field itself rather than the window manager.
    """
    transport = TransportBar()
    qtbot.addWidget(transport)
    transport.set_time(7_000_000, 30_000_000)

    edit = transport._position_edit
    edit.setText("00:00:20")
    assert edit.hasFocus() is False  # not focused -- confirms the next call's premise
    edit.hasFocus = lambda: True  # simulate real focus without depending on the WM
    try:
        transport.set_time(8_000_000, 30_000_000)
        assert edit.text() == "00:00:20"
    finally:
        del edit.hasFocus
