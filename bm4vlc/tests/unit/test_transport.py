from __future__ import annotations

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
