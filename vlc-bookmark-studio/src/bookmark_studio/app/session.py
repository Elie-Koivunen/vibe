"""PlaybackSession: the live connection to one player (a VLC window over HTTP, or the
in-app libVLC player).

Owns the adapter, the ordered command queue and the two polls (status, playlist). Every
adapter call runs off the UI thread; results come back as Qt signals on the UI thread:

* ``status_sampled(issued_ns, status)`` -- every status poll, with the monotonic time the
  request was *issued* (compare with ``is_stale``);
* ``playlist_sampled(items)`` -- every playlist poll;
* ``connection_changed(bool)``.

A poll that is still in flight is not repeated; with a slow or stalled VLC, piling up
overlapping requests would only make things worse.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from bookmark_studio.logging.setup import get_logger
from bookmark_studio.playback.adapter import PlaybackAdapter
from bookmark_studio.playback.command_queue import CommandExecutor, ThreadedCommandQueue

# Defaults for a VLC window driven over HTTP. VLC's Lua httpd couldn't take faster
# polling (see bridge_client.py), and the built-in interface gains nothing from it.
# In-process adapters declare their own, faster intervals (status_poll_ms attribute).
STATUS_POLL_MS = 400
PLAYLIST_POLL_MS = 2000
# A status poll issued less than this long after a seek/goto/play finished may still
# describe the old position (VLC applies seeks asynchronously). Such samples are treated
# as stale -- otherwise a sample taken just before a loop's seek-back but delivered just
# after it re-triggers the loop boundary and one pass is counted twice.
STALE_SAMPLE_MARGIN_NS = 150_000_000


class _CallSignals(QObject):
    finished = Signal(object)  # (issued_monotonic_ns, result)
    failed = Signal(str)


class _CallWorker(QRunnable):
    """One blocking adapter read on a QThreadPool worker (spec #108)."""

    def __init__(self, fn: Callable[[], object], signals: _CallSignals) -> None:
        super().__init__()
        self._fn = fn
        self._signals = signals

    def run(self) -> None:
        issued_ns = time.monotonic_ns()
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - reported to the UI thread via signal
            self._signals.failed.emit(str(exc))
            return
        self._signals.finished.emit((issued_ns, result))


def unpack_sample(payload: object) -> tuple[int, Any]:
    """Poll results arrive as (issued_ns, value); direct callers (tests) may pass the bare
    value, which is treated as fresh."""
    if isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[0], int):
        return payload[0], payload[1]
    return time.monotonic_ns(), payload


class PlaybackSession(QObject):
    status_sampled = Signal(object, object)  # issued_ns, PlaybackStatus
    status_failed = Signal(str)
    playlist_sampled = Signal(object)  # list[VlcPlaylistItem]
    connection_changed = Signal(bool)

    def __init__(self, adapter: PlaybackAdapter, *, command_executor: CommandExecutor | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._log = get_logger("VLC")
        self._adapter = adapter
        self.commands: CommandExecutor = command_executor or ThreadedCommandQueue(self)
        self.connected = False
        # Mute a VLC this app launched itself, once, on its first successful poll.
        self.mute_pending = False

        self._pool = QThreadPool(self)
        self._status_inflight = False
        self._playlist_inflight = False
        self._status_signals = _CallSignals(self)
        self._status_signals.finished.connect(self._on_status)
        self._status_signals.failed.connect(self._on_status_failed)
        self._playlist_signals = _CallSignals(self)
        self._playlist_signals.finished.connect(self._on_playlist)
        self._playlist_signals.failed.connect(self._on_playlist_failed)
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.poll_status)
        self.playlist_timer = QTimer(self)
        self.playlist_timer.timeout.connect(self.poll_playlist)
        self._stopped = False

    # -- adapter --

    @property
    def adapter(self) -> PlaybackAdapter:
        return self._adapter

    def start(self) -> None:
        self._connect(self._adapter)
        self.status_timer.start(self._status_interval())
        self.playlist_timer.start(self._playlist_interval())
        self.poll_playlist()

    def swap_adapter(self, adapter: PlaybackAdapter, *, mute_on_connect: bool = False) -> None:
        """Points the session at another player. Pending commands for the old one are
        dropped; the old adapter is disconnected (a VLC process is left running)."""
        self.status_timer.stop()
        self.playlist_timer.stop()
        self.commands.clear_pending()
        try:
            self._adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._adapter = adapter
        self.mute_pending = mute_on_connect
        self._set_connected(False)
        self.start()

    def _status_interval(self) -> int:
        return int(getattr(self._adapter, "status_poll_ms", STATUS_POLL_MS))

    def _playlist_interval(self) -> int:
        return int(getattr(self._adapter, "playlist_poll_ms", PLAYLIST_POLL_MS))

    def _connect(self, adapter: PlaybackAdapter) -> None:
        def _do_connect() -> None:
            try:
                adapter.connect()
            except Exception as exc:  # noqa: BLE001 - spec #104: stay usable offline
                self._log.info("connect failed (polling keeps retrying): %s", exc)

        self.submit(_do_connect, fences=())

    # -- commands --

    def submit(self, fn: Callable[[], object], *, fences: tuple[str, ...] = ("position",),
               on_done: Callable[[object], None] | None = None) -> None:
        """Queues one playback command; commands run in order, off the UI thread."""
        self.commands.submit(
            fn, fences=fences, on_done=on_done,
            on_error=lambda exc: self._log.debug("command failed: %s", exc),
        )

    def is_stale(self, issued_ns: int) -> bool:
        return issued_ns < self.commands.fence_ns("position") + STALE_SAMPLE_MARGIN_NS

    def is_volume_stale(self, issued_ns: int) -> bool:
        """The sample may predate our last volume change (the fader would jump back)."""
        return issued_ns < self.commands.fence_ns("volume") + STALE_SAMPLE_MARGIN_NS

    # -- polling --

    def poll_status(self) -> None:
        if self._status_inflight or self._stopped:
            return
        self._status_inflight = True
        self._pool.start(_CallWorker(self._adapter.get_status, self._status_signals))

    def poll_playlist(self) -> None:
        if self._playlist_inflight or self._stopped:
            return
        self._playlist_inflight = True
        self._pool.start(_CallWorker(self._adapter.get_playlist, self._playlist_signals))

    def _set_connected(self, connected: bool) -> None:
        if connected != self.connected:
            self.connected = connected
            self.connection_changed.emit(connected)

    def _on_status(self, payload: object) -> None:
        self._status_inflight = False
        issued_ns, status = unpack_sample(payload)
        self._set_connected(True)
        # mute_pending is acted on by the listener (Application), which also has to tell
        # the loop controller that the listening volume is now 0.
        self.status_sampled.emit(issued_ns, status)

    def _on_status_failed(self, message: str) -> None:
        self._status_inflight = False
        self._set_connected(False)
        self._log.debug("status poll failed: %s", message)
        self.status_failed.emit(message)

    def _on_playlist(self, payload: object) -> None:
        self._playlist_inflight = False
        _issued_ns, items = unpack_sample(payload)
        self.playlist_sampled.emit(items)

    def _on_playlist_failed(self, message: str) -> None:
        self._playlist_inflight = False
        self._log.debug("playlist poll failed: %s", message)

    # -- shutdown --

    def stop(self, *, drain_timeout_s: float = 1.5) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.status_timer.stop()
        self.playlist_timer.stop()
        shutdown = getattr(self.commands, "shutdown", None)
        if shutdown is not None:
            shutdown(drain_timeout_s=drain_timeout_s)
        self._pool.waitForDone(3000)
        try:
            self._adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass
