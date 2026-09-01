"""MockPlaybackAdapter: in-memory PlaybackAdapter for tests and UI automation (spec #30)."""
from __future__ import annotations

from bookmark_studio.playback.status import PlaybackStatus, VlcPlaylistItem


class MockPlaybackAdapter:
    """Deterministic stand-in for real VLC. Time only advances via `advance_time_us`,
    never a wall clock, so tests are reproducible.
    """

    def __init__(self, playlist: list[VlcPlaylistItem] | None = None) -> None:
        self._playlist: list[VlcPlaylistItem] = list(playlist or [])
        self._current_index: int | None = 0 if self._playlist else None
        self._state = "stopped"
        self._time_us = 0
        self._rate = 1.0
        self._volume = 256
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def get_status(self) -> PlaybackStatus:
        item = self._current_item()
        duration_us = int(item.duration_s * 1_000_000) if item and item.duration_s is not None else None
        position = self._time_us / duration_us if duration_us else 0.0
        return PlaybackStatus(
            state=self._state,
            time_us=self._time_us,
            position=position,
            rate=self._rate,
            current_playlist_item_id=item.vlc_id if item else None,
            duration_us=duration_us,
            media_uri=item.uri if item else None,
        )

    def get_playlist(self) -> list[VlcPlaylistItem]:
        return list(self._playlist)

    def play(self) -> None:
        if self._current_index is not None:
            self._state = "playing"

    def pause(self) -> None:
        if self._state == "playing":
            self._state = "paused"

    def stop(self) -> None:
        self._state = "stopped"
        self._time_us = 0

    def next_track(self) -> None:
        if self._current_index is None or not self._playlist:
            return
        if self._current_index + 1 < len(self._playlist):
            self._current_index += 1
            self._time_us = 0

    def previous_track(self) -> None:
        if self._current_index is None or not self._playlist:
            return
        if self._current_index > 0:
            self._current_index -= 1
            self._time_us = 0

    def goto_item(self, vlc_id: int) -> None:
        for index, item in enumerate(self._playlist):
            if item.vlc_id == vlc_id:
                self._current_index = index
                self._time_us = 0
                return
        raise ValueError(f"unknown vlc playlist item id {vlc_id}")

    def seek_absolute_us(self, time_us: int) -> None:
        self._time_us = max(0, time_us)

    def seek_relative_us(self, delta_us: int) -> None:
        self._time_us = max(0, self._time_us + delta_us)

    def set_rate(self, rate: float) -> None:
        self._rate = rate

    def set_volume(self, level: int) -> None:
        self._volume = level

    # -- test-only helpers, not part of the PlaybackAdapter protocol --

    def advance_time_us(self, delta_us: int) -> None:
        if self._state == "playing":
            self._time_us += int(delta_us * self._rate)

    def set_playlist(self, playlist: list[VlcPlaylistItem]) -> None:
        self._playlist = list(playlist)
        self._current_index = 0 if self._playlist else None
        self._time_us = 0

    def _current_item(self) -> VlcPlaylistItem | None:
        if self._current_index is None or not (0 <= self._current_index < len(self._playlist)):
            return None
        return self._playlist[self._current_index]
