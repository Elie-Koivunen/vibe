"""0.8.0: Reset glides back the way it came, equalizer presets glide over a set time, and
the "Bookmark settings" tab: Apply for a new bookmark, Undo/Redo beside it, and the
orange flash after every saved change."""
from __future__ import annotations

import time

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from bookmark_studio.domain.equalizer import EqualizerSettings
from bookmark_studio.domain.selection import Selection
from bookmark_studio.ui.bookmark_panel import FLASH_ROLE, GAP_COLUMN, BookmarkPanel
from bookmark_studio.ui.volume_eq_panel import VolumeEqPanel
from tests.unit.test_050_ui import _bookmark, _make_app
from tests.unit.test_070_layout import _ini, _playing_app

# -- Normalize (80 %) and Reset (50 %): to their own level, gliding the way they come --


def _volume_after(qtbot, adapter, ms: int) -> int:
    qtbot.wait(ms)
    return adapter.get_status().volume


def test_reset_coming_up_from_mute_glides_at_mutes_speed_to_its_level(qtbot, tmp_path) -> None:
    app, adapter = _playing_app(qtbot, tmp_path)
    panel = app.window._volume_eq
    assert panel.levels() == (80, 50) and panel._volume_reset_button.isEnabled()  # always
    panel._max_ms.setValue(0)  # Max's time must not matter here
    panel._mute_ms.setValue(500)
    panel._mute_button.click()
    qtbot.waitUntil(lambda: adapter.get_status().volume == 0, timeout=3000)
    panel._volume_reset_button.click()
    assert app._volume_ramp is not None  # gliding, not at once
    assert 0 < _volume_after(qtbot, adapter, 200) < 128
    qtbot.waitUntil(lambda: adapter.get_status().volume == 128, timeout=3000)  # 50 %


def test_normalize_coming_down_from_max_glides_at_maxs_speed_to_its_level(qtbot, tmp_path) -> None:
    app, adapter = _playing_app(qtbot, tmp_path)
    panel = app.window._volume_eq
    panel._mute_ms.setValue(0)  # Mute's time must not matter here
    panel._max_ms.setValue(500)
    adapter.set_volume(300)  # above Normalize's 80 % (= 205)
    qtbot.waitUntil(lambda: app._loop_controller.target_volume == 300, timeout=3000)
    started = time.monotonic()
    panel._normalize_button.click()
    assert 205 < _volume_after(qtbot, adapter, 200) < 300
    qtbot.waitUntil(lambda: adapter.get_status().volume == 205, timeout=3000)
    assert time.monotonic() - started >= 0.4  # about Max's 500 ms


def test_normalize_and_reset_levels_are_kept_and_act_at_once_when_nothing_plays(qtbot, tmp_path) -> None:
    settings = _ini(tmp_path)
    app = _make_app(qtbot, tmp_path, settings=settings)
    panel = app.window._volume_eq
    panel._normalize_level.setValue(90)
    panel._reset_level.setValue(30)
    assert (settings.volume_level_percent("normalize"), settings.volume_level_percent("reset")) == (90, 30)
    adapter = app.session.adapter
    panel._normalize_button.click()  # stopped: at once
    assert app._volume_ramp is None
    qtbot.waitUntil(lambda: adapter.get_status().volume == 230, timeout=2000)  # 90 %
    panel._volume_reset_button.click()
    qtbot.waitUntil(lambda: adapter.get_status().volume == 77, timeout=2000)  # 30 %
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _make_app(qtbot, second_dir, settings=settings)
    assert second.window.volume_levels() == (90, 30)


# -- equalizer presets glide --


def _eq_panel(qtbot, glide_ms: int) -> tuple[VolumeEqPanel, list[EqualizerSettings]]:
    panel = VolumeEqPanel()
    qtbot.addWidget(panel)
    panel.set_equalizer(EqualizerSettings(enabled=True))
    panel.set_glide_ms(glide_ms)
    seen: list[EqualizerSettings] = []
    panel.equalizer_changed.connect(seen.append)
    return panel, seen


