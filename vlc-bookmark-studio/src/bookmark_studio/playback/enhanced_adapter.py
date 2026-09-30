"""PlaybackAdapter backed by the custom Lua bridge (microsecond seek, spec #27)."""
from __future__ import annotations

import time

from bookmark_studio.domain.equalizer import VLC_BANDS_HZ, EqualizerSettings
from bookmark_studio.playback.bridge_client import BridgeClient
from bookmark_studio.playback.status import PlaybackStatus, VlcPlaylistItem

GOTO_SETTLE_TIMEOUT_S = 2.0
_GOTO_POLL_INTERVAL_S = 0.05


class EnhancedLuaPlaybackAdapter:
    """Uses bookmarkstudio.lua's JSON bridge for microsecond-precision transport (spec #27)."""

    def __init__(self, client: BridgeClient, *, goto_settle_timeout_s: float = GOTO_SETTLE_TIMEOUT_S) -> None:
        self._client = client
        self._goto_settle_timeout_s = goto_settle_timeout_s

    def connect(self) -> None:
        self._client.health()

    def disconnect(self) -> None:
        self._client.close()

    def get_status(self) -> PlaybackStatus:
        data = self._client.status()
        duration_us = data.get("duration_us")
        return PlaybackStatus(
            state=data.get("state", "stopped"),
            time_us=int(data.get("time_us") or 0),
            position=float(data.get("position") or 0.0),
            rate=float(data.get("rate") or 1.0),
            current_playlist_item_id=data.get("current_playlist_item_id"),
            duration_us=int(duration_us) if duration_us and duration_us > 0 else None,
            media_uri=data.get("media_uri"),
            # Without this, every status read 256 (the dataclass default) and a fade
            # always ramped to 100% regardless of the user's real volume.
            volume=int(data.get("volume", 256)),
        )

    def get_playlist(self) -> list[VlcPlaylistItem]:
        data = self._client.playlist()
        return [
            VlcPlaylistItem(
                vlc_id=item["vlc_id"],
                uri=item["uri"],
                name=item["name"],
                duration_s=item.get("duration_s") if (item.get("duration_s") or 0) > 0 else None,
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
        # vlc.playlist.goto() only queues the switch; wait until the new item is
        # actually current so a following seek lands in the right song.
        deadline = time.monotonic() + self._goto_settle_timeout_s
        while time.monotonic() < deadline:
            try:
                data = self._client.status()
            except Exception:  # noqa: BLE001 - best effort; the command itself was sent
                return
            if data.get("current_playlist_item_id") == vlc_id and (data.get("duration_us") or 0) > 0:
                return
            time.sleep(_GOTO_POLL_INTERVAL_S)

    def seek_absolute_us(self, time_us: int) -> None:
        self._client.seek(time_us)

    def seek_relative_us(self, delta_us: int) -> None:
        current = self.get_status().time_us
        self._client.seek(max(0, current + delta_us))

    def set_rate(self, rate: float) -> None:
        self._client.set_rate(rate)

    def set_volume(self, level: int) -> None:
        self._client.control("volume", val=max(0, min(512, int(level))))

    def set_exact_duration(self, vlc_id: int, duration_us: int | None) -> None:
        """No-op: the bridge already reports microsecond time and length."""

    # The Lua bridge has no equalizer commands.
    supports_equalizer = False
    equalizer_band_hz = VLC_BANDS_HZ

    def set_equalizer(self, settings: EqualizerSettings, *, full: bool = False) -> None:
        raise NotImplementedError("the Lua bridge has no equalizer")
