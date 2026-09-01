"""Bookmark Inspector: name/start/end/loop/repeat/gap/lane/tags/notes editor (spec #42)."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QLineEdit, QPlainTextEdit, QSpinBox, QWidget,
)

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import CompletionAction
from bookmark_studio.ui.transport import format_timecode, parse_timecode

COMPLETION_LABELS = {
    CompletionAction.CONTINUE: "Continue",
    CompletionAction.PAUSE: "Pause",
    CompletionAction.STOP: "Stop",
    CompletionAction.NEXT_BOOKMARK: "Next Bookmark",
    CompletionAction.PREVIOUS_BOOKMARK: "Previous Bookmark",
    CompletionAction.NEXT_SEGMENT_QUEUE_ITEM: "Next Segment Queue Item",
    CompletionAction.NEXT_TRACK: "Next Track",
}


class BookmarkInspector(QWidget):
    name_committed = Signal(str)
    start_committed = Signal(int)
    end_committed = Signal(int)
    loop_settings_committed = Signal(bool, object, int, object)  # enabled, repeat_count|None, gap_ms, action
    tags_committed = Signal(tuple)
    notes_committed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bookmark: Bookmark | None = None
        self._loading = False

        form = QFormLayout(self)

        self._name_edit = QLineEdit(self)
        self._name_edit.editingFinished.connect(self._on_name_committed)
        form.addRow("Name", self._name_edit)

        self._start_edit = QLineEdit(self)
        self._start_edit.editingFinished.connect(self._on_start_committed)
        form.addRow("Start", self._start_edit)

        self._end_edit = QLineEdit(self)
        self._end_edit.editingFinished.connect(self._on_end_committed)
        form.addRow("End", self._end_edit)

        self._loop_checkbox = QCheckBox(self)
        self._loop_checkbox.toggled.connect(self._on_loop_settings_changed)
        form.addRow("Loop", self._loop_checkbox)

        self._repeat_spin = QSpinBox(self)
        self._repeat_spin.setRange(0, 999)  # 0 means "Forever"
        self._repeat_spin.setSpecialValueText("Forever")
        self._repeat_spin.valueChanged.connect(self._on_loop_settings_changed)
        form.addRow("Repeat", self._repeat_spin)

        self._gap_spin = QSpinBox(self)
        self._gap_spin.setRange(0, 60_000)
        self._gap_spin.setSuffix(" ms")
        self._gap_spin.valueChanged.connect(self._on_loop_settings_changed)
        form.addRow("Gap", self._gap_spin)

        self._completion_combo = QComboBox(self)
        for action, label in COMPLETION_LABELS.items():
            self._completion_combo.addItem(label, action)
        self._completion_combo.currentIndexChanged.connect(self._on_loop_settings_changed)
        form.addRow("After loop", self._completion_combo)

        self._tags_edit = QLineEdit(self)
        self._tags_edit.editingFinished.connect(self._on_tags_committed)
        form.addRow("Tags", self._tags_edit)

        self._notes_edit = QPlainTextEdit(self)
        self._notes_edit.textChanged.connect(self._on_notes_committed)
        form.addRow("Notes", self._notes_edit)

    def current_bookmark(self) -> Bookmark | None:
        return self._bookmark

    def load_bookmark(self, bookmark: Bookmark) -> None:
        self._loading = True
        try:
            self._bookmark = bookmark
            self._name_edit.setText(bookmark.name)
            self._start_edit.setText(format_timecode(bookmark.start_us))
            self._end_edit.setText(format_timecode(bookmark.end_us) if bookmark.end_us is not None else "")
            self._end_edit.setEnabled(bookmark.end_us is not None)
            self._loop_checkbox.setChecked(bookmark.loop_enabled)
            self._repeat_spin.setValue(bookmark.repeat_count or 0)
            self._gap_spin.setValue(bookmark.loop_gap_ms)
            index = self._completion_combo.findData(bookmark.completion_action)
            self._completion_combo.setCurrentIndex(max(0, index))
            self._tags_edit.setText(", ".join(bookmark.tags))
            self._notes_edit.setPlainText(bookmark.notes or "")
        finally:
            self._loading = False

    def clear(self) -> None:
        self._bookmark = None
        for widget in (self._name_edit, self._start_edit, self._end_edit, self._tags_edit):
            widget.clear()
        self._notes_edit.clear()

    def _on_name_committed(self) -> None:
        if not self._loading and self._bookmark is not None:
            self.name_committed.emit(self._name_edit.text())

    def _on_start_committed(self) -> None:
        if self._loading or self._bookmark is None:
            return
        try:
            self.start_committed.emit(parse_timecode(self._start_edit.text()))
        except ValueError:
            self._start_edit.setText(format_timecode(self._bookmark.start_us))

    def _on_end_committed(self) -> None:
        if self._loading or self._bookmark is None or self._bookmark.end_us is None:
            return
        try:
            self.end_committed.emit(parse_timecode(self._end_edit.text()))
        except ValueError:
            self._end_edit.setText(format_timecode(self._bookmark.end_us))

    def _on_loop_settings_changed(self, *_args: object) -> None:
        if self._loading or self._bookmark is None:
            return
        repeat_count = self._repeat_spin.value() or None
        action = self._completion_combo.currentData()
        self.loop_settings_committed.emit(
            self._loop_checkbox.isChecked(), repeat_count, self._gap_spin.value(), action
        )

    def _on_tags_committed(self) -> None:
        if self._loading or self._bookmark is None:
            return
        tags = tuple(t.strip() for t in self._tags_edit.text().split(",") if t.strip())
        self.tags_committed.emit(tags)

    def _on_notes_committed(self) -> None:
        if self._loading or self._bookmark is None:
            return
        self.notes_committed.emit(self._notes_edit.toPlainText())