def test_a_preset_glides_there_step_by_step(qtbot) -> None:
    panel, seen = _eq_panel(qtbot, 400)
    club = EqualizerSettings.from_preset("Club")
    panel._preset_combo.activated.emit(panel._preset_combo.findText("Club"))
    assert panel.is_gliding() and seen == []  # nothing jumped
    assert panel._preset_combo.currentText() == "Club"  # where it is going
    qtbot.wait(200)
    middle = panel.equalizer()
    assert 0.0 < middle.bands_db[2] < 8.0 and 6.0 < middle.preamp_db < 12.0  # on the way
    qtbot.waitUntil(lambda: not panel.is_gliding(), timeout=2000)
    assert seen[-1] == club and panel.equalizer() == club
    assert len(seen) > 3  # every step went out, like a fader drag
    assert panel._band_values[2].text() == "+8.0"


def test_flat_glides_too_and_a_hand_on_a_fader_takes_over(qtbot) -> None:
    panel, seen = _eq_panel(qtbot, 2000)
    panel.set_equalizer(EqualizerSettings.from_preset("Rock"))
    panel._reset_button.click()  # Flat
    qtbot.wait(150)
    assert panel.is_gliding()
    panel._bands[0].db_changed.emit(-3.0)  # the user grabs a fader
    assert not panel.is_gliding()
    assert seen[-1].bands_db[0] == -3.0
    held = panel.equalizer()
    qtbot.wait(150)
    assert panel.equalizer() == held  # the glide is over


def test_presets_change_at_once_when_nothing_plays_or_the_time_is_zero(qtbot) -> None:
    panel, seen = _eq_panel(qtbot, 1000)
    panel.set_glide_condition(lambda: False)  # nothing playing
    panel._preset_combo.activated.emit(panel._preset_combo.findText("Pop"))
    assert not panel.is_gliding() and seen[-1] == EqualizerSettings.from_preset("Pop")
    panel.set_glide_condition(lambda: True)
    panel.set_glide_ms(0)
    panel._preset_combo.activated.emit(panel._preset_combo.findText("Rock"))
    assert not panel.is_gliding() and seen[-1] == EqualizerSettings.from_preset("Rock")


def test_in_the_app_a_preset_jumps_while_nothing_plays(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)  # stopped
    panel = app.window._volume_eq
    panel._enabled_check.setChecked(True)
    panel._glide_ms.setValue(2000)
    panel._preset_combo.activated.emit(panel._preset_combo.findText("Live"))
    assert not panel.is_gliding()
    qtbot.waitUntil(lambda: app.session.adapter.equalizer == EqualizerSettings.from_preset("Live"), timeout=2000)


def test_a_preset_glides_on_the_player_while_it_plays_and_the_time_is_kept(qtbot, tmp_path) -> None:
    settings = _ini(tmp_path)
    app, adapter = _playing_app(qtbot, tmp_path, settings=settings)
    panel = app.window._volume_eq
    panel._enabled_check.setChecked(True)
    panel._glide_ms.setValue(600)
    assert settings.equalizer_glide_ms() == 600
    applied: list[EqualizerSettings] = []
    original = adapter.set_equalizer

    def record(eq, *, full=False):  # noqa: ANN001
        applied.append(eq)
        original(eq, full=full)

    adapter.set_equalizer = record
    panel._preset_combo.activated.emit(panel._preset_combo.findText("Techno"))
    qtbot.waitUntil(lambda: adapter.equalizer == EqualizerSettings.from_preset("Techno"), timeout=3000)
    assert len(applied) > 2  # it went there gradually
    assert settings.equalizer() == EqualizerSettings.from_preset("Techno")
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _make_app(qtbot, second_dir, settings=settings)
    assert second.window.equalizer_glide_ms() == 600


