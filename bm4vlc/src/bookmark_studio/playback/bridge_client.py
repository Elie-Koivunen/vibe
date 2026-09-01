"""HTTP client for bookmarkstudio.lua's /bookmarkstudio/v1/ API (spec #22, #108-#109).

Uses a raw socket instead of `requests`/`http.client`, and a synchronous call instead
of QNetworkAccessManager (spec #108's suggestion) -- this codebase achieves "no network
calls on a UI-blocking worker" differently, since every PlaybackAdapter call happens on
a background polling thread and results cross back to Qt via signals (spec #186-#187,
see app/waveform_orchestrator.py).

The raw socket is not a style choice: verified live against a real VLC 3.0.23,
`vlc.httpd():handler()`'s response is NOT RFC 7230 compliant -- it sends a bare status
line (`HTTP/1.0 200 OK\r\n`) immediately followed by the body, with no header-terminating
blank line and no Content-Length. Every standard client (`requests`, `http.client`,
`curl`) hangs indefinitely waiting for headers that will never arrive, even though the
correct, complete body is actually delivered in a single packet within milliseconds.
`_get()` reads raw bytes until a short idle gap instead of waiting for a header
terminator, and tolerates a real header block if the peer sends one (VLC's built-in
HTTP interface, and every test fixture in tests/unit/test_playback_adapters.py, both
use a real, RFC-compliant httpd -- this same code path handles both).

THE CONNECTION IS PERSISTENT AND REUSED ACROSS CALLS -- this is not an optimization,
it is required for correctness. Verified live: `vlc.httpd():handler()` never closes its
side of a connection after responding (confirmed via `netstat`: every request-per-call
design left the socket in CLOSE_WAIT on VLC's side, a server-side leak). At this app's
normal status-polling cadence (every ~150ms) that leaked several hundred sockets within
minutes of real use, after which VLC stopped accepting new connections at all --
the exact "it just doesn't work" failure mode a live user session hit. Reusing one
connection for the client's whole lifetime, confirmed live, leaves zero leaked sockets
no matter how many requests are made. A `threading.Lock` serializes access since
multiple requests can be dispatched from different QThreadPool workers concurrently
(app/application.py) and this transport has no way to distinguish interleaved
request/response bytes from two requests in flight at once.
"""
from __future__ import annotations

import base64
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

MIN_PROTOCOL_VERSION = 1
MAX_PROTOCOL_VERSION = 1


# spec #109 suggests 0.5-1.5s here; verified live these are unrealistic for VLC's
# actual Lua httpd. It runs on VLC's single-threaded interface loop, which is not
# always free to service an HTTP request immediately -- real requests sometimes took
# several seconds to get a response even though a *typical* one arrives in well under
# 100ms. Every timeout this client hits forces a reconnect (see class docstring), which
# leaks a socket on VLC's side; the original spec-sized timeouts made this common
# enough that a live session degraded to fully broken within about 15 seconds of normal
# ~150ms status polling. These wider budgets trade a slower worst-case UI update for a
# dramatically lower reconnect (and thus leak) rate -- confirmed live.
HEALTH_TIMEOUT_S = 3.0
STATUS_TIMEOUT_S = 3.0
COMMAND_TIMEOUT_S = 3.0
PLAYLIST_TIMEOUT_S = 4.0

# How long to wait, after the most recent chunk, before deciding the response is
# complete. VLC's malformed response arrives in one shot; this just needs to be
# comfortably longer than that single round trip, not longer than a real timeout.
_IDLE_GAP_S = 0.3


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
    """Thin JSON client for bookmarkstudio.lua's /bookmarkstudio/v1/ endpoints.

    Holds one persistent, lazily-(re)connected socket for its whole lifetime -- see the
    module docstring for why that's required, not optional. Thread-safe: a lock
    serializes concurrent callers onto that single connection.
    """

    def __init__(self, host: str, port: int, token: str) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

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
        request = self._build_request(target)

        with self._lock:
            raw = self._request_locked(request, target, timeout)

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

    def _build_request(self, target: str) -> bytes:
        auth = base64.b64encode(f"bookmarkstudio:{self._token}".encode()).decode()
        # No "Connection: close": the whole point is to keep this connection alive
        # across many calls (see module docstring).
        return (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            f"Authorization: Basic {auth}\r\n\r\n"
        ).encode()

    def _request_locked(self, request: bytes, target: str, timeout: float) -> bytes:
        """Caller holds self._lock. Tries the existing connection first; on any
        failure, reconnects once and retries -- the shared connection can go stale
        (VLC restarted, network hiccup) without this client finding out until it tries
        to use it.
        """
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                if self._sock is None:
                    self._sock = socket.create_connection((self._host, self._port), timeout=timeout)
                return _send_and_read(self._sock, request, target, timeout)
            except OSError as exc:
                last_error = exc
                self._close_locked()
        raise BridgeConnectionError(f"cannot reach {self._host}:{self._port}{target}: {last_error}")


def _send_and_read(sock: socket.socket, request: bytes, target: str, timeout: float) -> bytes:
    """sendall() is the actual staleness probe: if a previous response left the shared
    socket half-dead (e.g. a non-VLC RFC-compliant peer that closes after one
    response -- see the test fixtures), the write itself raises (broken pipe/reset),
    which the caller (_request_locked) catches and reconnects on. Here, a clean peer
    close mid-read (chunk == b"") is a NORMAL end-of-response signal, same as before
    this class started reusing connections -- not every peer is VLC, which never
    closes at all; a well-behaved one without Content-Length closes to mark the end.
    """
    sock.sendall(request)
    sock.settimeout(min(_IDLE_GAP_S, timeout))
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
        except (socket.timeout, TimeoutError):
            if chunks:
                break  # got a full burst, then a quiet gap -- treat as done
            continue  # nothing yet at all; keep waiting until the deadline
        if not chunk:
            break  # peer closed after responding -- a valid end-of-response signal
        chunks.append(chunk)

    if not chunks:
        raise OSError(f"no response from {target} within {timeout}s")
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
