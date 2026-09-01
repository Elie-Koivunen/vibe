from __future__ import annotations

import pytest

from bookmark_studio.domain.enums import CompletionAction, LoopState
from bookmark_studio.domain.loop import LoopSpec
from bookmark_studio.playback.loop_controller import LoopController
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.playback.playback_clock import PlaybackClock
from bookmark_studio.playback.status import VlcPlaylistItem

NS_PER_US = 1_000


@pytest.fixture()
def rig():
    adapter = MockPlaybackAdapter(
        [VlcPlaylistItem(vlc_id=1, uri="file:///song.mp3", name="Song", duration_s=60.0)]
    )
    clock = PlaybackClock()
    controller = LoopController(adapter, clock)
    return adapter, clock, controller


def _sync_clock(adapter: MockPlaybackAdapter, clock: PlaybackClock, now_ns: int) -> None:
    clock.update(adapter.get_status(), now_ns=now_ns)


def test_infinite_loop_repeats_forever(rig) -> None:
    adapter, clock, controller = rig
    spec = LoopSpec(start_us=1_000_000, end_us=2_000_000, repeat_count=None, gap_ms=0,
                     completion_action=CompletionAction.CONTINUE)
    controller.start(spec)
    assert controller.state is LoopState.PLAYING
    assert adapter.get_status().time_us == 1_000_000

    now = 0
    for _ in range(5):
        adapter.advance_time_us(1_100_000)  # pass the 2,000,000us boundary
        now += 1_100_000 * NS_PER_US
        _sync_clock(adapter, clock, now)
        controller.on_tick(now_ns=now)
        assert controller.state is LoopState.PLAYING
        assert adapter.get_status().time_us == 1_000_000  # seeked back to A each time
        _sync_clock(adapter, clock, now)  # re-sync after the internal seek


def test_fixed_repeat_count_completes(rig) -> None:
    adapter, clock, controller = rig
    completions = []
    controller.loop_completed.connect(lambda action: completions.append(action))
    spec = LoopSpec(start_us=0, end_us=1_000_000, repeat_count=3, gap_ms=0,
                     completion_action=CompletionAction.CONTINUE)
    controller.start(spec)

    now = 0
    for i in range(3):
        adapter.advance_time_us(1_100_000)
        now += 1_100_000 * NS_PER_US
        _sync_clock(adapter, clock, now)
        controller.on_tick(now_ns=now)
        if i < 2:
            assert controller.state is LoopState.PLAYING
        _sync_clock(adapter, clock, now)

    assert controller.state is LoopState.COMPLETED
    assert completions == [CompletionAction.CONTINUE]


def test_completion_action_pause(rig) -> None:
    adapter, clock, controller = rig
    spec = LoopSpec(start_us=0, end_us=500_000, repeat_count=1, gap_ms=0,
                     completion_action=CompletionAction.PAUSE)
    controller.start(spec)
    adapter.advance_time_us(600_000)
    _sync_clock(adapter, clock, 600_000 * NS_PER_US)
    controller.on_tick(now_ns=600_000 * NS_PER_US)
    assert adapter.get_status().state == "paused"


def test_completion_action_stop(rig) -> None:
    adapter, clock, controller = rig
    spec = LoopSpec(start_us=0, end_us=500_000, repeat_count=1, gap_ms=0,
                     completion_action=CompletionAction.STOP)
    controller.start(spec)
    adapter.advance_time_us(600_000)
    _sync_clock(adapter, clock, 600_000 * NS_PER_US)
    controller.on_tick(now_ns=600_000 * NS_PER_US)
    assert adapter.get_status().state == "stopped"


def test_next_bookmark_completion_emits_navigation_not_adapter_calls(rig) -> None:
    adapter, clock, controller = rig
    requests = []
    controller.bookmark_navigation_requested.connect(lambda action: requests.append(action))
    spec = LoopSpec(start_us=0, end_us=500_000, repeat_count=1, gap_ms=0,
                     completion_action=CompletionAction.NEXT_BOOKMARK)
    controller.start(spec)
    adapter.advance_time_us(600_000)
    _sync_clock(adapter, clock, 600_000 * NS_PER_US)
    controller.on_tick(now_ns=600_000 * NS_PER_US)
    assert requests == [CompletionAction.NEXT_BOOKMARK]
    # LoopController must not have tried to drive VLC transport itself for this action.
    assert adapter.get_status().state == "playing"


def test_gap_pauses_and_resume_after_gap_reseeks(rig) -> None:
    adapter, clock, controller = rig
    gaps = []
    controller.gap_started.connect(lambda ms: gaps.append(ms))
    spec = LoopSpec(start_us=1_000_000, end_us=2_000_000, repeat_count=None, gap_ms=500,
                     completion_action=CompletionAction.CONTINUE)
    controller.start(spec)

    adapter.advance_time_us(1_100_000)
    _sync_clock(adapter, clock, 1_100_000 * NS_PER_US)
    controller.on_tick(now_ns=1_100_000 * NS_PER_US)

    assert controller.state is LoopState.GAP
    assert gaps == [500]
    assert adapter.get_status().state == "paused"
    # Must not have re-seeked yet -- position holds at wherever it stopped.
    assert adapter.get_status().time_us != 1_000_000

    controller.resume_after_gap()
    assert controller.state is LoopState.PLAYING
    assert adapter.get_status().time_us == 1_000_000
    assert adapter.get_status().state == "playing"


def test_boundary_reached_before_poll_still_detected(rig) -> None:
    """spec #166: 'end reached before status poll' -- a big jump past B in one tick."""
    adapter, clock, controller = rig
    spec = LoopSpec(start_us=0, end_us=1_000_000, repeat_count=None, gap_ms=0,
                     completion_action=CompletionAction.CONTINUE)
    controller.start(spec)
    adapter.advance_time_us(10_000_000)  # jump way past B in a single tick
    _sync_clock(adapter, clock, 10_000_000 * NS_PER_US)
    controller.on_tick(now_ns=10_000_000 * NS_PER_US)
    assert controller.state is LoopState.PLAYING
    assert adapter.get_status().time_us == 0  # seeked back to A


def test_user_stop_applies_no_completion_action(rig) -> None:
    adapter, clock, controller = rig
    completions = []
    controller.loop_completed.connect(lambda action: completions.append(action))
    spec = LoopSpec(start_us=0, end_us=1_000_000, repeat_count=None, gap_ms=0,
                     completion_action=CompletionAction.STOP)
    controller.start(spec)
    controller.stop()
    assert controller.state is LoopState.IDLE
    assert completions == []
    assert adapter.get_status().state == "playing"  # never touched by stop()


def test_start_validates_via_loopspec_construction() -> None:
    with pytest.raises(ValueError):
        LoopSpec(start_us=1000, end_us=500, repeat_count=None, gap_ms=0,
                 completion_action=CompletionAction.CONTINUE)
