"""The "Edit tags" window: add, rename, remove and reorder the tags the Bookmark tab offers.

Changes apply at once, to every bookmark (see TagRepository): renaming a tag renames it on
all bookmarks that carry it -- into an existing tag, the two merge -- and removing it takes
it off them. Both ask first when bookmarks are affected. Each tag shows how many bookmarks
carry it.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from bookmark_studio.persistence.tag_repository import MAX_TAG_LENGTH, InvalidTagName, TagRepository

Confirm = Callable[[str, str], bool]  # (title, question) -> yes?
AskText = Callable[[str, str, str], str | None]  # (title, label, current) -> new text or None
NAME_ROLE = Qt.ItemDataRole.UserRole


class TagCatalogDialog(QDialog):
    def __init__(self, tags: TagRepository, parent: QWidget | None = None, *, selected: str | None = None,
                 confirm: Confirm | None = None, ask_text: AskText | None = None,
                 show_error: Callable[[str], None] | None = None) -> None:
        super().__init__(parent)
        self._tags = tags
        self._confirm = confirm or self._confirm_dialog
        self._ask_text = ask_text or self._ask_text_dialog
        self._show_error = show_error or (lambda message: QMessageBox.warning(self, "Edit tags", message))
        self.changed = False  # whether anything was changed (the caller refreshes then)
        self.setWindowTitle("Edit tags")
        self.setModal(True)
        self.resize(420, 440)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "The tags offered for bookmarks. Renaming or removing a tag changes every "
            "bookmark that has it.", self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        add_row = QHBoxLayout()
        self._new_name = QLineEdit(self)
        self._new_name.setPlaceholderText("New tag, e.g. crowd chant")
        self._new_name.setMaxLength(MAX_TAG_LENGTH)
        self._new_name.returnPressed.connect(self._on_add_clicked)
        add_row.addWidget(self._new_name, 1)
        self._add_button = QPushButton("Add", self)
        self._add_button.clicked.connect(self._on_add_clicked)
        add_row.addWidget(self._add_button)
        layout.addLayout(add_row)

        body = QHBoxLayout()
        self._list = QListWidget(self)
        self._list.itemSelectionChanged.connect(self._update_buttons)
        self._list.itemDoubleClicked.connect(lambda _item: self._on_rename_clicked())
        body.addWidget(self._list, 1)
        side = QVBoxLayout()
        self._rename_button = QPushButton("Rename...", self)
        self._rename_button.clicked.connect(self._on_rename_clicked)
        self._remove_button = QPushButton("Remove", self)
        self._remove_button.clicked.connect(self._on_remove_clicked)
        self._up_button = QPushButton("Move up", self)
        self._up_button.clicked.connect(lambda: self._on_move_clicked(-1))
        self._down_button = QPushButton("Move down", self)
        self._down_button.clicked.connect(lambda: self._on_move_clicked(1))
        for button in (self._rename_button, self._remove_button, self._up_button, self._down_button):
            side.addWidget(button)
        side.addStretch(1)
        body.addLayout(side)
        layout.addLayout(body, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._reload(selected)

    # -- the list --

    def names(self) -> list[str]:
        return [self._list.item(i).data(NAME_ROLE) for i in range(self._list.count())]

    def _selected_name(self) -> str | None:
        items = self._list.selectedItems()
        return items[0].data(NAME_ROLE) if items else None

    def _reload(self, select: str | None = None) -> None:
        counts = self._tags.usage_counts()
        self._list.clear()
        for name in self._tags.names():
            used = counts.get(name.casefold(), 0)
            label = name if used == 0 else f"{name}   ({used} bookmark{'s' if used != 1 else ''})"
            item = QListWidgetItem(label)
            item.setData(NAME_ROLE, name)
            self._list.addItem(item)
            if select is not None and name.casefold() == select.casefold():
                item.setSelected(True)
                self._list.setCurrentItem(item)
        self._update_buttons()

    def _update_buttons(self) -> None:
        name = self._selected_name()
        names = self.names()
        index = names.index(name) if name in names else -1
        self._rename_button.setEnabled(name is not None)
        self._remove_button.setEnabled(name is not None)
        self._up_button.setEnabled(index > 0)
        self._down_button.setEnabled(0 <= index < len(names) - 1)

    # -- actions (also called directly by tests) --

    def add_tag(self, name: str) -> bool:
        try:
            added = self._tags.add(name)
        except InvalidTagName as exc:
            self._show_error(str(exc))
            return False
        self.changed = True
        self._reload(added)
        return True

    def rename_tag(self, old: str, new: str) -> bool:
        if new.strip() == old:
            return False
        used = self._tags.usage_counts().get(old.casefold(), 0)
        existing = next((n for n in self._tags.names() if n.casefold() == " ".join(new.split()).casefold()), None)
        if existing is not None and existing.casefold() != old.casefold():
            question = f"“{existing}” is already in the list. Merge “{old}” into it?"
            if used:
                question += f" {used} bookmark{'s' if used != 1 else ''} will carry “{existing}” instead."
            if not self._confirm("Merge tags", question):
                return False
        elif used and not self._confirm(
            "Rename tag",
            f"Rename “{old}” to “{' '.join(new.split())}” on "
            f"{used} bookmark{'s' if used != 1 else ''}?",
        ):
            return False
        try:
            self._tags.rename(old, new)
        except InvalidTagName as exc:
            self._show_error(str(exc))
            return False
        self.changed = True
        self._reload(existing or new)
        return True

    def remove_tag(self, name: str) -> bool:
        used = self._tags.usage_counts().get(name.casefold(), 0)
        question = f"Remove “{name}” from the list?"
        if used:
            question += f" It will be taken off {used} bookmark{'s' if used != 1 else ''}."
        if not self._confirm("Remove tag", question):
            return False
        try:
            self._tags.remove(name)
        except InvalidTagName as exc:
            self._show_error(str(exc))
            return False
        self.changed = True
        self._reload()
        return True

    def move_tag(self, name: str, delta: int) -> None:
        self._tags.move(name, delta)
        self.changed = True
        self._reload(name)

    # -- buttons --

    def _on_add_clicked(self) -> None:
        if self.add_tag(self._new_name.text()):
            self._new_name.clear()

    def _on_rename_clicked(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        new = self._ask_text("Rename tag", f"New name for “{name}”:", name)
        if new is not None:
            self.rename_tag(name, new)

    def _on_remove_clicked(self) -> None:
        name = self._selected_name()
        if name is not None:
            self.remove_tag(name)

    def _on_move_clicked(self, delta: int) -> None:
        name = self._selected_name()
        if name is not None:
            self.move_tag(name, delta)

    # -- default prompts --

    def _confirm_dialog(self, title: str, question: str) -> bool:
        answer = QMessageBox.question(self, title, question)
        return answer == QMessageBox.StandardButton.Yes

    def _ask_text_dialog(self, title: str, label: str, current: str) -> str | None:
        text, ok = QInputDialog.getText(self, title, label, QLineEdit.EchoMode.Normal, current)
        return text if ok else None


__all__ = ["TagCatalogDialog"]