# -- the Bookmark settings tab --


def _with_selection(qtbot, tmp_path):
    app = _make_app(qtbot, tmp_path)
    app.window._waveform_scene.set_selection(Selection(start_us=1_000_000, end_us=3_000_000))
    return app, app.window._inspector


def test_the_tab_is_called_bookmark_studio(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    assert app.window._side_tabs.tabText(0) == "Bookmark Studio"  # "Bookmark settings" in 0.8.0


def test_apply_saves_a_new_bookmark_as_set_up_in_the_tab(qtbot, tmp_path) -> None:
    app, inspector = _with_selection(qtbot, tmp_path)
    assert inspector.is_drafting() and inspector._apply_button.isEnabled()
    assert inspector._loop_checkbox.isChecked()  # the usual defaults
    inspector._name_edit.setText("Drop one")
    inspector._repeat_spin.setValue(3)
    inspector._fade_in_spin.setValue(250)
    inspector._tags_picker.set_tags(("drop",))
    inspector._notes_edit.setPlainText("big one")
    assert app._bookmark_repository.list_for_playlist(app.playlists.synchronizer.active_playlist_id) == []

    inspector._apply_button.click()
    (saved,) = app._bookmark_repository.list_for_playlist(app.playlists.synchronizer.active_playlist_id)
    assert (saved.name, saved.start_us, saved.end_us) == ("Drop one", 1_000_000, 3_000_000)
    assert (saved.repeat_count, saved.fade_in_ms, saved.tags, saved.notes) == (3, 250, ("drop",), "big one")
    assert inspector.current_bookmark().id == saved.id and not inspector.is_drafting()
    assert not inspector._apply_button.isEnabled()  # existing bookmark: saved as you go
    assert app.window._waveform_scene.selection() is None
    assert app.window._bookmark_panel.is_flashing(saved.id) and inspector.is_flashing()


def test_enter_in_the_name_and_bookmark_selection_also_apply(qtbot, tmp_path) -> None:
    app, inspector = _with_selection(qtbot, tmp_path)
    inspector._name_edit.setText("Via Enter")
    inspector._gap_spin.setValue(500)
    inspector._name_edit.returnPressed.emit()
    playlist = app.playlists.synchronizer.active_playlist_id
    assert [(b.name, b.loop_gap_ms) for b in app._bookmark_repository.list_for_playlist(playlist)] == [("Via Enter", 500)]

    app.window._waveform_scene.set_selection(Selection(start_us=4_000_000, end_us=5_000_000))
    inspector._name_edit.setText("Via Ctrl+B")
    inspector._loop_checkbox.setChecked(False)
    app.window._bookmark_selection_button.click()
    names = {b.name: b for b in app._bookmark_repository.list_for_playlist(playlist)}
    assert set(names) == {"Via Enter", "Via Ctrl+B"} and names["Via Ctrl+B"].loop_enabled is False


def test_typed_start_and_end_move_the_selection(qtbot, tmp_path) -> None:
    app, inspector = _with_selection(qtbot, tmp_path)
    inspector._start_edit.setText("00:00:00.500")
    inspector._on_start_committed()
    assert app.window._waveform_scene.selection() == Selection(start_us=500_000, end_us=3_000_000)
    inspector._end_edit.setText("00:00:00.200")  # before the start: refused
    inspector._on_end_committed()
    assert app.window._waveform_scene.selection() == Selection(start_us=500_000, end_us=3_000_000)
    assert inspector._end_edit.text() == "00:00:03.000"


def test_clearing_the_selection_drops_the_form(qtbot, tmp_path) -> None:
    app, inspector = _with_selection(qtbot, tmp_path)
    app.window._waveform_scene.clear_selection()
    assert not inspector.is_drafting() and not inspector._apply_button.isEnabled()
    assert not inspector._start_edit.isEnabled()


def test_a_change_to_a_bookmark_is_saved_at_once_and_flashes_orange(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    bookmark = _bookmark(app, end_us=3_000_000)
    app.window._on_bookmark_activated(bookmark.id)
    inspector = app.window._inspector
    assert not inspector._apply_button.isEnabled()
    inspector._fade_out_spin.setValue(400)  # on the fly
    assert app._bookmark_repository.get(bookmark.id).fade_out_ms == 400
    panel = app.window._bookmark_panel
    assert panel.is_flashing(bookmark.id) and inspector.is_flashing()
    assert "#ff9f1a" in inspector._name_edit.styleSheet()
    qtbot.waitUntil(lambda: not inspector.is_flashing(), timeout=4000)
    assert inspector._name_edit.styleSheet() == ""
    qtbot.waitUntil(lambda: not panel.is_flashing(bookmark.id), timeout=4000)


def test_the_flashing_row_is_orange_even_when_selected(qtbot) -> None:
    from uuid import uuid4

    from tests.unit.test_regressions_020 import _segment

    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    panel.resize(700, 200)
    panel.show()
    qtbot.waitExposed(panel)
    media_id = uuid4()
    bookmark = _segment(media_id, None, loop_enabled=False)
    panel.set_bookmarks([bookmark], {media_id: "Song"})
    panel.select_bookmark(bookmark.id)
    gaps: list = []
    panel.gap_edited.connect(lambda *args: gaps.append(args))
    panel.flash_bookmark(bookmark.id, duration_ms=1500)
    QApplication.processEvents()
    tree = panel._tree
    rect = tree.visualItemRect(tree.topLevelItem(0))
    image = tree.viewport().grab().toImage()
    orange = QColor(255, 159, 26)
    y = rect.top() + 1  # above the text
    row = [image.pixelColor(x, y) for x in range(rect.left(), min(rect.right(), image.width() - 1), 3)]
    orange_share = sum(1 for p in row if (p.red(), p.green(), p.blue()) == (orange.red(), orange.green(), orange.blue()))
    assert orange_share > len(row) // 2  # orange, not the selection's colour
    assert gaps == []  # showing the flash is not an edit (it used to re-save the Gap column)
    panel.set_bookmarks([bookmark], {media_id: "Song 2"})  # rebuilt meanwhile: still flashing
    assert tree.topLevelItem(0).data(GAP_COLUMN, FLASH_ROLE)


def test_undo_and_redo_beside_apply(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    bookmark = _bookmark(app, end_us=3_000_000)
    app.window._on_bookmark_activated(bookmark.id)
    inspector = app.window._inspector
    assert not inspector.undo_button.isEnabled() and not inspector.redo_button.isEnabled()
    inspector._gap_spin.setValue(750)
    assert inspector.undo_button.isEnabled() and "Undo" in inspector.undo_button.toolTip()
    inspector.undo_button.click()
    assert app._bookmark_repository.get(bookmark.id).loop_gap_ms == 0
    assert inspector._gap_spin.value() == 0 and inspector.redo_button.isEnabled()
    inspector.redo_button.click()
    assert app._bookmark_repository.get(bookmark.id).loop_gap_ms == 750


# -- a playing loop follows its bookmark's edits (reported on 0.8.0's test build) --


def _loop_rig():
    from bookmark_studio.playback.loop_controller import LoopController
    from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
    from bookmark_studio.playback.playback_clock import PlaybackClock
    from bookmark_studio.playback.status import VlcPlaylistItem

    adapter = MockPlaybackAdapter([VlcPlaylistItem(vlc_id=1, uri="file:///song.mp3", name="Song", duration_s=60.0)])
    clock = PlaybackClock()
    return adapter, clock, LoopController(adapter, clock)


def _pass(adapter, clock, controller, now_ns: int) -> int:
    """Plays past the 1 s segment's end; returns the new time."""
    adapter.advance_time_us(1_100_000)
    now_ns += 1_100_000_000
    clock.update(adapter.get_status(), now_ns=now_ns)
    controller.on_tick(now_ns=now_ns)
    clock.update(adapter.get_status(), now_ns=now_ns)
    return now_ns


def _spec(**overrides):
    from bookmark_studio.domain.enums import CompletionAction
    from bookmark_studio.domain.loop import LoopSpec

    values = dict(start_us=0, end_us=1_000_000, repeat_count=None, gap_ms=0,
                  completion_action=CompletionAction.CONTINUE)
    values.update(overrides)
    return LoopSpec(**values)


def test_a_repeat_count_set_while_looping_ends_the_loop_with_the_new_action() -> None:
    from bookmark_studio.domain.enums import CompletionAction, LoopState

    adapter, clock, controller = _loop_rig()
    controller.start(_spec())  # forever
    now = _pass(adapter, clock, controller, 0)
    now = _pass(adapter, clock, controller, now)  # two passes done
    assert controller.state is LoopState.PLAYING
    # The user sets Repeat 1 and After loop: Stop during the 3rd pass.
    controller.update_spec(_spec(repeat_count=1, completion_action=CompletionAction.STOP))
    assert controller.state is LoopState.PLAYING  # the current pass finishes
    _pass(adapter, clock, controller, now)
    assert controller.state is LoopState.COMPLETED
    assert adapter.get_status().state == "stopped"


def test_a_higher_repeat_count_counts_the_passes_already_played() -> None:
    from bookmark_studio.domain.enums import LoopState

    adapter, clock, controller = _loop_rig()
    controller.start(_spec(repeat_count=2))
    now = _pass(adapter, clock, controller, 0)  # 1 of 2 done
    controller.update_spec(_spec(repeat_count=4))  # 3 more to go, this one included
    for _ in range(2):
        now = _pass(adapter, clock, controller, now)
        assert controller.state is LoopState.PLAYING
    _pass(adapter, clock, controller, now)
    assert controller.state is LoopState.COMPLETED


def test_switching_loop_off_finishes_the_current_pass_and_ends_a_gap_at_once() -> None:
    from bookmark_studio.domain.enums import CompletionAction, LoopState

    adapter, clock, controller = _loop_rig()
    controller.start(_spec())
    controller.update_spec(_spec(completion_action=CompletionAction.PAUSE), last_pass=True)
    _pass(adapter, clock, controller, 0)
    assert controller.state is LoopState.COMPLETED and adapter.get_status().state == "paused"

    adapter2, clock2, controller2 = _loop_rig()
    controller2.start(_spec(gap_ms=60_000))
    _pass(adapter2, clock2, controller2, 0)
    assert controller2.state is LoopState.GAP
    controller2.update_spec(_spec(gap_ms=60_000, repeat_count=1, completion_action=CompletionAction.STOP))
    assert controller2.state is LoopState.COMPLETED and adapter2.get_status().state == "stopped"


def test_in_the_app_editing_the_playing_bookmark_changes_its_loop(qtbot, tmp_path) -> None:
    """Reported on the 0.8.0 test build: with a bookmark looping, setting Repeat 1 and
    After loop "Stop" in the Bookmark settings tab was ignored -- the loop went on."""
    from bookmark_studio.domain.enums import CompletionAction, LoopState

    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    bookmark = _bookmark(app, start_us=1_000_000, end_us=1_600_000, loop_enabled=True)
    app._on_loop_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.PLAYING, timeout=3000)
    app.window._on_bookmark_activated(bookmark.id)
    inspector = app.window._inspector
    inspector._repeat_spin.setValue(1)
    index = inspector._completion_combo.findData(CompletionAction.STOP.value)
    inspector._completion_combo.setCurrentIndex(index)
    spec = app._loop_controller.spec
    assert spec.repeat_count == 1 and spec.completion_action is CompletionAction.STOP  # picked up at once
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.COMPLETED, timeout=4000)
    qtbot.waitUntil(lambda: adapter.get_status().state == "stopped", timeout=2000)


def test_deleting_the_playing_bookmark_stops_its_loop(qtbot, tmp_path) -> None:
    from bookmark_studio.domain.enums import LoopState

    app = _make_app(qtbot, tmp_path)
    bookmark = _bookmark(app, start_us=1_000_000, end_us=3_000_000, loop_enabled=True)
    app._on_loop_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.PLAYING, timeout=3000)
    app.window._on_delete_bookmark_requested([bookmark.id])
    assert app._loop_controller.state is LoopState.IDLE


