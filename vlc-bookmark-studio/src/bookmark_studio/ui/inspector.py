"""Bookmark Inspector -- the "Bookmark" tab: name, start/end, loop settings, fades, tags and
notes (spec #42). Loop/Repeat/After loop share a row, so do Gap/Fade in/Fade out; tags are
picked from the tag list (TagPicker)."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QWidget,
)

from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import CompletionAction
from bookmark_studio.domain.selection import Selection
from bookmark_studio.domain.timecode import format_timecode, parse_timecode
from bookmark_studio.ui.tag_picker import TagPicker
from bookmark_studio.ui.transport import TimecodeEdit

COMPLETION_LABELS = {
    CompletionAction.CONTINUE: "Continue",
    CompletionAction.PAUSE: "Pause",
    CompletionAction.STOP: "Stop",
    CompletionAction.NEXT_BOOKMARK: "Next Bookmark",
    CompletionAction.PREVIOUS_BOOKMARK: "Previous Bookmark",
    CompletionAction.NEXT_SEGMENT_QUEUE_ITEM: "Next Segment Queue Item (not available yet)",
    CompletionAction.NEXT_TRACK: "Next Track",
}
# The Segment Queue (spec #175, P1) isn't built, so its completion action isn't offered;
# it's only shown for a bookmark that already has it (e.g. from an imported project).
OFFERED_COMPLETION_ACTIONS = [
    action for action in COMPLETION_LABELS if action is not CompletionAction.NEXT_SEGMENT_QUEUE_ITEM
]


class _NotesEdit(QPlainTextEdit):
    """QPlainTextEdit has no editingFinished. This one commits once, when focus
    leaves the field, not on every keystroke (one undo step per edit)."""

    editingFinished = Signal()

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        self.editingFinished.emit()


def _row(parent: QWidget, *parts: object) -> QWidget:
    """Several fields on one form row; strings become their labels."""
    row = QWidget(parent)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    for part in parts:
        if isinstance(part, str):
            label = QLabel(part, row)
            layout.addSpacing(6)
            layout.addWidget(label)
        else:
            layout.addWidget(part)  # type: ignore[arg-type]
    layout.addStretch(1)
    return row


class BookmarkInspector(QWidget):
    name_committed = Signal(str)
    # object, not int -- see TransportBar.bookmark_start_committed's comment: a plain
    # `int` signal arg overflows PySide6's 32-bit C++ marshaling beyond ~35.8 minutes,
    # which the new per-section (incl. hour) arrow-key stepping can reach directly.
    start_committed = Signal(object)
    end_committed = Signal(object)
    # enabled, repeat_count|None, gap_ms, action, fade_in_ms, fade_out_ms
    loop_settings_committed = Signal(bool, object, int, object, int, int)
    tags_committed = Signal(tuple)
    notes_committed = Signal(object)  # str | None
    edit_tags_requested = Signal()  # "Edit..." beside the tags: open the tag list window

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bookmark: Bookmark | None = None
        self._loading = False

        form = QFormLayout(self)

        self._name_edit = QLineEdit(self)
        self._name_edit.editingFinished.connect(self._on_name_committed)
        form.addRow("Name", self._name_edit)

        # TimecodeEdit: typed entry plus per-unit arrows (Up/Down steps).
        self._start_edit = TimecodeEdit(self)
        self._start_edit.editingFinished.connect(self._on_start_committed)
        form.addRow("Start", self._start_edit)

        self._end_edit = TimecodeEdit(self)
        self._end_edit.editingFinished.connect(self._on_end_committed)
        form.addRow("End", self._end_edit)

        # One row: Loop, Repeat, After loop.
        self._loop_checkbox = QCheckBox(self)
        self._loop_checkbox.setToolTip("Loop the bookmark when it plays")
        self._loop_checkbox.toggled.connect(self._on_loop_settings_changed)

        self._repeat_spin = QSpinBox(self)
        self._repeat_spin.setRange(0, 999)  # 0 means "Forever"
        self._repeat_spin.setSpecialValueText("Forever")
        self._repeat_spin.valueChanged.connect(self._on_loop_settings_changed)

        self._completion_combo = QComboBox(self)
        # Item data is the enum's string value: that is what Qt stores and hands back
        # (currentData() returns a plain str), so it's converted back on the way out.
        for action in OFFERED_COMPLETION_ACTIONS:
            self._completion_combo.addItem(COMPLETION_LABELS[action], action.value)
        self._completion_combo.currentIndexChanged.connect(self._on_loop_settings_changed)
        form.addRow("Loop", _row(self, self._loop_checkbox, "Repeat", self._repeat_spin,
                                 "After loop", self._completion_combo))

        # The row below: Gap, Fade in, Fade out. Fades: 0 disables, like Gap.
        self._gap_spin = QSpinBox(self)
        self._gap_spin.setRange(0, 60_000)
        self._gap_spin.setSuffix(" ms")
        self._gap_spin.valueChanged.connect(self._on_loop_settings_changed)

        self._fade_in_spin = QSpinBox(self)
        self._fade_in_spin.setRange(0, 60_000)
        self._fade_in_spin.setSuffix(" ms")
        self._fade_in_spin.setSpecialValueText("Off")
        self._fade_in_spin.valueChanged.connect(self._on_loop_settings_changed)

        self._fade_out_spin = QSpinBox(self)
        self._fade_out_spin.setRange(0, 60_000)
        self._fade_out_spin.setSuffix(" ms")
        self._fade_out_spin.setSpecialValueText("Off")
        self._fade_out_spin.valueChanged.connect(self._on_loop_settings_changed)
        form.addRow("Gap", _row(self, self._gap_spin, "Fade in", self._fade_in_spin, "Fade out", self._fade_out_spin))

        # Picked from the tag list, several at once; committed when the list closes.
        self._tags_picker = TagPicker(self)
        self._tags_picker.tags_changed.connect(self._on_tags_committed)
        self._tags_picker.edit_catalog_requested.connect(self.edit_tags_requested.emit)
        form.addRow("Tags", self._tags_picker)

        self._notes_edit = _NotesEdit(self)
        self._notes_edit.editingFinished.connect(self._on_notes_committed)
        form.addRow("Notes", self._notes_edit)

        self.show_selection(None)  # starts disabled: nothing loaded or selected yet

    def current_bookmark(self) -> Bookmark | None:
        return self._bookmark

    def set_tag_catalog(self, names: list[str]) -> None:
        """The tags to offer (TagRepository.names())."""
        self._tags_picker.set_catalog(names)

    def set_snapshot(self, bookmark: Bookmark | None) -> None:
        """Updates the inspected bookmark's stored values WITHOUT touching the widgets.
        Called after every committed edit: the stale snapshot this replaces made each
        later edit record the wrong "old" value, so undo skipped intermediate states."""
        if bookmark is not None and self._bookmark is not None and bookmark.id == self._bookmark.id:
            self._bookmark = bookmark

    def load_bookmark(self, bookmark: Bookmark) -> None:
        self._flush_notes()
        self._loading = True
        try:
            self._bookmark = bookmark
            self._name_edit.setText(bookmark.name)
            self._start_edit.setEnabled(True)
            self._start_edit.setText(format_timecode(bookmark.start_us))
            self._end_edit.setText(format_timecode(bookmark.end_us) if bookmark.end_us is not None else "")
            self._end_edit.setEnabled(bookmark.end_us is not None)
            self._loop_checkbox.setChecked(bookmark.loop_enabled)
            self._repeat_spin.setValue(bookmark.repeat_count or 0)
            self._gap_spin.setValue(bookmark.loop_gap_ms)
            index = self._completion_combo.findData(bookmark.completion_action.value)
            if index < 0:
                self._completion_combo.addItem(COMPLETION_LABELS[bookmark.completion_action],
                                               bookmark.completion_action.value)
                index = self._completion_combo.count() - 1
            self._completion_combo.setCurrentIndex(index)
            self._fade_in_spin.setValue(bookmark.fade_in_ms)
            self._fade_out_spin.setValue(bookmark.fade_out_ms)
            self._tags_picker.set_tags(bookmark.tags)
            self._notes_edit.setPlainText(bookmark.notes or "")
        finally:
            self._loading = False

    def clear(self) -> None:
        self._flush_notes()
        self._loading = True
        try:
            self._bookmark = None
            for widget in (self._name_edit, self._start_edit, self._end_edit):
                widget.clear()
            self._tags_picker.set_tags(())
            self._notes_edit.clear()
        finally:
            self._loading = False

    def show_selection(self, selection: Selection | None) -> None:
        """Mirrors an in-progress drag-selection (not yet a bookmark) in the
        Start/End fields, live while it is marked or adjusted. Only
        while nothing is actually loaded here: an in-progress drag elsewhere on
        the waveform must not silently clobber a bookmark someone is mid-edit on.
        Typing into the fields during a preview is a harmless no-op (see
        _on_start_committed/_on_end_committed's existing `self._bookmark is None`
        guard) -- the actual way to edit a raw selection is dragging it on the
        waveform, not typing here.
        """
        if self._bookmark is not None:
            return
        self._loading = True
        try:
            has_selection = selection is not None
            self._start_edit.setText(format_timecode(selection.start_us) if selection is not None else "")
            self._start_edit.setEnabled(has_selection)
            self._end_edit.setText(format_timecode(selection.end_us) if selection is not None else "")
            self._end_edit.setEnabled(has_selection)
        finally:
            self._loading = False

    def commit_pending(self) -> None:
        """Saves text still being typed (name, times, notes) -- before quitting. (Tags are
        saved as soon as their list closes.)"""
        if self._bookmark is None or self._loading:
            return
        self._on_name_committed()
        self._on_start_committed()
        self._on_end_committed()
        self._on_notes_committed()

    def _flush_notes(self) -> None:
        """Commits notes typed into the field before the inspector switches away."""
        if self._bookmark is not None and not self._loading:
            self._on_notes_committed()

    def _on_name_committed(self) -> None:
        if self._loading or self._bookmark is None:
            return
        new_name = self._name_edit.text().strip()
        if not new_name:
            self._name_edit.setText(self._bookmark.name)  # a bookmark needs a name: revert
            return
        if new_name != self._bookmark.name:
            self.name_committed.emit(new_name)

    def _on_start_committed(self) -> None:
        if self._loading or self._bookmark is None:
            return
        try:
            value = parse_timecode(self._start_edit.text())
        except ValueError:
            self._start_edit.setText(format_timecode(self._bookmark.start_us))
            return
        if value != self._bookmark.start_us:
            self.start_committed.emit(value)

    def _on_end_committed(self) -> None:
        if self._loading or self._bookmark is None or self._bookmark.end_us is None:
            return
        try:
            value = parse_timecode(self._end_edit.text())
        except ValueError:
            self._end_edit.setText(format_timecode(self._bookmark.end_us))
            return
        if value != self._bookmark.end_us:
            self.end_committed.emit(value)

    def _on_loop_settings_changed(self, *_args: object) -> None:
        if self._loading or self._bookmark is None:
            return
        repeat_count = self._repeat_spin.value() or None
        action = CompletionAction(self._completion_combo.currentData())
        new = (
            self._loop_checkbox.isChecked(), repeat_count, self._gap_spin.value(), action,
            self._fade_in_spin.value(), self._fade_out_spin.value(),
        )
        bookmark = self._bookmark
        old = (
            bookmark.loop_enabled, bookmark.repeat_count, bookmark.loop_gap_ms, bookmark.completion_action,
            bookmark.fade_in_ms, bookmark.fade_out_ms,
        )
        if new != old:
            self.loop_settings_committed.emit(*new)

    def _on_tags_committed(self, picked: tuple[str, ...]) -> None:
        if self._loading or self._bookmark is None:
            return
        tags = tuple(dict.fromkeys(t.strip() for t in picked if t.strip()))
        if tuple(sorted(t.casefold() for t in tags)) != tuple(sorted(t.casefold() for t in self._bookmark.tags)):
            self.tags_committed.emit(tags)

    def _on_notes_committed(self) -> None:
        if self._loading or self._bookmark is None:
            return
        notes = self._notes_edit.toPlainText()
        normalized = notes if notes.strip() else None
        if normalized != (self._bookmark.notes if (self._bookmark.notes or "").strip() else None):
            self.notes_committed.emit(normalized)
