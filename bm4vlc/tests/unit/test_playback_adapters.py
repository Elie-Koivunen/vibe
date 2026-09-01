from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from bookmark_studio.playback.bridge_client import BridgeClient, BridgeError, BridgeHTTPError
from bookmark_studio.playback.enhanced_adapter import EnhancedLuaPlaybackAdapter
from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter

BRIDGE_TOKEN = "secret-token"
VLC_PASSWORD = "vlc-password"


class _BridgeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        expected_auth = "Basic " + base64.b64encode(f"bookmarkstudio:{BRIDGE_TOKEN}".encode()).decode()
        if self.headers.get("Authorization") != expected_auth:
            self.send_response(401)
            self.end_headers()
            return

        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/bookmarkstudio/v1/health":
            body = {"ok": True, "protocol_version": 1, "vlc_version": "3.0.20", "bridge_version": "1.0.0"}
        elif parsed.path == "/bookmarkstudio/v1/status":
            body = {
                "state": "playing", "time_us": 12345, "position": 0.1, "rate": 1.0,
                "current_playlist_item_id": 7, "duration_us": 999999, "media_uri": "file:///x.mp3",
            }
        elif parsed.path == "/bookmarkstudio/v1/playlist":
            body = {"current_id": 7, "items": [{"vlc_id": 7, "uri": "file:///x.mp3", "name": "X", "duration_s": 12.3}]}
        elif parsed.path == "/bookmarkstudio/v1/control":
            body = {"ok": True}
        elif parsed.path == "/bookmarkstudio/v1/seek":
            time_us = int(qs.get("time_us", ["-1"])[0])
            body = (
                {"ok": False, "error": {"code": "INVALID_TIME", "message": "time_us must be non-negative"}}
                if time_us < 0
                else {"ok": True}
            )
        elif parsed.path == "/bookmarkstudio/v1/rate":
            body = {"ok": True}
        else:
            self.send_response(404)
            self.end_headers()
            return

        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # silence test output
        pass


class _VlcHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        expected_auth = "Basic " + base64.b64encode(f":{VLC_PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != expected_auth:
            self.send_response(401)
            self.end_headers()
            return

        parsed = urlparse(self.path)
        if parsed.path == "/requests/status.json":
            body = {
                "state": "playing", "time": 5, "position": 0.5, "rate": 1.0,
                "currentplid": 3, "length": 10,
                "information": {"category": {"meta": {"filename": "song.mp3"}}},
            }
            payload = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
        elif parsed.path == "/requests/playlist.xml":
            xml = (
                b'<node><leaf id="3" uri="file:///song.mp3" name="Song" duration="10"/></node>'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.end_headers()
            self.wfile.write(xml)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def _start_server(handler_cls: type[BaseHTTPRequestHandler]) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture()
def bridge_server():
    server, thread = _start_server(_BridgeHandler)
    yield server.server_address
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def vlc_server():
    server, thread = _start_server(_VlcHandler)
    yield server.server_address
    server.shutdown()
    thread.join(timeout=5)


def test_bridge_client_health_and_status(bridge_server) -> None:
    host, port = bridge_server
    client = BridgeClient(host, port, BRIDGE_TOKEN)
    health = client.health()
    assert health.ok
    assert health.protocol_version == 1
    status = client.status()
    assert status["time_us"] == 12345


def test_bridge_client_rejects_wrong_token(bridge_server) -> None:
    host, port = bridge_server
    client = BridgeClient(host, port, "wrong-token")
    with pytest.raises(BridgeHTTPError):
        client.health()


def test_bridge_client_raises_bridge_error_on_invalid_time(bridge_server) -> None:
    host, port = bridge_server
    client = BridgeClient(host, port, BRIDGE_TOKEN)
    with pytest.raises(BridgeError) as exc_info:
        client.seek(-1)
    assert exc_info.value.code == "INVALID_TIME"


def test_enhanced_adapter_full_roundtrip(bridge_server) -> None:
    host, port = bridge_server
    client = BridgeClient(host, port, BRIDGE_TOKEN)
    adapter = EnhancedLuaPlaybackAdapter(client)
    adapter.connect()

    status = adapter.get_status()
    assert status.time_us == 12345
    assert status.media_uri == "file:///x.mp3"

    playlist = adapter.get_playlist()
    assert playlist[0].name == "X"
    assert playlist[0].vlc_id == 7

    adapter.play()
    adapter.pause()
    adapter.stop()
    adapter.seek_absolute_us(5000)
    adapter.set_rate(1.5)  # none of these should raise


def test_standard_http_adapter_reads_status_and_playlist(vlc_server) -> None:
    host, port = vlc_server
    adapter = StandardHttpPlaybackAdapter(host, port, VLC_PASSWORD)

    status = adapter.get_status()
    assert status.state == "playing"
    assert status.time_us == 5_000_000  # 5 seconds, converted to microseconds
    assert status.duration_us == 10_000_000
    assert status.media_uri == "song.mp3"

    playlist = adapter.get_playlist()
    assert len(playlist) == 1
    assert playlist[0].name == "Song"
    assert playlist[0].duration_s == 10.0


class _VlcStyleNeverClosingHandler(BaseHTTPRequestHandler):
    """Mimics VLC's actual httpd():handler() behavior (verified live, see
    bridge_client.py's module docstring): a bare status line immediately followed by
    the body, no header terminator, no Content-Length -- and it never closes the
    connection, so a client MUST reuse it or leak a socket per request.
    """

    protocol_version = "HTTP/1.1"  # keep the underlying socket open between requests

    def do_GET(self) -> None:  # noqa: N802
        _VlcStyleNeverClosingHandler.request_count += 1
        _VlcStyleNeverClosingHandler.client_ports.add(self.client_address[1])
        body = json.dumps({"ok": True, "protocol_version": 1, "vlc_version": "x", "bridge_version": "x"})
        # Deliberately malformed like real VLC: status line + body, no blank line.
        self.wfile.write(f"HTTP/1.0 200 OK\r\n{body}".encode())

    def log_message(self, *args: object) -> None:
        pass


def test_bridge_client_reuses_one_connection_across_many_requests(qtbot) -> None:
    """Regression: an earlier per-call-socket design left VLC's real httpd leaking a
    connection into CLOSE_WAIT on every single request -- confirmed live via netstat,
    several hundred accumulate within minutes of normal ~150ms status polling, after
    which VLC stops accepting new connections at all. This is the actual, most severe
    root cause behind a live user session going from "works" to "just doesn't work"."""
    _VlcStyleNeverClosingHandler.request_count = 0
    _VlcStyleNeverClosingHandler.client_ports = set()
    server = HTTPServer(("127.0.0.1", 0), _VlcStyleNeverClosingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        client = BridgeClient(host, port, "any-token")
        for _ in range(10):
            client.health()
        assert _VlcStyleNeverClosingHandler.request_count == 10
        # A new TCP connection per request would show 10 distinct ephemeral client
        # ports; connection reuse shows exactly one.
        assert len(_VlcStyleNeverClosingHandler.client_ports) == 1
        client.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_standard_http_adapter_transport_commands_do_not_raise(vlc_server) -> None:
    host, port = vlc_server
    adapter = StandardHttpPlaybackAdapter(host, port, VLC_PASSWORD)
    adapter.play()
    adapter.pause()
    adapter.stop()
    adapter.next_track()
    adapter.previous_track()
    adapter.seek_absolute_us(3_000_000)
    adapter.set_rate(1.25)