# -- room for the side tab (reported on 0.8.0's test build: the equalizer needed scrolling) --


def test_the_volume_and_eq_tab_gets_room_for_the_whole_equalizer(qtbot, tmp_path) -> None:
    from PySide6.QtWidgets import QApplication

    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.resize(1100, 700)
    window.show()
    qtbot.waitExposed(window)
    window._splitters["bottom"].setSizes([900, 200])  # the list took nearly everything
    QApplication.processEvents()
    screen = window.screen().availableGeometry()
    window.show_side_tab("volume")
    area = window._side_tabs.currentWidget()

    def fits() -> bool:
        needed = area.widget().minimumSizeHint()
        return area.viewport().width() >= needed.width() and area.viewport().height() >= needed.height()

    def at_the_screens_edge() -> bool:
        frame = window.frameGeometry()
        return frame.width() >= screen.width() or frame.height() >= screen.height()

    # The list and the waveform give up their room first; past that the window grows as
    # far as the screen allows (the offscreen test screen is smaller than this window).
    qtbot.waitUntil(lambda: fits() or at_the_screens_edge(), timeout=3000)
    QApplication.processEvents()
    assert window._splitters["bottom"].sizes()[1] > 200  # the tab got room from the list
    if fits():  # the scroll bars go once the area has re-laid out
        qtbot.waitUntil(lambda: not area.horizontalScrollBar().isVisible()
                        and not area.verticalScrollBar().isVisible(), timeout=3000)


