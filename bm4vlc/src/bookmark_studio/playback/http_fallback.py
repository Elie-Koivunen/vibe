"""PlaybackAdapter backed by VLC's built-in HTTP interface (spec #28)."""
from __future__ import annotations

import threading
import time
from xml.etree import ElementTree

import requests

from bookmark_studio.playback.status import PlaybackStatus, VlcPlaylistItem

STATUS_TIMEOUT_S = 0.5
COMMAND_TIMEOUT_S = 1.0
PLAYLIST_TIMEOUT_S = 1.5
# pl_play&id=<X> only *queues* the switch inside VLC; the new input opens a moment
# later. goto_item() waits (on the calling worker thread) until VLC reports the new
# item as current, so a seek sent right after it lands in the right song.
GOTO_SETTLE_TIMEOUT_S = 2.0
_GOTO_POLL_INTERVAL_S = 0.05
EXACT_DURATION_TOLERANCE_US = 1_500_000


class StandardHttpPlaybackAdapter:
    """Talks to VLC's built-in :status.json / :requests/*.xml HTTP interface (spec #28).

    Coarser than the enhanced Lua bridge: no microsecond seek endpoint, and VLC reports
    time/length in whole seconds only. Sub-second precision comes from VLC's fractional
    "position" (0..1) combined with the best duration known for the current item: an
    exact one from the decoded waveform (set_exact_duration), else VLC's whole-second
    length. Safe to call from several threads at once: each thread gets its own
    requests.Session.
    """

    def __init__(self, host: str, port: int, password: str, *, timeout_scale: float = 1.0,
                 goto_settle_timeout_s: float = GOTO_SETTLE_TIMEOUT_S) -> None:
        self._base_url = f"http://{host}:{port}"
        self._auth = ("", password)
        self._timeout_scale = timeout_scale
        self._goto_settle_timeout_s = goto_settle_timeout_s
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._lock = threading.Lock()
        # Kept per item, not just "the last one", so the first seek after switching
        # songs uses the NEW song's length -- the old single cached value made a
        # cross-song Play/Loop Bookmark land at the wrong offset until the next poll.
        self._current_item_id: int | None = None
        self._status_duration_us: dict[int, int] = {}
        self._playlist_duration_us: dict[int, int] = {}
        self._exact_duration_us: dict[int, int] = {}
        self._last_duration_us: int | None = None  # VLC's own length for whatever was current
        self._goto_seq = 0  # bumped by every goto_item(); see get_status()

    # -- connection --

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def _get(self, path: str, *, timeout: float, params: dict | None = None) -> requests.Response:
        response = self._session().get(
            f"{self._base_url}{path}", params=params, auth=self._auth, timeout=timeout * self._timeout_scale
        )
        response.raise_for_status()
        return response

    def connect(self) -> None:
        self._get("/requests/status.json", timeout=STATUS_TIMEOUT_S)

    def disconnect(self) -> None:
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()
        self._local = threading.local()

    # -- durations --

    def set_exact_duration(self, vlc_id: int, duration_us: int | None) -> None:
        """Exact media length (e.g. from the decoded waveform) for a playlist item --
        VLC itself only reports whole seconds, which skews every percent-based seek and
        position-derived time by up to (fraction x 1s)."""
        with self._lock:
            if duration_us and duration_us > 0:
                self._exact_duration_us[vlc_id] = int(duration_us)
            else:
                self._exact_duration_us.pop(vlc_id, None)

    def _best_duration_us(self, vlc_id: int | None) -> int | None:
        with self._lock:
            if vlc_id is None:
                return self._last_duration_us
            vlc_length = self._status_duration_us.get(vlc_id) or self._playlist_duration_us.get(vlc_id)
            exact = self._exact_duration_us.get(vlc_id)
            # VLC's position fraction is relative to VLC's own idea of the length. Use
            # the exact decoded length only when it agrees with VLC's whole-second value
            # (a truncated file or a bad VBR estimate would otherwise skew every seek).
            if exact and (vlc_length is None or abs(exact - vlc_length) <= EXACT_DURATION_TOLERANCE_US):
                return exact
            return vlc_length

    # -- reads --

    def _status_json(self) -> dict:
        return self._get("/requests/status.json", timeout=STATUS_TIMEOUT_S).json()

    def get_status(self) -> PlaybackStatus:
        with self._lock:
            goto_seq = self._goto_seq
        data = self._status_json()
        current_id = _valid_id(data.get("currentplid"))
        vlc_duration_us = _seconds_to_us(data.get("length"))
        with self._lock:
            # A poll that was already in flight when goto_item() switched songs describes
            # the OLD song. Letting it overwrite the current item made the following seek
            # use the old song's length (found live: 15.5 s of a 20.7 s song was sent as
            # >100% of a 12.3 s song -> VLC ran off the end of the playlist and stopped).
            if goto_seq == self._goto_seq:
                self._current_item_id = current_id
                self._last_duration_us = vlc_duration_us
            if current_id is not None and vlc_duration_us is not None:
                self._status_duration_us[current_id] = vlc_duration_us
        duration_us = self._best_duration_us(current_id) if current_id is not None else vlc_duration_us
        position = float(data.get("position", 0.0) or 0.0)
        return PlaybackStatus(
            state=data.get("state", "stopped"),
            time_us=_precise_time_us(data.get("time", 0), position, duration_us),
            position=position,
            rate=float(data.get("rate", 1.0) or 1.0),
            current_playlist_item_id=current_id,
            duration_us=duration_us,
            media_uri=_extract_media_uri(data),
            volume=int(float(data.get("volume", 256) or 0)),
        )

    def get_playlist(self) -> list[VlcPlaylistItem]:
        root = ElementTree.fromstring(self._get("/requests/playlist.xml", timeout=PLAYLIST_TIMEOUT_S).content)
        # VLC 3 nests <node Playlist> and <node Media Library> under the root; only the
        # first (the play queue) is the playlist. Test fixtures send bare leaves.
        child_nodes = root.findall("node")
        scope = child_nodes[0] if child_nodes else root
        items: list[VlcPlaylistItem] = []
        durations: dict[int, int] = {}
        for leaf in scope.iter("leaf"):
            vlc_id = int(leaf.get("id", "0"))
            duration_s = _positive_float(leaf.get("duration"))
            if duration_s is not None:
                durations[vlc_id] = int(duration_s * 1_000_000)
            items.append(
                VlcPlaylistItem(
                    vlc_id=vlc_id,
                    uri=leaf.get("uri", ""),
                    name=leaf.get("name", ""),
                    duration_s=duration_s,
                )
            )
        with self._lock:
            self._playlist_duration_us = durations
        return items

    # -- commands --

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
        with self._lock:
            self._goto_seq += 1
            self._current_item_id = vlc_id
        self._command("pl_play", {"id": vlc_id})
        self._wait_until_current(vlc_id)

    def _wait_until_current(self, vlc_id: int) -> None:
        deadline = time.monotonic() + self._goto_settle_timeout_s
        while time.monotonic() < deadline:
            try:
                data = self._status_json()
            except requests.RequestException:
                return
            length_us = _seconds_to_us(data.get("length"))
            if _valid_id(data.get("currentplid")) == vlc_id and length_us is not None:
                with self._lock:
                    self._status_duration_us[vlc_id] = length_us
                    self._last_duration_us = length_us
                    self._current_item_id = vlc_id
                    self._goto_seq += 1  # polls issued before this confirmation are stale too
                return
            time.sleep(_GOTO_POLL_INTERVAL_S)

    def seek_absolute_us(self, time_us: int) -> None:
        """Direct user report: "the bookmark playback is not respecting the loop, it
        drifts away". Root-caused live against a real VLC instance (see this
        module's percent-seek verification, not a spec assumption): the built-in
        interface's `seek` command silently mis-parses a plain fractional-seconds
        value -- `val=12.345` actually landed at 345 SECONDS, not 12.345s -- so the
        previous whole-second-only seek wasn't just imprecise, it was the *safe*
        choice given that bug. Confirmed live, its documented percent syntax
        (`val=<float>%`) is both correctly parsed AND sub-second precise. Falls back
        to whole seconds only when no duration is known for the current item at all.
        """
        time_us = max(0, time_us)
        with self._lock:
            current = self._current_item_id
        duration_us = self._best_duration_us(current)
        if duration_us:
            percent = min(100.0, (time_us / duration_us) * 100.0)
            self._command("seek", {"val": f"{percent:.5f}%"})
        else:
            self._command("seek", {"val": time_us // 1_000_000})

    def seek_relative_us(self, delta_us: int) -> None:
        sign = "+" if delta_us >= 0 else "-"
        self._command("seek", {"val": f"{sign}{abs(delta_us) // 1_000_000}S"})

    def set_rate(self, rate: float) -> None:
        self._command("rate", {"val": rate})

    def set_volume(self, level: int) -> None:
        # VLC's built-in interface takes 0-512 (256 = 100%), not a percentage.
        self._command("volume", {"val": max(0, min(512, int(level)))})

    def _command(self, command: str, params: dict | None = None) -> None:
        self._get("/requests/status.json", timeout=COMMAND_TIMEOUT_S, params={"command": command, **(params or {})})


def _valid_id(raw: object) -> int | None:
    """VLC reports currentplid -1 when nothing is current."""
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _positive_float(raw: object) -> float | None:
    """VLC reports -1 for a playlist item it hasn't parsed yet; treat that as unknown."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _seconds_to_us(raw: object) -> int | None:
    seconds = _positive_float(raw)
    return int(seconds * 1_000_000) if seconds is not None else None


def _precise_time_us(raw_time_s: object, position: float, duration_us: int | None) -> int:
    """VLC's status.json "time" field is a whole number of seconds -- confirmed live,
    it never carries a fractional part, unlike "position" (a float fraction of the
    track, confirmed live to genuinely track sub-second progress). Deriving time from
    position*duration instead gives PlaybackClock (and, downstream, LoopController's
    boundary check) far better precision than truncating to the nearest second. Falls
    back to the plain integer field when duration or position isn't usable.
    """
    if duration_us and 0.0 <= position <= 1.0:
        return int(position * duration_us)
    try:
        return int(float(raw_time_s or 0) * 1_000_000)
    except (TypeError, ValueError):
        return 0


def _extract_media_uri(status_json: dict) -> str | None:
    # VLC sends `"information": []` (a list, not an object) when nothing is loaded.
    info = status_json.get("information")
    category = info.get("category") if isinstance(info, dict) else None
    meta = category.get("meta") if isinstance(category, dict) else None
    if not isinstance(meta, dict):
        return None
    return meta.get("filename") or meta.get("url")
