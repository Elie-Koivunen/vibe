"""HTTP client for bookmarkstudio.lua's /bookmarkstudio/v1/ API (spec #22, #108-#109).

Uses a raw socket instead of `requests`/`http.client`, and a synchronous call instead
of QNetworkAccessManager (spec #108's suggestion) -- this codebase achieves "no network
calls on a UI-blocking worker" differently, since every PlaybackAdapter call happens on
a background polling thread and results cross back to Qt via signals (spec #186-#187,
see app/waveform_orchestrator.py).

The raw socket is not a style choice: verified live against a real VLC 3.0.23,
`vlc.httpd():handler()`'s response is NOT RFC 7230 compliant -- it sends a bare status
line (`HTTP/1.0 200 OK\r\n`) immediately followed by the body, with no header-terminating
blank line, no Content-Length, and no Connection: close (the socket is never closed by
the server either). Every standard client (`requests`, `http.client`, `curl`) hangs
indefinitely waiting for headers that will never arrive, even though the correct,
complete body is actually delivered in a single packet within milliseconds. `_get()`
below reads raw bytes until a short idle gap instead of waiting for a header terminator
or connection close, and tolerates a real header block if the peer sends one (VLC's
built-in HTTP interface, and every test fixture in tests/unit/test_playback_adapters.py,
both use a real, RFC-compliant httpd -- this same code path handles both.
"""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

MIN_PROTOCOL_VERSION = 1
MAX_PROTOCOL_VERSION = 1

HEALTH_TIMEOUT_S = 1.0
STATUS_TIMEOUT_S = 0.5
COMMAND_TIMEOUT_S = 1.0
PLAYLIST_TIMEOUT_S = 1.5

# How long to wait, after the most recent chunk, before deciding the response is
# complete. VLC's malformed response arrives in one shot; this just needs to be
# comfortably longer than that single round trip, not longer than a real timeout.
_IDLE_GAP_S = 0.2


class BridgeError(Exception):
    """Raised on a well-formed {"ok": false, "error": {...}} bridge response (spec #107)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BridgeHTTPError(Exception):
    """Raised on a non-2xx HTTP status (e.g. 401 for a bad token)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class BridgeConnectionError(Exception):
    """Raised when the bridge can't be reached or the response can't be parsed."""


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
        self._host = host
        self._port = port
        self._token = token

    def close(self) -> None:
        pass  # no persistent connection to close; kept for API parity with callers

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
        query = f"?{urlencode(params)}" if params else ""
        target = f"/bookmarkstudio/v1{path}{query}"
        raw = _raw_http_get(
            self._host, self._port, target, username="bookmarkstudio", password=self._token, timeout=timeout
        )
        status_code, body = _split_status_and_body(raw)
        if status_code >= 400:
            raise BridgeHTTPError(status_code)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise BridgeConnectionError(f"non-JSON response from bridge: {body[:200]!r}") from exc
        if isinstance(data, dict) and data.get("ok") is False:
            error = data.get("error", {})
            raise BridgeError(error.get("code", "UNKNOWN"), error.get("message", ""))
        return data


def _raw_http_get(
    host: str, port: int, target: str, *, username: str, password: str, timeout: float
) -> bytes:
    import base64
    import time

    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = (
        f"GET {target} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Authorization: Basic {auth}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise BridgeConnectionError(f"cannot connect to {host}:{port}: {exc}") from exc

    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    try:
        sock.sendall(request)
        sock.settimeout(min(_IDLE_GAP_S, timeout))
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                if chunks:
                    break  # got a full burst, then a quiet gap -- treat as done
                continue  # nothing yet at all; keep waiting until the deadline
            if not chunk:
                break  # peer closed the connection -- a well-behaved server
            chunks.append(chunk)
    finally:
        sock.close()

    if not chunks:
        raise BridgeConnectionError(f"no response from {host}:{port}{target} within {timeout}s")
    return b"".join(chunks)


def _split_status_and_body(raw: bytes) -> tuple[int, str]:
    """Tolerates both a real RFC-compliant header block and VLC's bare status line."""
    header_end = raw.find(b"\r\n\r\n")
    if header_end != -1:
        head, body = raw[:header_end], raw[header_end + 4 :]
    else:
        line_end = raw.find(b"\r\n")
        head, body = (raw[:line_end], raw[line_end + 2 :]) if line_end != -1 else (raw, b"")

    status_line = head.split(b"\r\n", 1)[0]
    parts = status_line.split(b" ", 2)
    status_code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 200
    return status_code, body.decode("utf-8", errors="replace")