def test_the_window_grows_when_the_panels_cannot_make_room(qtbot, tmp_path) -> None:
    from PySide6.QtWidgets import QApplication

    app = _make_app(qtbot, tmp_path)
    window = app.window
    window.show()
    qtbot.waitExposed(window)
    window.resize(window.minimumSize())
    QApplication.processEvents()
    before = window.width()
    screen_width = window.screen().availableGeometry().width()
    window.show_side_tab("volume")
    area = window._side_tabs.currentWidget()

    def fits() -> bool:
        return area.viewport().width() >= area.widget().minimumSizeHint().width()

    # As far as the screen allows: a small screen (e.g. the offscreen test screen in WSL,
    # narrower than this window already is) leaves no room to grow into.
    qtbot.waitUntil(lambda: fits() or window.frameGeometry().width() >= screen_width, timeout=3000)
    assert window.width() >= before  # never shrunk
    assert window._splitters["vertical"].sizes()[1] > 0


# -- a playing bookmark is green on its song, waveform area and list row; yellow when done --


def _marks(app, bookmark_id):
    """What each of the three places shows for the bookmark: (playlist, waveform, list)."""
    app.window._refresh_bookmarks()  # the song's bookmarks drawn on the waveform, as in the app
    item = app.window._waveform_scene.bookmark_item(bookmark_id)
    song = app.window._playlist_panel.bookmark_song()
    return (
        song[1] if song is not None else None,
        item.playback_state() if item is not None else None,
        app.window._bookmark_panel.playback_state(bookmark_id),
    )


