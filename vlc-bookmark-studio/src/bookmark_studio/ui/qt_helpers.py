"""Small helpers shared by the Qt widgets."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QAbstractSpinBox, QTabWidget, QTreeWidget, QTreeWidgetItem

# The selected tab is orange (the orange of the "saved" flash), the others follow the
# palette, light or dark. A style sheet: the platform styles ignore a palette for tabs.
SELECTED_TAB_COLOR = "#ff9f1a"
SELECTED_TAB_TEXT_COLOR = "#1a1a1a"
TAB_STYLE = f"""
QTabBar::tab {{
    padding: 4px 12px;
    margin-right: 2px;
    border: 1px solid palette(mid);
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    background: palette(button);
    color: palette(button-text);
}}
QTabBar::tab:selected {{
    background: {SELECTED_TAB_COLOR};
    color: {SELECTED_TAB_TEXT_COLOR};
    border-color: #c97700;
}}
QTabBar::tab:!selected:hover {{
    background: palette(midlight);
}}
"""


class _SelectAllOnFocus(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.FocusIn and isinstance(watched, QAbstractSpinBox):
            # After the click that gave the focus has placed the cursor.
            QTimer.singleShot(0, watched.selectAll)
        return False


def select_all_on_focus(*spins: QAbstractSpinBox) -> None:
    """A click (or Tab) into one of `spins` selects its text, so typing replaces it -- e.g.
    "Off" or "Forever" -- instead of having to delete it first."""
    for spin in spins:
        spin.installEventFilter(_SelectAllOnFocus(spin))


def style_tabs(tabs: QTabWidget) -> None:
    """Gives `tabs` the orange selected tab."""
    tabs.tabBar().setStyleSheet(TAB_STYLE)


def top_level_rows(tree: QTreeWidget) -> list[QTreeWidgetItem]:
    """The tree's top-level rows, in order (Qt's topLevelItem() may return None)."""
    rows = (tree.topLevelItem(i) for i in range(tree.topLevelItemCount()))
    return [row for row in rows if row is not None]
