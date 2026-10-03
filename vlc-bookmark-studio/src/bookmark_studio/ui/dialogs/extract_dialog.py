"""The "Extract audio" window: the bookmarks selected in the list, saved as audio files of
their own -- song name, bookmark name and range in each file name (a name some file
system couldn't store is replaced, see media.extract.file_name_for) -- to a folder, in an
open format (MP3, Ogg Vorbis, Opus, FLAC or WAV), with the song's tags (and cover, in MP3
and FLAC), the tool's name and any tags of the user's own.

As each bookmark is set up in Bookmark Studio, by default: it loops its Repeat count (gaps
as silence; "Forever" has no count, so once), and fades in at the start and out at the
end of the whole file. FFmpeg cuts them, one after another, in the background; a point
bookmark, or one whose song file is missing, is listed and skipped."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bookmark_studio.domain.timecode import format_timecode
from bookmark_studio.media.extract import (
    FORMATS,
    FORMATS_BY_KEY,
    KNOWN_TAGS,
    ExtractCancelled,
    ExtractJob,
    available_formats,
    clean_tags,
    extract,
    file_name_for,
)
from bookmark_studio.settings.settings_service import ExtractOptions, SettingsService


class _Progress(QObject):
    """Signals from the extracting thread (queued to the window's thread)."""

    job_done = Signal(int, object, str)  # job index, Path or None, error ("" when saved)
    finished = Signal(bool)  # cancelled?


def _default_folder() -> str:
    music = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MusicLocation)
    return music or str(Path.home())


def _seconds(ms: int) -> str:
    return f"{ms / 1000:g} s"


