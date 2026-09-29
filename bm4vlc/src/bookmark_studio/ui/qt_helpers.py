"""Small helpers shared by the Qt widgets."""
from __future__ import annotations

from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem


def top_level_rows(tree: QTreeWidget) -> list[QTreeWidgetItem]:
    """The tree's top-level rows, in order (Qt's topLevelItem() may return None)."""
    rows = (tree.topLevelItem(i) for i in range(tree.topLevelItemCount()))
    return [row for row in rows if row is not None]
