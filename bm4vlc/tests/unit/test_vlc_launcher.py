from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PySide6.QtCore import QSettings

from bookmark_studio.app.vlc_launcher import (
    discover_vlc_instances, find_free_http_port, parse_m3u, resolve_startup_media,
)
from bookmark_studio.settings.settings_service import SettingsService


def test_parse_m3u_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u"
    playlist.write_text(
        "#EXTM3U\n#EXTINF:123,Some Song\nsong1.mp3\n\nhttps://example.com/stream.mp3\n",
        encoding="utf-8",
    )
    entries = parse_m3u(playlist)
    assert entries == [str((tmp_path / "song1.mp3").resolve()), "https://example.com/stream.mp3"]


def test_parse_m3u_resolves_relative_entries_against_playlist_directory(tmp_path: Path) -> None:
    subdir = tmp_path / "music"
    subdir.mkdir()
    playlist = tmp_path / "list.m3u8"
    playlist.write_text("music/track.flac\n", encoding="utf-8")
    entries = parse_m3u(playlist)
    assert entries == [str((subdir / "track.flac").resolve())]


def test_resolve_startup_media_expands_a_single_playlist_selection(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u"
    playlist.write_text("a.mp3\nb.mp3\n", encoding="utf-8")
    result = resolve_startup_media([str(playlist)])
    assert result == [str((tmp_path / "a.mp3").resolve()), str((tmp_path / "b.mp3").resolve())]


def test_resolve_startup_media_passes_through_direct_media_selections(tmp_path: Path) -> None:
    files = [str(tmp_path / "a.mp3"), str(tmp_path / "b.wav")]
    assert resolve_startup_media(files) == files


def test_find_free_http_port_returns_preferred_when_free() -> None:
    # Bind briefly just to grab a certainly-free ephemeral port, then release it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert find_free_http_port(free_port) == free_port


def test_find_free_http_port_skips_a_port_already_in_use() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    held_port = holder.getsockname()[1]
    try:
        result = find_free_http_port(held_port)
        assert result != held_port
    finally:
        holder.close()


class _FakeVlcHandler(BaseHTTPRequestHandler):
    """Ignores auth entirely -- discover_vlc_instances only needs reachability plus
    playlist/status shape, not the real password check already covered elsewhere
    (test_playback_adapters.py)."""

    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        if parsed.path == "/requests/status.json":
            body = {
                "state": "playing", "time": 1, "position": 0.1, "rate": 1.0,
                "currentplid": 1, "length": 10,
                "information": {"category": {"meta": {"filename": "song.mp3"}}},
            }
            payload = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
        elif parsed.path == "/requests/playlist.xml":
            xml = b'<node><leaf id="1" uri="file:///song.mp3" name="Song" duration="10"/></node>'
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.end_headers()
            self.wfile.write(xml)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def test_discover_vlc_instances_finds_reachable_port_and_drops_stale_known_port(tmp_path: Path) -> None:
    server = HTTPServer(("127.0.0.1", 0), _FakeVlcHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        reachable_port = server.server_address[1]
        # An unreachable port that's never actually going to answer.
        stale_port = find_free_http_port(reachable_port + 1)

        settings = SettingsService(QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat))
        settings.set_bridge_port(reachable_port)
        settings.add_known_vlc_port(stale_port)

        instances = discover_vlc_instances(settings)

        assert [i.port for i in instances] == [reachable_port]
        assert "song.mp3" in instances[0].label
        # Self-healing: the dead port should have been dropped from settings.
        assert stale_port not in settings.known_vlc_ports()
    finally:
        server.shutdown()
        thread.join(timeout=5)