def test_a_looping_bookmark_is_green_everywhere_then_yellow_when_its_loop_ends(qtbot, tmp_path) -> None:
    from bookmark_studio.domain.enums import CompletionAction, LoopState

    app = _make_app(qtbot, tmp_path)
    bookmark = _bookmark(app, start_us=1_000_000, end_us=1_500_000, loop_enabled=True, repeat_count=1,
                         completion_action=CompletionAction.PAUSE)
    app._on_play_bookmark_requested(bookmark.id)
    assert _marks(app, bookmark.id) == ("playing", "playing", "playing")
    song = app.window._playlist_panel.bookmark_song()
    assert song[0] == app._playlist_item_for_media(bookmark.media_id).vlc_id  # its own song
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.COMPLETED, timeout=4000)
    assert _marks(app, bookmark.id) == ("done", "done", "done")


def test_the_colours_are_drawn_green_and_yellow(qtbot, tmp_path) -> None:
    from bookmark_studio.ui.bookmark_panel import PLAYBACK_ROW_COLORS
    from bookmark_studio.ui.playlist_panel import _BOOKMARK_SONG_COLORS
    from bookmark_studio.ui.waveform.bookmark_item import PLAYBACK_COLORS

    assert PLAYBACK_ROW_COLORS["playing"] == _BOOKMARK_SONG_COLORS["playing"]  # the same green
    assert PLAYBACK_ROW_COLORS["done"] == _BOOKMARK_SONG_COLORS["done"]  # the same yellow
    green, yellow = PLAYBACK_COLORS["playing"][0], PLAYBACK_COLORS["done"][0]
    assert green.green() > green.red() and yellow.red() > 200 and yellow.green() > 200 and yellow.blue() < 100

    app = _make_app(qtbot, tmp_path)
    bookmark = _bookmark(app, start_us=1_000_000, end_us=3_000_000, loop_enabled=True)
    app._on_loop_bookmark_requested(bookmark.id)
    playlist = app.window._playlist_panel._tree
    row = next(playlist.topLevelItem(i) for i in range(playlist.topLevelItemCount())
               if playlist.topLevelItem(i).data(0, 32) == app.window._playlist_panel.bookmark_song()[0])
    assert row.background(0).color() == _BOOKMARK_SONG_COLORS["playing"]
    app._stop_loop()  # e.g. Stop
    assert row.background(0).color() == _BOOKMARK_SONG_COLORS["done"]


