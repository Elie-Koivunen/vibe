"""VlcLaunchDialog: choose the player -- attach to an open VLC window, launch a new VLC
window with a playlist/media, or play them inside this app (libVLC)."""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

LAUNCH_NEW_SENTINEL = -1
IN_APP_SENTINEL = -2


@dataclass
class VlcLaunchChoice:
    mode: str  # "attach", "launch" (a new VLC window) or "in_app" (libVLC inside this app)
    port: int | None = None
    media_paths: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"  # attach: the address that instance answers on
    source_uri: str | None = None  # launch/in_app: file:// URI of the .m3u it was started from


class VlcLaunchDialog(QDialog):
    def __init__(
        self, instances: list, media_filter: str, parent: QWidget | None = None,
        *, unmanaged_vlc_running: bool = False, can_launch_vlc: bool = True,
        can_play_in_app: bool = False, prefer_in_app: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open Media")
        self.setMinimumWidth(460)
        self._media_filter = media_filter
        self._media_paths: list[str] = []
        self._source_uri: str | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose how to play: an open VLC window, a new one, or inside this app.", self))

        self._combo = QComboBox(self)
        for instance in instances:
            self._combo.addItem(instance.label, (getattr(instance, "host", "127.0.0.1"), instance.port))
        if can_launch_vlc:
            self._combo.addItem("Launch a new VLC window...", LAUNCH_NEW_SENTINEL)
        if can_play_in_app:
            self._combo.addItem("Play inside this app (no VLC window)...", IN_APP_SENTINEL)
        preferred = IN_APP_SENTINEL if prefer_in_app and can_play_in_app else None
        if not instances or preferred is not None:
            target = preferred if preferred is not None else (
                LAUNCH_NEW_SENTINEL if can_launch_vlc else IN_APP_SENTINEL
            )
            index = self._combo.findData(target)
            self._combo.setCurrentIndex(max(0, index))
        layout.addWidget(self._combo)

        if not instances and unmanaged_vlc_running:
            # A VLC window IS open, but it wasn't started with its web interface, and that
            # can't be switched on from outside -- say so instead of an empty list.
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
        self._media_label = QLabel("No media selected", self)
        self._media_label.setWordWrap(True)
        browse_row.addWidget(self._media_label, 1)
        layout.addLayout(browse_row)

        self._combo.currentIndexChanged.connect(self._update_browse_enabled)
        self._update_browse_enabled()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _is_launch_new_selected(self) -> bool:
        return self._combo.currentData() == LAUNCH_NEW_SENTINEL

    def _is_in_app_selected(self) -> bool:
        return self._combo.currentData() == IN_APP_SENTINEL

    def _update_browse_enabled(self) -> None:
        self._browse_button.setEnabled(self._is_launch_new_selected() or self._is_in_app_selected())

    def _on_browse(self) -> None:
        from bookmark_studio.app.vlc_launcher import resolve_startup_media, startup_playlist_source_uri

        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self, "Select a playlist or media files", "", self._media_filter
        )
        if not paths:
            return
        self._media_paths = resolve_startup_media(paths)
        self._source_uri = startup_playlist_source_uri(paths)
        self._media_label.setText(f"{len(self._media_paths)} media item(s) selected")

    def choice(self) -> VlcLaunchChoice:
        if self._is_in_app_selected():
            return VlcLaunchChoice(mode="in_app", media_paths=self._media_paths, source_uri=self._source_uri)
        if self._is_launch_new_selected():
            return VlcLaunchChoice(mode="launch", media_paths=self._media_paths, source_uri=self._source_uri)
        host, port = self._combo.currentData()
        return VlcLaunchChoice(mode="attach", port=port, host=host)
