"""HTTP client for bookmarkstudio.lua's /bookmarkstudio/v1/ API (spec #22, #108-#109).

Uses a synchronous `requests.Session` rather than QNetworkAccessManager (spec #108's
suggestion). The spec's stated reason for QNetworkAccessManager is "no network calls on
a UI-blocking worker" -- this codebase achieves the same property differently: every
PlaybackAdapter call happens on a background polling thread (never the GUI thread), and
results cross back to Qt via signals, mirroring the waveform pipeline's
thread-boundary discipline (spec #186-#187, see app/waveform_orchestrator.py). A
synchronous client is easier to unit test deterministically than callback-based
QNetworkReply handling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

MIN_PROTOCOL_VERSION = 1
MAX_PROTOCOL_VERSION = 1

HEALTH_TIMEOUT_S = 1.0
STATUS_TIMEOUT_S = 0.5
COMMAND_TIMEOUT_S = 1.0
PLAYLIST_TIMEOUT_S = 1.5


class BridgeError(Exception):
    """Raised on a well-formed {"ok": false, "error": {...}} bridge response (spec #107)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BridgeProtocolMismatch(Exception):
    """Raised when the bridge's protocol_version is outside this client's supported range."""


@dataclass(frozen=True, slots=True)
class BridgeHealth:
    ok: bool
    protocol_version: int
    vlc_version: str
    bridge_version: str


class BridgeClient:
    """Thin JSON client for bookmarkstudio.lua's /bookmarkstudio/v1/ endpoints."""

    def __init__(self, host: str, port: int, token: str) -> None:
        self._base_url = f"http://{host}:{port}/bookmarkstudio/v1"
        self._auth = ("bookmarkstudio", token)
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def health(self) -> BridgeHealth:
        data = self._get("/health", timeout=HEALTH_TIMEOUT_S)
        health = BridgeHealth(
            ok=bool(data["ok"]),
            protocol_version=int(data["protocol_version"]),
            vlc_version=data.get("vlc_version", ""),
            bridge_version=data.get("bridge_version", ""),
        )
        if not (MIN_PROTOCOL_VERSION <= health.protocol_version <= MAX_PROTOCOL_VERSION):
            raise BridgeProtocolMismatch(
                f"bridge protocol_version {health.protocol_version} is outside "
                f"supported range [{MIN_PROTOCOL_VERSION}, {MAX_PROTOCOL_VERSION}]"
            )
        return health

    def status(self) -> dict[str, Any]:
        return self._get("/status", timeout=STATUS_TIMEOUT_S)

    def playlist(self) -> dict[str, Any]:
        return self._get("/playlist", timeout=PLAYLIST_TIMEOUT_S)

    def control(self, command: str, **params: Any) -> None:
        self._get("/control", params={"command": command, **params}, timeout=COMMAND_TIMEOUT_S)

    def seek(self, time_us: int) -> None:
        self._get("/seek", params={"time_us": time_us}, timeout=COMMAND_TIMEOUT_S)

    def set_rate(self, value: float) -> None:
        self._get("/rate", params={"value": value}, timeout=COMMAND_TIMEOUT_S)

    def _get(self, path: str, *, timeout: float, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._session.get(
            f"{self._base_url}{path}", params=params, auth=self._auth, timeout=timeout
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("ok") is False:
            error = data.get("error", {})
            raise BridgeError(error.get("code", "UNKNOWN"), error.get("message", ""))
        return data