class ExtractDialog(QDialog):
    def __init__(self, jobs: list[ExtractJob], ffmpeg_path: str | None, settings: SettingsService | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._jobs = jobs
        self._ffmpeg = ffmpeg_path
        self._settings = settings
        self._available = available_formats(ffmpeg_path)
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._saved_folder: Path | None = None
        self._building = True
        # (job index, Path or None, error) for every job that was tried
        self.results: list[tuple[int, Path | None, str]] = []
        self._progress = _Progress(self)
        self._progress.job_done.connect(self._on_job_done)
        self._progress.finished.connect(self._on_finished)
        self.setWindowTitle("Extract audio")
        self.setModal(True)
        self.resize(600, 600)
        options = settings.extract_options() if settings is not None else ExtractOptions()

        layout = QVBoxLayout(self)
        doable = [job for job in jobs if job.can_extract()]
        intro = QLabel(
            f"Save {len(doable)} bookmark{'s' if len(doable) != 1 else ''} as audio files of their own, "
            "named after the song, the bookmark and its start and end.", self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._list = QListWidget(self)
        for job in jobs:
            item = QListWidgetItem()
            if job.why_not():
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self._list.addItem(item)
        layout.addWidget(self._list, 1)

        form = QFormLayout()
        folder_row = QHBoxLayout()
        self._folder_edit = QLineEdit(options.folder if options.folder and Path(options.folder).is_dir()
                                      else _default_folder(), self)
        self._folder_edit.textChanged.connect(self._update_state)
        folder_row.addWidget(self._folder_edit, 1)
        self._browse_button = QPushButton("Browse...", self)
        self._browse_button.clicked.connect(self._browse)
        folder_row.addWidget(self._browse_button)
        form.addRow("Save to", folder_row)

        self._format_combo = QComboBox(self)
        model = self._format_combo.model()
        for fmt in FORMATS:
            usable = fmt.key in self._available
            self._format_combo.addItem(fmt.label if usable else f"{fmt.label} (not in this FFmpeg)", fmt.key)
            if not usable and isinstance(model, QStandardItemModel):
                entry = model.item(self._format_combo.count() - 1)
                if entry is not None:
                    entry.setEnabled(False)
        self._quality_combo = QComboBox(self)
        self._format_combo.currentIndexChanged.connect(self._on_format_changed)
        form.addRow("Format", self._format_combo)
        form.addRow("Quality", self._quality_combo)

        # As each bookmark is set up in Bookmark Studio (the list shows what that means).
        self._loops_check = QCheckBox("Loop as set in Bookmark Studio (its Repeat count; “Forever”: once)", self)
        self._loops_check.setToolTip("The range as many times as the bookmark's Repeat, its gap as silence in "
                                     "between; Loop off or Forever: once")
        self._loops_check.setChecked(options.apply_loops)
        self._loops_check.toggled.connect(self._show_names)
        form.addRow("Loops", self._loops_check)
        self._fades_check = QCheckBox("Fade in and out as set in Bookmark Studio", self)
        self._fades_check.setToolTip("At the very start and the very end of the whole file, however often it "
                                     "repeats")
        self._fades_check.setChecked(options.apply_fades)
        self._fades_check.toggled.connect(self._show_names)
        form.addRow("Fades", self._fades_check)

        # Tags of the user's own, in every file (on top of the song's).
        tags_box = QVBoxLayout()
        self._tags_table = QTableWidget(0, 2, self)
        self._tags_table.setHorizontalHeaderLabels(["Tag", "Value"])
        self._tags_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._tags_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tags_table.setColumnWidth(0, 140)
        self._tags_table.verticalHeader().setVisible(False)
        self._tags_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tags_table.setFixedHeight(96)
        self._tags_table.setToolTip("Written into every file, over the song's own tag of the same name")
        tags_box.addWidget(self._tags_table)
        tag_buttons = QHBoxLayout()
        self._add_tag_button = QPushButton("Add tag", self)
        self._add_tag_button.clicked.connect(lambda: self.add_tag("", ""))
        self._remove_tag_button = QPushButton("Remove tag", self)
        self._remove_tag_button.clicked.connect(self._remove_selected_tag)
        tag_buttons.addWidget(self._add_tag_button)
        tag_buttons.addWidget(self._remove_tag_button)
        tag_buttons.addStretch(1)
        tags_box.addLayout(tag_buttons)
        form.addRow("Your tags", tags_box)
        for key, value in options.tags:
            self.add_tag(key, value)

        self._example = QLabel(self)
        self._example.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._example.setWordWrap(True)
        form.addRow("File names", self._example)
        layout.addLayout(form)

        self._progress_bar = QProgressBar(self)
        self._progress_bar.setRange(0, max(1, len(doable)))
        self._progress_bar.setValue(0)
        layout.addWidget(self._progress_bar)
        self._status = QLabel(self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        self._open_folder_button = QPushButton("Open folder", self)
        self._open_folder_button.clicked.connect(self._open_folder)
        self._open_folder_button.hide()
        buttons.addWidget(self._open_folder_button)
        buttons.addStretch(1)
        self.extract_button = QPushButton("Extract", self)
        self.extract_button.setDefault(True)
        self.extract_button.clicked.connect(self.start)
        buttons.addWidget(self.extract_button)
        self._close_button = QPushButton("Close", self)
        self._close_button.clicked.connect(self.reject)
        buttons.addWidget(self._close_button)
        layout.addLayout(buttons)

        # The format last used (if this FFmpeg has it), else the first one it has.
        keys = [fmt.key for fmt in FORMATS]
        preferred = options.format_key if options.format_key in self._available else next(
            (key for key in keys if key in self._available), keys[0])
        self._format_combo.setCurrentIndex(keys.index(preferred))
        self._building = False
        self._on_format_changed()
        if options.format_key == preferred:
            self._quality_combo.setCurrentIndex(min(options.quality, self._quality_combo.count() - 1))
        if not ffmpeg_path:
            self._status.setText("FFmpeg was not found, so nothing can be extracted (Tools > Settings: FFmpeg).")
        elif not self._available:
            self._status.setText("This FFmpeg can't write any of these formats.")
        self._update_state()

    # -- choices --

    def _selected_format(self):  # noqa: ANN202 - AudioFormat
        return FORMATS_BY_KEY[str(self._format_combo.currentData())]

    def _on_format_changed(self) -> None:
        if self._building:
            return
        fmt = self._selected_format()
        self._quality_combo.clear()
        for label, _args in fmt.qualities:
            self._quality_combo.addItem(label)
        self._quality_combo.setEnabled(len(fmt.qualities) > 1)
        self._show_names()
        self._update_state()

    def _repeats(self, job: ExtractJob) -> int:
        return job.repeats() if self._loops_check.isChecked() else 1

    def _show_names(self, *_args) -> None:
        """Each line: the bookmark, and what its file gets (loops, fades, a replaced name);
        and the first file's name."""
        if self._building:
            return
        fmt = self._selected_format()
        for index, job in enumerate(self._jobs):
            item = self._list.item(index)
            if item is None or item.text().startswith(("✓", "✗")):
                continue
            text = f"{job.song} – {job.name}   {format_timecode(job.start_us)}"
            if job.end_us is not None:
                text += f" → {format_timecode(job.end_us)}"
            reason = job.why_not()
            if reason:
                item.setText(f"{text}   (skipped: {reason})")
                continue
            details = []
            if self._loops_check.isChecked() and job.loop_enabled:
                details.append(f"×{job.repeats()}" if job.repeat_count else "loops forever: once")
            if self._fades_check.isChecked() and (job.fade_in_ms or job.fade_out_ms):
                details.append(f"fade in {_seconds(job.fade_in_ms)}, out {_seconds(job.fade_out_ms)}")
            _name, renamed = file_name_for(job, fmt.extension, self._repeats(job))
            if renamed:
                details.append("file renamed")
            item.setText(text + (f"   ({'; '.join(details)})" if details else ""))
            item.setToolTip(f"File name changed: {renamed}" if renamed else "")
        first = next((job for job in self._jobs if job.can_extract()), None)
        if first is not None:
            name, renamed = file_name_for(first, fmt.extension, self._repeats(first))
            self._example.setText(name + (f"\n(renamed: {renamed})" if renamed else ""))

    def add_tag(self, key: str, value: str) -> None:
        """A row in Your tags: the tag's name (a known one, or any) and its value."""
        row = self._tags_table.rowCount()
        self._tags_table.insertRow(row)
        names = QComboBox(self._tags_table)
        names.setEditable(True)
        names.addItems(KNOWN_TAGS)
        names.setCurrentText(key or "comment")
        self._tags_table.setCellWidget(row, 0, names)
        cell = QTableWidgetItem(value)
        self._tags_table.setItem(row, 1, cell)
        if not value:
            self._tags_table.setCurrentCell(row, 1)
            self._tags_table.editItem(cell)

    def _remove_selected_tag(self) -> None:
        row = self._tags_table.currentRow()
        if row >= 0:
            self._tags_table.removeRow(row)

    def user_tags(self) -> list[tuple[str, str]]:
        pairs = []
        for row in range(self._tags_table.rowCount()):
            names = self._tags_table.cellWidget(row, 0)
            value = self._tags_table.item(row, 1)
            key = names.currentText() if isinstance(names, QComboBox) else ""
            pairs.append((key, value.text() if value is not None else ""))
        return list(clean_tags(pairs).items())

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Save the audio files to", self._folder_edit.text())
        if folder:
            self._folder_edit.setText(folder)

    def folder(self) -> Path:
        return Path(self._folder_edit.text().strip()).expanduser()

    def set_folder(self, folder: str) -> None:
        self._folder_edit.setText(folder)

    def set_format(self, key: str, quality: int = 0) -> None:
        self._format_combo.setCurrentIndex([fmt.key for fmt in FORMATS].index(key))
        self._quality_combo.setCurrentIndex(quality)

    def _update_state(self) -> None:
        if self._building:
            return
        running = self.is_running()
        ready = (bool(self._ffmpeg) and self._selected_format().key in self._available
                 and any(job.can_extract() for job in self._jobs) and bool(self._folder_edit.text().strip()))
        self.extract_button.setEnabled(ready and not running)
        for widget in (self._folder_edit, self._browse_button, self._format_combo, self._quality_combo,
                       self._loops_check, self._fades_check, self._tags_table, self._add_tag_button,
                       self._remove_tag_button):
            widget.setEnabled(not running)
        if not running:
            self._quality_combo.setEnabled(self._quality_combo.count() > 1)
        self._close_button.setText("Cancel" if running else "Close")

    # -- extracting --

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running() or not self.extract_button.isEnabled():
            return
        folder = self.folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._status.setText(f"Can't use that folder: {exc}")
            return
        fmt = self._selected_format()
        quality = max(0, self._quality_combo.currentIndex())
        loops = self._loops_check.isChecked()
        fades = self._fades_check.isChecked()
        tags = self.user_tags()
        if self._settings is not None:
            self._settings.set_extract_options(ExtractOptions(
                folder=str(folder), format_key=fmt.key, quality=quality, apply_loops=loops, apply_fades=fades,
                tags=tuple(tags),
            ))
        self._saved_folder = folder
        self.results = []
        self._cancel.clear()
        self._progress_bar.setValue(0)
        self._open_folder_button.hide()
        self._status.setText("Extracting...")
        ffmpeg = self._ffmpeg
        assert ffmpeg is not None
        jobs = list(enumerate(self._jobs))
        progress, cancel = self._progress, self._cancel
        tag_dict = dict(tags)

        def run() -> None:
            cancelled = False
            for index, job in jobs:
                if not job.can_extract():
                    continue
                if cancel.is_set():
                    cancelled = True
                    break
                try:
                    repeats = job.repeats() if loops else 1
                    path = extract(ffmpeg, job, folder, fmt, quality, apply_fades=fades, repeats=repeats,
                                   tags=tag_dict, cancelled=cancel)
                    progress.job_done.emit(index, path, "")
                except ExtractCancelled:
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001 - reported in the list, the rest go on
                    progress.job_done.emit(index, None, str(exc) or exc.__class__.__name__)
            progress.finished.emit(cancelled)

        self._thread = threading.Thread(target=run, name="extract-audio", daemon=True)
        self._thread.start()
        self._update_state()

    def _on_job_done(self, index: int, path: object, error: str) -> None:
        self.results.append((index, path if isinstance(path, Path) else None, error))
        item = self._list.item(index)
        if item is not None:
            if error:
                item.setText(f"✗ {item.text()}   ({error.splitlines()[-1]})")
                item.setToolTip(error)
            else:
                item.setText(f"✓ {item.text()}")
                item.setToolTip(str(path))
        self._progress_bar.setValue(len(self.results))

    def _on_finished(self, cancelled: bool) -> None:
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._thread = None
        saved = sum(1 for _index, path, _error in self.results if path is not None)
        failed = sum(1 for _index, _path, error in self.results if error)
        message = f"Saved {saved} file{'s' if saved != 1 else ''} to {self._saved_folder}."
        if failed:
            message += f" {failed} failed (see the list)."
        if cancelled:
            message += " Cancelled."
        self._status.setText(message)
        self._open_folder_button.setVisible(saved > 0)
        self._update_state()

    def _open_folder(self) -> None:
        if self._saved_folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._saved_folder)))

    def reject(self) -> None:  # Close, Esc, the title bar's X
        if self.is_running():
            self._cancel.set()  # Cancel: stop after cleaning up the file being written
            if self._thread is not None:
                self._thread.join(timeout=5)
            return
        super().reject()

    def wait_until_done(self, timeout_s: float = 30.0) -> None:
        """Tests: until the extracting thread has finished (its signals still queued)."""
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
