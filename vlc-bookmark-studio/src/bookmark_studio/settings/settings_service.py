"""Reads/writes QSettings: window geometry, theme, VLC path, bridge port/token (spec #120)."""
from __future__ import annotations

import secrets

from PySide6.QtCore import QByteArray, QSettings

ORGANIZATION = "VLCBookmarkStudio"
APPLICATION = "VLCBookmarkStudio"
# Where the settings lived before 0.4.0; copied over once, never removed.
_LEGACY_LOCATION = ("BookmarkStudio", "VLCBookmarkStudio")

DEFAULT_BRIDGE_PORT = 43119


def _bytes_or_none(value: object) -> QByteArray | None:
    return value if isinstance(value, QByteArray) else None


def _adopt_legacy_settings(settings: QSettings, legacy: QSettings) -> None:
    """First start after the rename: bring the old settings (VLC path, window layout,
    sync folder, ...) along. Only while the new location is still empty."""
    if settings.allKeys() or not legacy.allKeys():
        return
    for key in legacy.allKeys():
        settings.setValue(key, legacy.value(key))
    settings.sync()


class SettingsService:
    """Small preferences only (spec #120) -- bookmark data itself lives in SQLite, never here."""

    def __init__(self, settings: QSettings | None = None) -> None:
        if settings is None:
            settings = QSettings(ORGANIZATION, APPLICATION)
            _adopt_legacy_settings(settings, QSettings(*_LEGACY_LOCATION))
        self._settings = settings

    # -- window/UI state --

    def window_geometry(self) -> QByteArray | None:
        return _bytes_or_none(self._settings.value("window/geometry"))

    def set_window_geometry(self, geometry: QByteArray) -> None:
        self._settings.setValue("window/geometry", geometry)

    def splitter_state(self, name: str) -> QByteArray | None:
        return _bytes_or_none(self._settings.value(f"splitters/{name}"))

    def set_splitter_state(self, name: str, state: QByteArray) -> None:
        self._settings.setValue(f"splitters/{name}", state)

    def theme(self) -> str:
        return str(self._settings.value("appearance/theme", "system") or "system")

    def set_theme(self, theme: str) -> None:
        if theme not in ("system", "light", "dark"):
            raise ValueError(f"unknown theme {theme!r}")
        self._settings.setValue("appearance/theme", theme)

    # -- VLC integration --

    def vlc_path(self) -> str | None:
        return self._settings.value("vlc/path") or None

    def set_vlc_path(self, path: str) -> None:
        self._settings.setValue("vlc/path", path)

    def ffmpeg_path(self) -> str | None:
        return self._settings.value("ffmpeg/path") or None

    def set_ffmpeg_path(self, path: str) -> None:
        self._settings.setValue("ffmpeg/path", path)

    def libvlc_dir(self) -> str | None:
        """Folder of a (64-bit) VLC whose libVLC the in-app player should use."""
        return self._settings.value("libvlc/dir") or None

    def set_libvlc_dir(self, path: str) -> None:
        self._settings.setValue("libvlc/dir", path)

    def playback_backend(self) -> str:
        """'http' (a VLC window driven over HTTP) or 'libvlc' (the in-app player): what
        the launch dialog preselects. The last choice is remembered."""
        value = str(self._settings.value("playback/backend", "http") or "http")
        return value if value in ("http", "libvlc") else "http"

    def set_playback_backend(self, backend: str) -> None:
        if backend not in ("http", "libvlc"):
            raise ValueError(f"unknown playback backend {backend!r}")
        self._settings.setValue("playback/backend", backend)

    def sync_dir(self) -> str | None:
        return self._settings.value("sync/dir") or None

    def set_sync_dir(self, path: str | None) -> None:
        if path:
            self._settings.setValue("sync/dir", path)
        else:
            self._settings.remove("sync/dir")

    def bridge_port(self) -> int:
        try:
            return int(str(self._settings.value("bridge/port", DEFAULT_BRIDGE_PORT)))
        except ValueError:  # a hand-edited settings file
            return DEFAULT_BRIDGE_PORT

    def set_bridge_port(self, port: int) -> None:
        self._settings.setValue("bridge/port", port)

    def known_vlc_ports(self) -> list[int]:
        """Ports of VLC instances this app has itself launched with a distinct HTTP
        port (see vlc_launcher.find_free_http_port) -- lets the "attach to a running
        VLC" picker find them again later, including across app restarts, without
        needing to enumerate OS processes. Entries are self-healing: discover_vlc_
        instances() (bootstrap.py) drops any port that no longer answers.
        """
        raw = self._settings.value("vlc/known_ports", "")
        if not raw:
            return []
        return sorted({int(p) for p in str(raw).split(",") if p.strip().isdigit()})

    def add_known_vlc_port(self, port: int) -> None:
        ports = set(self.known_vlc_ports())
        ports.add(port)
        self._settings.setValue("vlc/known_ports", ",".join(str(p) for p in sorted(ports)))

    def remove_known_vlc_port(self, port: int) -> None:
        ports = set(self.known_vlc_ports())
        ports.discard(port)
        self._settings.setValue("vlc/known_ports", ",".join(str(p) for p in sorted(ports)))

    def bridge_token(self) -> str:
        """Generates a random per-install token on first access (spec #21)."""
        token = str(self._settings.value("bridge/token") or "")
        if not token:
            token = secrets.token_urlsafe(32)
            self._settings.setValue("bridge/token", token)
        return token

    # -- keyboard shortcuts (spec #84) --

    def shortcut(self, action_name: str, default: str) -> str:
        return str(self._settings.value(f"shortcuts/{action_name}", default) or default)

    def set_shortcut(self, action_name: str, sequence: str) -> None:
        self._settings.setValue(f"shortcuts/{action_name}", sequence)

    def sync(self) -> None:
        self._settings.sync()
