"""Software A-B loop state machine driven over a PlaybackAdapter (spec #33-#35)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from bookmark_studio.domain.enums import CompletionAction, LoopState
from bookmark_studio.domain.loop import LoopSpec
from bookmark_studio.playback.playback_clock import PlaybackClock

if TYPE_CHECKING:
    from bookmark_studio.playback.adapter import PlaybackAdapter

__all__ = ["LoopState", "LoopSpec", "LoopController"]


class LoopController(QObject):
    """Drives one A-B loop over `adapter`, polled via `on_tick` (spec #32, #34).

    Not a DAW-grade sample-accurate loop (spec #35) -- boundary detection depends on
    the caller polling frequently enough (spec #32: 20-40ms near the B boundary).
    """

    loop_started = Signal(object)  # LoopSpec
    iteration_changed = Signal(object)  # remaining repeats, or None for infinite
    gap_started = Signal(int)  # gap_ms -- caller schedules resume_after_gap()
    loop_completed = Signal(object)  # CompletionAction that was applied
    bookmark_navigation_requested = Signal(object)  # CompletionAction (NEXT/PREVIOUS_BOOKMARK etc.)

    def __init__(self, adapter: "PlaybackAdapter", clock: PlaybackClock, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._adapter = adapter
        self._clock = clock
        self._spec: LoopSpec | None = None
        self._remaining: int | None = None
        self._state = LoopState.IDLE

    @property
    def state(self) -> LoopState:
        return self._state

    @property
    def spec(self) -> LoopSpec | None:
        return self._spec

    def start(self, spec: LoopSpec) -> None:
        self._spec = spec
        self._remaining = spec.repeat_count
        self._state = LoopState.ARMED
        self._adapter.seek_absolute_us(spec.start_us)
        self._adapter.play()
        self._state = LoopState.PLAYING
        self.loop_started.emit(spec)

    def stop(self) -> None:
        """User-initiated stop (spec #166 case): no completion action applied."""
        self._state = LoopState.IDLE
        self._spec = None
        self._remaining = None

    def on_tick(self, *, now_ns: int | None = None) -> None:
        """Call periodically while PLAYING; also handles a boundary already passed
        before this poll arrived (spec #166: "end reached before status poll")."""
        if self._state != LoopState.PLAYING or self._spec is None:
            return
        position_us = self._clock.estimated_position_us(now_ns=now_ns)
        if position_us >= self._spec.end_us:
            self._handle_boundary_reached()

    def resume_after_gap(self) -> None:
        if self._state != LoopState.GAP:
            return
        self._seek_and_continue()

    def _handle_boundary_reached(self) -> None:
        assert self._spec is not None
        if self._remaining is not None:
            self._remaining -= 1

        if self._remaining is not None and self._remaining <= 0:
            self._state = LoopState.COMPLETED
            self._apply_completion_action()
            return

        if self._spec.gap_ms > 0:
            self._state = LoopState.GAP
            self._adapter.pause()
            self.gap_started.emit(self._spec.gap_ms)
        else:
            self._seek_and_continue()

    def _seek_and_continue(self) -> None:
        assert self._spec is not None
        self._state = LoopState.SEEKING_BACK
        self._adapter.seek_absolute_us(self._spec.start_us)
        self._adapter.play()
        self._state = LoopState.PLAYING
        self.iteration_changed.emit(self._remaining)

    def _apply_completion_action(self) -> None:
        assert self._spec is not None
        action = self._spec.completion_action
        if action is CompletionAction.PAUSE:
            self._adapter.pause()
        elif action is CompletionAction.STOP:
            self._adapter.stop()
        elif action is CompletionAction.NEXT_TRACK:
            self._adapter.next_track()
        elif action in (
            CompletionAction.NEXT_BOOKMARK,
            CompletionAction.PREVIOUS_BOOKMARK,
            CompletionAction.NEXT_SEGMENT_QUEUE_ITEM,
        ):
            # Bookmark/queue navigation needs data this class doesn't own; the caller
            # (which does own the bookmark list / segment queue) handles it.
            self.bookmark_navigation_requested.emit(action)
        # CONTINUE: leave playback exactly as it is.
        self.loop_completed.emit(action)