def test_a_bookmark_played_without_a_loop_is_done_once_playback_passes_its_end(qtbot, tmp_path) -> None:
    from bookmark_studio.app import application as application_module

    app = _make_app(qtbot, tmp_path)
    adapter = app.session.adapter
    bookmark = _bookmark(app, start_us=1_000_000, end_us=2_000_000, loop_enabled=False)
    app._on_play_bookmark_requested(bookmark.id)
    assert _marks(app, bookmark.id) == ("playing", "playing", "playing")
    qtbot.waitUntil(lambda: adapter.get_status().state == "playing", timeout=3000)
    qtbot.wait(int(application_module.BOOKMARK_PLAYBACK_GRACE_S * 1000) + 300)
    assert _marks(app, bookmark.id)[1] == "playing"  # still inside it
    adapter.advance_time_us(1_500_000)  # past the end
    qtbot.waitUntil(lambda: _marks(app, bookmark.id) == ("done", "done", "done"), timeout=3000)


def test_the_next_bookmark_takes_over_and_a_deleted_one_loses_its_colour(qtbot, tmp_path) -> None:
    app = _make_app(qtbot, tmp_path)
    first = _bookmark(app, start_us=1_000_000, end_us=2_000_000, loop_enabled=True)
    second = _bookmark(app, start_us=3_000_000, end_us=4_000_000, loop_enabled=True)
    app._on_loop_bookmark_requested(first.id)
    app._on_loop_bookmark_requested(second.id)
    assert _marks(app, second.id) == ("playing", "playing", "playing")
    assert _marks(app, first.id)[1:] == (None, None)  # only one at a time
    app.window._on_delete_bookmark_requested([second.id])
    assert app._bookmark_playback is None
    assert app.window._playlist_panel.bookmark_song() is None
