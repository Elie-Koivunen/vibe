"""PlaybackAdapter backed by VLC's built-in HTTP interface (spec #28)."""
from __future__ import annotations

from xml.etree import ElementTree

import requests

from bookmark_studio.playback.status import PlaybackStatus, VlcPlaylistItem

STATUS_TIMEOUT_S = 0.5
COMMAND_TIMEOUT_S = 1.0
PLAYLIST_TIMEOUT_S = 1.5


class StandardHttpPlaybackAdapter:
    """Talks to VLC's built-in :status.json / :requests/*.xml HTTP interface (spec #28).

    Coarser than the enhanced Lua bridge: no microsecond seek endpoint, and playlist
    items come back with a fractional-second duration, not microseconds. Used when
    bookmarkstudio.lua isn't installed.
    """

    def __init__(self, host: str, port: int, password: str) -> None:
        self._base_url = f"http://{host}:{port}"
        self._auth = ("", password)
        self._session = requests.Session()

    def connect(self) -> None:
        self._session.get(f"{self._base_url}/requests/status.json", auth=self._auth, timeout=STATUS_TIMEOUT_S)

    def disconnect(self) -> None:
        self._session.close()

    def get_status(self) -> PlaybackStatus:
        response = self._session.get(
            f"{self._base_url}/requests/status.json", auth=self._auth, timeout=STATUS_TIMEOUT_S
        )
        response.raise_for_status()
        data = response.json()
        length_s = data.get("length")
        return PlaybackStatus(
            state=data.get("state", "stopped"),
            time_us=int(data.get("time", 0)) * 1_000_000,
            position=float(data.get("position", 0.0)),
            rate=float(data.get("rate", 1.0)),
            current_playlist_item_id=data.get("currentplid"),
            duration_us=int(length_s) * 1_000_000 if length_s is not None else None,
            media_uri=_extract_media_uri(data),
        )

    def get_playlist(self) -> list[VlcPlaylistItem]:
        response = self._session.get(
            f"{self._base_url}/requests/playlist.xml", auth=self._auth, timeout=PLAYLIST_TIMEOUT_S
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        items: list[VlcPlaylistItem] = []
        for leaf in root.iter("leaf"):
            duration_raw = leaf.get("duration")
            items.append(
                VlcPlaylistItem(
                    vlc_id=int(leaf.get("id", "0")),
                    uri=leaf.get("uri", ""),
                    name=leaf.get("name", ""),
                    duration_s=float(duration_raw) if duration_raw else None,
                )
            )
        return items

    def play(self) -> None:
        self._command("pl_play")

    def pause(self) -> None:
        self._command("pl_forcepause")

    def stop(self) -> None:
        self._command("pl_stop")

    def next_track(self) -> None:
        self._command("pl_next")

    def previous_track(self) -> None:
        self._command("pl_previous")

    def goto_item(self, vlc_id: int) -> None:
        self._command("pl_play", {"id": vlc_id})

    def seek_absolute_us(self, time_us: int) -> None:
        # spec #27: the built-in interface's seek is integer-seconds granularity.
        self._command("seek", {"val": max(0, time_us) // 1_000_000})

    def seek_relative_us(self, delta_us: int) -> None:
        sign = "+" if delta_us >= 0 else "-"
        self._command("seek", {"val": f"{sign}{abs(delta_us) // 1_000_000}S"})

    def set_rate(self, rate: float) -> None:
        self._command("rate", {"val": rate})

    def set_volume(self, level: int) -> None:
        # VLC's built-in interface takes 0-256 (256 = 100%), not a percentage.
        self._command("volume", {"val": max(0, min(256, level))})

    def _command(self, command: str, params: dict | None = None) -> None:
        query = {"command": command, **(params or {})}
        response = self._session.get(
            f"{self._base_url}/requests/status.json",
            params=query,
            auth=self._auth,
            timeout=COMMAND_TIMEOUT_S,
        )
        response.raise_for_status()


def _extract_media_uri(status_json: dict) -> str | None:
    info = status_json.get("information", {})
    category = info.get("category", {})
    meta = category.get("meta", {})
    return meta.get("filename") or meta.get("url")
