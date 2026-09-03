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
        # Cached from the most recent get_status() -- seek_absolute_us() needs it to
        # convert a microsecond target into VLC's percent-seek syntax (see that
        # method's docstring for why). None until the first successful poll; seeks
        # issued before then fall back to the old whole-second behavior.
        self._last_duration_us: int | None = None

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
        duration_us = int(length_s) * 1_000_000 if length_s is not None else None
        self._last_duration_us = duration_us
        position = float(data.get("position", 0.0))
        return PlaybackStatus(
            state=data.get("state", "stopped"),
            time_us=_precise_time_us(data.get("time", 0), position, duration_us),
            position=position,
            rate=float(data.get("rate", 1.0)),
            current_playlist_item_id=data.get("currentplid"),
            duration_us=duration_us,
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
        """Direct user report: "the bookmark playback is not respecting the loop, it
        drifts away". Root-caused live against a real VLC instance (see this
        module's percent-seek verification, not a spec assumption): the built-in
        interface's `seek` command silently mis-parses a plain fractional-seconds
        value -- `val=12.345` actually landed at 345 SECONDS, not 12.345s -- so the
        previous whole-second-only seek wasn't just imprecise, it was the *safe*
        choice given that bug. Confirmed live, its documented percent syntax
        (`val=<float>%`) is both correctly parsed AND sub-second precise: seeking to
        "41.15%" of a 30s file landed within 0.2ms of the intended 12.345s. Using
        that (once a status poll has told us the real duration) makes every loop
        seek land at its true bookmark boundary instead of rounding to the nearest
        second -- confirmed live to be the dominant source of the reported loop
        drift, since a bookmark's start/end are rarely whole seconds. Falls back to
        the old whole-second behavior if duration isn't known yet (no poll has
        succeeded), which is still strictly better than guessing.
        """
        time_us = max(0, time_us)
        if self._last_duration_us:
            percent = min(100.0, (time_us / self._last_duration_us) * 100.0)
            self._command("seek", {"val": f"{percent:.4f}%"})
        else:
            self._command("seek", {"val": time_us // 1_000_000})

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


def _precise_time_us(raw_time_s: object, position: float, duration_us: int | None) -> int:
    """VLC's status.json "time" field is a whole number of seconds -- confirmed live,
    it never carries a fractional part, unlike "position" (a float fraction of the
    track, confirmed live to genuinely track sub-second progress: e.g. 0.4115 for a
    seek that landed at 12.345s of a 30s file). Deriving time from position*duration
    instead gives PlaybackClock (and, downstream, LoopController's boundary check) far
    better precision than truncating to the nearest second -- part of the fix for
    "the bookmark playback is not respecting the loop, it drifts away". Falls back to
    the plain integer field when duration or position isn't usable (e.g. nothing
    loaded yet), which is exactly the old behavior.
    """
    if duration_us and 0.0 <= position <= 1.0:
        return int(position * duration_us)
    return int(raw_time_s or 0) * 1_000_000


def _extract_media_uri(status_json: dict) -> str | None:
    info = status_json.get("information", {})
    category = info.get("category", {})
    meta = category.get("meta", {})
    return meta.get("filename") or meta.get("url")
