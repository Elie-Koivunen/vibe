"""PlaybackAdapter backed by the custom Lua bridge (microsecond seek, spec #27)."""
from __future__ import annotations

from bookmark_studio.playback.bridge_client import BridgeClient
from bookmark_studio.playback.status import PlaybackStatus, VlcPlaylistItem


class EnhancedLuaPlaybackAdapter:
    """Uses bookmarkstudio.lua's JSON bridge for microsecond-precision transport (spec #27)."""

    def __init__(self, client: BridgeClient) -> None:
        self._client = client

    def connect(self) -> None:
        self._client.health()

    def disconnect(self) -> None:
        self._client.close()

    def get_status(self) -> PlaybackStatus:
        data = self._client.status()
        return PlaybackStatus(
            state=data["state"],
            time_us=int(data["time_us"]),
            position=float(data["position"]),
            rate=float(data["rate"]),
            current_playlist_item_id=data.get("current_playlist_item_id"),
            duration_us=data.get("duration_us"),
            media_uri=data.get("media_uri"),
        )

    def get_playlist(self) -> list[VlcPlaylistItem]:
        data = self._client.playlist()
        return [
            VlcPlaylistItem(
                vlc_id=item["vlc_id"],
                uri=item["uri"],
                name=item["name"],
                duration_s=item.get("duration_s"),
            )
            for item in data.get("items", [])
        ]

    def play(self) -> None:
        self._client.control("play")

    def pause(self) -> None:
        self._client.control("pause")

    def stop(self) -> None:
        self._client.control("stop")

    def next_track(self) -> None:
        self._client.control("next")

    def previous_track(self) -> None:
        self._client.control("previous")

    def goto_item(self, vlc_id: int) -> None:
        self._client.control("goto", id=vlc_id)

    def seek_absolute_us(self, time_us: int) -> None:
        self._client.seek(time_us)

    def seek_relative_us(self, delta_us: int) -> None:
        current = self.get_status().time_us
        self._client.seek(max(0, current + delta_us))

    def set_rate(self, rate: float) -> None:
        self._client.set_rate(rate)
