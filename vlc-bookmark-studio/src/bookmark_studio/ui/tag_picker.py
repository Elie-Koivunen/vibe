"""TagPicker: the Bookmark tab's tag field -- a drop list of the catalog's tags with a
tick box each (several at once), and "Edit tags..." to open the tag list window.

The list stays open while tags are ticked; the choice is committed once, when it closes
(one undo step). A tag the bookmark carries that the catalog doesn't list (shouldn't
happen -- see TagRepository.canonical -- but never hidden if it does) is shown ticked.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QMenu,
    QSizePolicy,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QToolButton,
    QWidget,
)

EDIT_TAGS_TEXT = "Edit tags..."


class _StayOpenMenu(QMenu):
    """A click on a tick box toggles it without closing the menu."""

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        action = self.actionAt(event.position().toPoint())
        if action is not None and action.isCheckable() and action.isEnabled():
            action.trigger()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _TagCombo(QComboBox):
    """Looks like the other drop lists (left-aligned text, arrow) but opens the tick-box
    menu and shows the ticked tags as its text."""

    def __init__(self, menu: QMenu, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._menu = menu
        self._text = ""

    def set_text(self, text: str) -> None:
        self._text = text
        self.update()

    def text(self) -> str:
        return self._text

    def showPopup(self) -> None:  # noqa: N802 - Qt override
        self._menu.setMinimumWidth(self.width())
        self._menu.popup(self.mapToGlobal(QPoint(0, self.height())))

    def hidePopup(self) -> None:  # noqa: N802 - Qt override
        self._menu.hide()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentText = self._text
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)


class TagPicker(QWidget):
    tags_changed = Signal(tuple)  # the ticked tags, in the catalog's order
    edit_catalog_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._catalog: list[str] = []
        self._tags: tuple[str, ...] = ()
        self._opened_with: tuple[str, ...] = ()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._menu = _StayOpenMenu(self)
        self._menu.aboutToShow.connect(self._on_menu_about_to_show)
        self._menu.aboutToHide.connect(self._on_menu_hidden)
        self._button = _TagCombo(self._menu, self)
        self._button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._button, 1)

        self._edit_button = QToolButton(self)
        self._edit_button.setText("Edit...")
        self._edit_button.setToolTip("Add, rename, remove or reorder the tags in this list")
        self._edit_button.clicked.connect(self.edit_catalog_requested.emit)
        layout.addWidget(self._edit_button)
        self._show_text()

    # -- API --

    def set_catalog(self, names: list[str]) -> None:
        self._catalog = list(names)
        self._tags = self._ordered(self._tags)
        self._show_text()

    def set_tags(self, tags: tuple[str, ...] | list[str]) -> None:
        """Shows a bookmark's tags; no tags_changed."""
        self._tags = self._ordered(tags)
        self._show_text()

    def tags(self) -> tuple[str, ...]:
        return self._tags

    def menu(self) -> QMenu:
        return self._menu

    # -- internals --

    def _ordered(self, tags) -> tuple[str, ...]:  # noqa: ANN001
        wanted = {t.casefold(): t for t in tags}
        ordered = [name for name in self._catalog if name.casefold() in wanted]
        known = {name.casefold() for name in self._catalog}
        ordered += [t for key, t in wanted.items() if key not in known]
        return tuple(ordered)

    def _show_text(self) -> None:
        text = ", ".join(self._tags) if self._tags else "No tags"
        self._button.set_text(text)
        self._button.setToolTip(f"Tags: {text} -- click to pick (several at once)")

    def _on_menu_about_to_show(self) -> None:
        self._opened_with = self._tags
        self._menu.clear()
        ticked = {t.casefold() for t in self._tags}
        known = {name.casefold() for name in self._catalog}
        for name in self._catalog + [t for t in self._tags if t.casefold() not in known]:
            action = QAction(name if name.casefold() in known else f"{name} (not in the list)", self._menu)
            action.setCheckable(True)
            action.setChecked(name.casefold() in ticked)
            action.toggled.connect(lambda checked, name=name: self._on_toggled(name, checked))
            self._menu.addAction(action)
        if not self._catalog:
            empty = self._menu.addAction("The list is empty")
            empty.setEnabled(False)
        self._menu.addSeparator()
        edit = self._menu.addAction(EDIT_TAGS_TEXT)
        edit.triggered.connect(self.edit_catalog_requested.emit)

    def _on_toggled(self, name: str, checked: bool) -> None:
        current = [t for t in self._tags if t.casefold() != name.casefold()]
        if checked:
            current.append(name)
        self._tags = self._ordered(current)
        self._show_text()

    def _on_menu_hidden(self) -> None:
        if self._tags != self._opened_with:
            self.tags_changed.emit(self._tags)
        self._opened_with = self._tags


__all__ = ["EDIT_TAGS_TEXT", "TagPicker"]
