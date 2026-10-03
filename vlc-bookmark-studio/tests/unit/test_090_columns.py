"""0.9.0 (requested on its build): right-click a column title of the bookmark list for a
menu of which columns show, and in which order (the Arrange columns window too)."""
from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QDialog

from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.bookmark_panel import COLUMNS, DEFAULT_ORDER, BookmarkPanel
from bookmark_studio.ui.dialogs.columns_dialog import ColumnsDialog
from tests.unit.test_050_ui import _make_app


def _panel(qtbot) -> BookmarkPanel:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    return panel


def _actions(menu) -> dict[str, object]:
    return {action.text(): action for action in menu.actions() if action.text()}


def test_the_header_has_a_right_click_menu(qtbot) -> None:
    panel = _panel(qtbot)
    header = panel._tree.header()
    assert header.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    menu = panel.column_menu(COLUMNS.index("Name"))
    texts = [action.text() for action in menu.actions() if action.text()]
    assert texts[:len(COLUMNS)] == DEFAULT_ORDER  # every column, in its place
    assert all(action.isCheckable() and action.isChecked() for action in menu.actions()[:len(COLUMNS)])
    assert texts[len(COLUMNS):] == ["Move “Name” Left", "Move “Name” Right", "Arrange Columns...",
                                    "Restore Default Columns"]


def test_unticking_a_column_hides_it_but_one_always_stays(qtbot) -> None:
    panel = _panel(qtbot)
    _actions(panel.column_menu())["Tags"].trigger()
    assert "Tags" not in panel.shown_columns()
    _actions(panel.column_menu())["Tags"].trigger()  # ticked again
    assert "Tags" in panel.shown_columns()
    for name in COLUMNS[1:]:
        panel.set_column_shown(COLUMNS.index(name), False)
    assert panel.shown_columns() == ["Song"]
    last = _actions(panel.column_menu())["Song"]
    assert not last.isEnabled()  # the last one can't be unticked
    panel.set_column_shown(0, False)
    assert panel.shown_columns() == ["Song"]


def test_a_column_moves_left_and_right_past_hidden_ones(qtbot) -> None:
    panel = _panel(qtbot)
    panel.set_column_shown(COLUMNS.index("Tags"), False)  # between Song and Name
    actions = _actions(panel.column_menu(COLUMNS.index("Name")))
    actions["Move “Name” Left"].trigger()
    assert panel.shown_columns()[:2] == ["Name", "Song"]
    first = _actions(panel.column_menu(COLUMNS.index("Name")))
    assert not first["Move “Name” Left"].isEnabled() and first["Move “Name” Right"].isEnabled()
    panel.move_column(COLUMNS.index("Name"), 1)
    assert panel.shown_columns()[:2] == ["Song", "Name"]


def test_the_arrange_window_sets_which_show_and_their_order(qtbot, monkeypatch) -> None:
    panel = _panel(qtbot)

    def arrange(dialog: ColumnsDialog) -> int:
        dialog.restore_defaults()
        dialog.select_column(COLUMNS.index("End"))
        dialog._move(-1)  # End before Start
        dialog.set_shown(COLUMNS.index("Gap"), False)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ColumnsDialog, "exec", arrange)
    _actions(panel.column_menu())["Arrange Columns..."].trigger()
    shown = panel.shown_columns()
    assert "Gap" not in shown and shown.index("End") == shown.index("Start") - 1


def test_the_arrange_window_keeps_one_column_ticked(qtbot) -> None:
    dialog = ColumnsDialog([(0, "Song", True), (2, "Name", False)], [0, 2])
    qtbot.addWidget(dialog)
    dialog.set_shown(0, False)
    assert dialog.layout_chosen() == [(0, True), (2, False)]


def test_restore_default_columns(qtbot) -> None:
    panel = _panel(qtbot)
    panel.set_column_shown(COLUMNS.index("Gap"), False)
    panel.move_column(COLUMNS.index("Name"), 1)
    _actions(panel.column_menu())["Restore Default Columns"].trigger()
    assert panel.shown_columns() == DEFAULT_ORDER


def test_hidden_columns_are_remembered(qtbot, tmp_path) -> None:
    settings = SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    first = _make_app(qtbot, tmp_path, settings=settings, quit_app=lambda: None)
    first.window._bookmark_panel.set_column_shown(COLUMNS.index("Fade In"), False)
    first.window._bookmark_panel.move_column(COLUMNS.index("Loop"), -1)
    expected = first.window._bookmark_panel.shown_columns()
    first.quit()
    (tmp_path / "again").mkdir()
    again = _make_app(qtbot, tmp_path / "again", settings=settings, quit_app=lambda: None)
    assert again.window._bookmark_panel.shown_columns() == expected
    assert "Fade In" not in expected
