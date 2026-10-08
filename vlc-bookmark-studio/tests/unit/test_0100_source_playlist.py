"""0.10.0 (requested on the 0.9.0 build): Launch VLC... and Quit in the Source Playlist
tab with Follow; above the connection, the source playlist's name."""
from __future__ import annotations

from PySide6.QtCore import QPoint

from bookmark_studio.ui.playlist_panel import PlaylistPanel
from tests.unit.test_050_ui import _make_app


def test_launch_and_quit_are_in_the_source_playlist_tab_above_follow(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    panel = window._playlist_panel
    page = panel._tabs.widget(0)
    for widget in (panel._launch_vlc_button, panel._quit_button, panel._follow_checkbox):
        assert page.isAncestorOf(widget)

    def at(widget) -> QPoint:  # noqa: ANN001
        return widget.mapTo(window, QPoint(0, 0))

    assert at(panel._launch_vlc_button).y() == at(panel._quit_button).y()  # one row...
    assert at(panel._launch_vlc_button).x() < at(panel._quit_button).x()
    assert at(panel._quit_button).y() < at(panel._follow_checkbox).y() < at(panel._filter_edit).y()  # ...on top
    assert at(panel._tabs).y() < at(panel._launch_vlc_button).y()


def test_the_moved_buttons_still_ask_for_launch_and_quit(qtbot) -> None:
    panel = PlaylistPanel()  # (the app's own Launch window would wait for an answer)
    qtbot.addWidget(panel)
    with qtbot.waitSignal(panel.launch_vlc_requested, timeout=1000):
        panel._launch_vlc_button.click()
    with qtbot.waitSignal(panel.quit_requested, timeout=1000):
        panel._quit_button.click()


def test_the_playlists_name_is_shown_above_the_connection(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    panel = window._playlist_panel
    record = app._playlist_repository.get(app.playlists.active_playlist_id)
    qtbot.waitUntil(lambda: panel.playlist_name() == record.playlist.name, timeout=3000)
    assert panel.playlist_name().startswith("Unsaved VLC Playlist")  # VLC's own playlist: no .m3u
    assert panel._playlist_name.isReadOnly()
    page = panel._tabs.widget(0)  # (in the tab since the 0.10.0 build's feedback)
    assert page.isAncestorOf(panel._playlist_name) and page.isAncestorOf(panel._connection_label)

    def y(widget) -> int:  # noqa: ANN001
        return widget.mapTo(window, QPoint(0, 0)).y()

    assert y(panel._tabs) < y(panel._playlist_name) < y(panel._connection_label) < y(panel._launch_vlc_button)


def test_a_playlist_from_an_m3u_shows_its_file_name(qtbot, tmp_path) -> None:
    from bookmark_studio.playlist.synchronizer import _playlist_name_from_source

    panel = PlaylistPanel()
    qtbot.addWidget(panel)
    assert panel.playlist_name() == "" and panel._playlist_name.placeholderText()
    # A path of the system the test runs on (a Windows C: path means nothing to Linux).
    name = _playlist_name_from_source((tmp_path / "Practice set.m3u").as_uri())
    assert name == "Practice set"
    panel.set_playlist_name(name)
    assert panel.playlist_name() == name
    panel.set_playlist_name(None)  # another player, its playlist not known yet
    assert panel.playlist_name() == ""
