"""0.9.0 (requested on its build): a looping bookmark fades in at its very first start and
out before the end of its last pass only -- as Extract fades the whole file -- not on
every pass."""
from __future__ import annotations

from bookmark_studio.domain.enums import LoopState
from tests.unit.test_080_settings_tab import _loop_rig, _pass, _spec


def _volume_writes(adapter) -> list[int]:  # noqa: ANN001
    writes: list[int] = []
    original = adapter.set_volume

    def record(level: int) -> None:
        writes.append(level)
        original(level)

    adapter.set_volume = record
    return writes


def test_the_fade_in_is_the_first_pass_only(qtbot) -> None:
    adapter, clock, controller = _loop_rig()
    controller.set_user_volume(200)
    writes = _volume_writes(adapter)
    controller.start(_spec(repeat_count=3, fade_in_ms=400))
    assert writes[:1] == [0] and controller._fade_active  # the first pass starts at 0, fading in
    controller._fade_active = False  # (the fade's timer ends it; here, at once)
    controller._volume_owned = False
    writes.clear()
    _pass(adapter, clock, controller, 0)  # the second pass
    assert controller.state is LoopState.PLAYING
    assert 0 not in writes and not controller._fade_active  # no fade-in again


def test_the_fade_out_is_the_last_pass_only(qtbot) -> None:
    adapter, clock, controller = _loop_rig()
    controller.start(_spec(repeat_count=3, fade_out_ms=300))
    assert not controller._fade_out_timer.isActive()  # pass 1 of 3
    now = _pass(adapter, clock, controller, 0)
    assert not controller._fade_out_timer.isActive()  # pass 2 of 3
    _pass(adapter, clock, controller, now)
    assert controller._fade_out_timer.isActive() or controller._fade_active  # pass 3: the last


def test_a_forever_loop_never_fades_out(qtbot) -> None:
    adapter, clock, controller = _loop_rig()
    controller.start(_spec(repeat_count=None, fade_out_ms=300))
    now = 0
    for _ in range(3):
        assert not controller._fade_out_timer.isActive() and not controller._fade_active
        now = _pass(adapter, clock, controller, now)


def test_a_pass_that_becomes_the_last_fades_out_and_one_that_no_longer_is_comes_back(qtbot) -> None:
    adapter, clock, controller = _loop_rig()
    controller.set_user_volume(200)
    controller.start(_spec(repeat_count=None, fade_out_ms=300))
    controller.update_spec(_spec(repeat_count=1, fade_out_ms=300))  # set to 1 during the pass: the last
    assert controller._fade_out_timer.isActive() or controller._fade_active
    controller._begin_fade("out")  # (its fade-out has begun)
    controller._volume_owned = True
    controller.update_spec(_spec(repeat_count=None, fade_out_ms=300))  # Forever again: not the last
    assert not controller._fade_active and not controller._volume_owned  # back to the user's level


def test_switching_loop_off_fades_the_pass_playing_out(qtbot) -> None:
    adapter, clock, controller = _loop_rig()
    controller.start(_spec(repeat_count=None, fade_out_ms=300))
    controller.update_spec(_spec(repeat_count=None, fade_out_ms=300), last_pass=True)
    assert controller._fade_out_timer.isActive() or controller._fade_active
