"""Reads/writes QSettings: window geometry, theme, VLC path, bridge port/token (spec #120)."""
from __future__ import annotations

import secrets

from PySide6.QtCore import QByteArray, QSettings

ORGANIZATION = "BookmarkStudio"
APPLICATION = "VLCBookmarkStudio"

DEFAULT_BRIDGE_PORT = 43119


class SettingsService:
    """Small preferences only (spec #120) -- bookmark data itself lives in SQLite, never here."""

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings or QSettings(ORGANIZATION, APPLICATION)

    # -- window/UI state --

    def window_geometry(self) -> QByteArray | None:
        return self._settings.value("window/geometry")

    def set_window_geometry(self, geometry: QByteArray) -> None:
        self._settings.setValue("window/geometry", geometry)

    def splitter_state(self, name: str) -> QByteArray | None:
        return self._settings.value(f"splitters/{name}")

    def set_splitter_state(self, name: str, state: QByteArray) -> None:
        self._settings.setValue(f"splitters/{name}", state)

    def theme(self) -> str:
        return self._settings.value("appearance/theme", "system")

    def set_theme(self, theme: str) -> None:
        if theme not in ("system", "light", "dark"):
            raise ValueError(f"unknown theme {theme!r}")
        self._settings.setValue("appearance/theme", theme)

    # -- VLC integration --

    def vlc_path(self) -> str | None:
        return self._settings.value("vlc/path") or None

    def set_vlc_path(self, path: str) -> None:
        self._settings.setValue("vlc/path", path)

    def bridge_port(self) -> int:
        return int(self._settings.value("bridge/port", DEFAULT_BRIDGE_PORT))

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
        token = self._settings.value("bridge/token")
        if not token:
            token = secrets.token_urlsafe(32)
            self._settings.setValue("bridge/token", token)
        return token

    # -- keyboard shortcuts (spec #84) --

    def shortcut(self, action_name: str, default: str) -> str:
        return self._settings.value(f"shortcuts/{action_name}", default)

    def set_shortcut(self, action_name: str, sequence: str) -> None:
        self._settings.setValue(f"shortcuts/{action_name}", sequence)

    def sync(self) -> None:
        self._settings.sync()
