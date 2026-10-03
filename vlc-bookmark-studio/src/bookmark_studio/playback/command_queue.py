"""Runs playback commands (seek, play, volume, goto...) in order, off the UI thread.

Every PlaybackAdapter call is a blocking network round trip to VLC. Called straight
from a Qt slot or timer, a slow or stalled VLC froze the whole window for the call's
timeout (up to seconds), and fire-and-forget calls spread over a thread pool could
reach VLC out of order (e.g. a seek overtaking the goto_item it depended on).
ThreadedCommandQueue fixes both: one worker thread, strict FIFO order, results and
errors delivered back on the UI thread.

Each executor also keeps *fences*: the monotonic time (ns) at which the last command
of a given kind finished (``"position"`` for anything that moves the playhead,
``"volume"`` for volume writes). A status poll that was *issued* before a fence was
sampled before that command took effect, so callers can discard it instead of
reacting to stale data (see Application._on_status_result).

ImmediateExecutor has the same interface but runs each command inline -- used by
unit tests and anything that wants the old synchronous behaviour.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from PySide6.QtCore import QObject, Signal

DoneCallback = Callable[[Any], None]
ErrorCallback = Callable[[BaseException], None]


class CommandExecutor(Protocol):
    def submit(
        self,
        fn: Callable[[], Any],
        *,
        on_done: DoneCallback | None = None,
        on_error: ErrorCallback | None = None,
        coalesce_key: str | None = None,
        fences: Iterable[str] = (),
    ) -> None: ...

    def clear_pending(self) -> None: ...

    def fence_ns(self, name: str) -> int: ...

    def has_pending(self, fence: str) -> bool: ...


class ImmediateExecutor:
    """Runs every command synchronously on the caller's thread. Errors propagate to the
    caller unless an `on_error` callback is given."""

    def __init__(self) -> None:
        self._fences: dict[str, int] = {}

    def submit(
        self,
        fn: Callable[[], Any],
        *,
        on_done: DoneCallback | None = None,
        on_error: ErrorCallback | None = None,
        coalesce_key: str | None = None,
        fences: Iterable[str] = (),
    ) -> None:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - handed to on_error or re-raised
            if on_error is None:
                raise
            on_error(exc)
            return
        now = time.monotonic_ns()
        for name in fences:
            self._fences[name] = now
        if on_done is not None:
            on_done(result)

    def clear_pending(self) -> None:
        pass

    def fence_ns(self, name: str) -> int:
        return self._fences.get(name, 0)

    def has_pending(self, fence: str) -> bool:
        return False  # every command has run by the time submit() returns

    def shutdown(self, *, drain_timeout_s: float = 0.0) -> None:
        pass


@dataclass
class _Job:
    job_id: int
    fn: Callable[[], Any]
    coalesce_key: str | None
    fences: tuple[str, ...]
    on_done: DoneCallback | None = field(default=None, repr=False)
    on_error: ErrorCallback | None = field(default=None, repr=False)


class _Signals(QObject):
    done = Signal(int, object)
    failed = Signal(int, object)


class ThreadedCommandQueue(QObject):
    """Single worker thread, FIFO. Callbacks run on the thread that created the queue
    (the UI thread), via queued Qt signals.

    `coalesce_key`: a new job replaces any job with the same key that hasn't started
    yet (used for volume ramps -- only the latest level matters).
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._signals = _Signals(self)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._cv = threading.Condition()
        self._pending: deque[_Job] = deque()
        self._callbacks: dict[int, _Job] = {}
        self._next_id = 0
        self._busy = False
        self._running: _Job | None = None
        self._closed = False
        self._fences: dict[str, int] = {}
        self._thread = threading.Thread(target=self._run, name="vlc-bookmark-studio-commands", daemon=True)
        self._thread.start()

    # -- API (UI thread) --

    def submit(
        self,
        fn: Callable[[], Any],
        *,
        on_done: DoneCallback | None = None,
        on_error: ErrorCallback | None = None,
        coalesce_key: str | None = None,
        fences: Iterable[str] = (),
    ) -> None:
        with self._cv:
            if self._closed:
                return
            if coalesce_key is not None:
                for queued in [job for job in self._pending if job.coalesce_key == coalesce_key]:
                    self._pending.remove(queued)
                    self._callbacks.pop(queued.job_id, None)
            self._next_id += 1
            job = _Job(self._next_id, fn, coalesce_key, tuple(fences), on_done, on_error)
            self._callbacks[job.job_id] = job
            self._pending.append(job)
            self._cv.notify()

    def clear_pending(self) -> None:
        """Drops every job that hasn't started (e.g. when switching VLC instances)."""
        with self._cv:
            for job in self._pending:
                self._callbacks.pop(job.job_id, None)
            self._pending.clear()

    def fence_ns(self, name: str) -> int:
        with self._cv:
            return self._fences.get(name, 0)

    def is_idle(self) -> bool:
        with self._cv:
            return not self._pending and not self._busy

    def has_pending(self, fence: str) -> bool:
        """Whether a command with this fence (e.g. a volume write) is queued or running: a
        player status read meanwhile may still show what it is about to change."""
        with self._cv:
            running = self._running
            return (running is not None and fence in running.fences) or any(
                fence in job.fences for job in self._pending)

    def shutdown(self, *, drain_timeout_s: float = 1.5) -> None:
        """Lets already-queued jobs run for up to `drain_timeout_s`, then stops."""
        deadline = time.monotonic() + drain_timeout_s
        with self._cv:
            while (self._pending or self._busy) and time.monotonic() < deadline:
                self._cv.wait(timeout=max(0.0, deadline - time.monotonic()))
            self._closed = True
            self._pending.clear()
            self._callbacks.clear()
            self._cv.notify_all()
        self._thread.join(timeout=max(0.1, deadline - time.monotonic()))

    # -- worker thread --

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                job = self._pending.popleft()
                self._busy = True
                self._running = job
            try:
                result = job.fn()
            except Exception as exc:  # noqa: BLE001 - delivered to the UI thread
                with self._cv:
                    self._busy = False
                    self._running = None
                    self._cv.notify_all()
                self._signals.failed.emit(job.job_id, exc)
                continue
            finished = time.monotonic_ns()
            with self._cv:
                for name in job.fences:
                    self._fences[name] = finished
                self._busy = False
                self._running = None
                self._cv.notify_all()
            self._signals.done.emit(job.job_id, result)

    # -- UI thread --

    def _on_done(self, job_id: int, result: object) -> None:
        with self._cv:
            job = self._callbacks.pop(job_id, None)
        if job is not None and job.on_done is not None:
            job.on_done(result)

    def _on_failed(self, job_id: int, exc: object) -> None:
        with self._cv:
            job = self._callbacks.pop(job_id, None)
        if job is not None and job.on_error is not None:
            job.on_error(exc)  # type: ignore[arg-type]
