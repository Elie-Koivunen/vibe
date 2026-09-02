"""VlcLaunchDialog: pick an already-open VLC instance to attach to, or browse for a
playlist/media and launch a fresh one -- the "dropdown of open instances, alternatively
launch a new one with a browse button" flow the user asked for explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

LAUNCH_NEW_SENTINEL = -1


@dataclass
class VlcLaunchChoice:
    mode: str  # "attach" or "launch"
    port: int | None = None
    media_paths: list[str] = field(default_factory=list)


class VlcLaunchDialog(QDialog):
    def __init__(
        self, instances: list, media_filter: str, parent: QWidget | None = None,
        *, unmanaged_vlc_running: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Launch or Attach to VLC")
        self.setMinimumWidth(420)
        self._media_filter = media_filter
        self._media_paths: list[str] = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select an already-open VLC instance, or launch a new one:", self))

        self._combo = QComboBox(self)
        for instance in instances:
            self._combo.addItem(instance.label, instance.port)
        self._combo.addItem("Launch a new VLC instance...", LAUNCH_NEW_SENTINEL)
        if not instances:
            self._combo.setCurrentIndex(0)  # only "Launch a new instance..." exists
        layout.addWidget(self._combo)

        if not instances and unmanaged_vlc_running:
            # Direct fix for "it doesn't recognize preopen existing vlc instances":
            # a VLC window IS open, but this app genuinely cannot attach to it -- VLC's
            # remote-control HTTP interface can only be turned on at process launch
            # (--extraintf=http) or via a persistent choice in VLC's own Preferences,
            # never toggled onto a process from the outside after the fact. Explaining
            # that beats a dropdown that just silently has nothing in it.
            note = QLabel(
                "A VLC window appears to be open, but it wasn't started with remote "
                "control enabled, so this app can't attach to it or see its playlist. "
                "Launching a new instance below won't close it -- you'll end up with "
                "two VLC windows unless you close the other one yourself first.",
                self,
            )
            note.setWordWrap(True)
            note.setStyleSheet("color: #866;")
            layout.addWidget(note)

        browse_row = QHBoxLayout()
        self._browse_button = QPushButton("Browse for playlist/media...", self)
        self._browse_button.clicked.connect(self._on_browse)
        browse_row.addWidget(self._browse_button)
        self._media_label = QLabel("No media selected (VLC will start with an empty playlist)", self)
        self._media_label.setWordWrap(True)
        browse_row.addWidget(self._media_label, 1)
        layout.addLayout(browse_row)

        self._combo.currentIndexChanged.connect(self._update_browse_enabled)
        self._update_browse_enabled()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _is_launch_new_selected(self) -> bool:
        return self._combo.currentData() == LAUNCH_NEW_SENTINEL

    def _update_browse_enabled(self) -> None:
        self._browse_button.setEnabled(self._is_launch_new_selected())

    def _on_browse(self) -> None:
        from bookmark_studio.app.vlc_launcher import resolve_startup_media

        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self, "Select a playlist or media files", "", self._media_filter
        )
        if not paths:
            return
        self._media_paths = resolve_startup_media(paths)
        self._media_label.setText(f"{len(self._media_paths)} media item(s) selected")

    def choice(self) -> VlcLaunchChoice:
        if self._is_launch_new_selected():
            return VlcLaunchChoice(mode="launch", media_paths=self._media_paths)
        return VlcLaunchChoice(mode="attach", port=self._combo.currentData())
