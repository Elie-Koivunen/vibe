"""Regression tests for the bugs fixed in 0.2.0 (each one was first reproduced by a
failing probe against 0.1.0 -- see CHANGELOG.md)."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import wave
import zipfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox

from bookmark_studio.app.application import Application
from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction, LoopState
from bookmark_studio.domain.loop import LoopSpec
from bookmark_studio.domain.media import Media
from bookmark_studio.domain.playlist import Playlist
from bookmark_studio.domain.selection import Selection
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.database import connect
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.migrations import current_version, migrate
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.playback.command_queue import ThreadedCommandQueue
from bookmark_studio.playback.loop_controller import LoopController
from bookmark_studio.playback.mock_adapter import MockPlaybackAdapter
from bookmark_studio.playback.playback_clock import PlaybackClock
from bookmark_studio.playback.status import PlaybackStatus, VlcPlaylistItem
from bookmark_studio.playlist.recognition import PlaylistRecognitionService
from bookmark_studio.playlist.synchronizer import PlaylistSynchronizer, SyncAction
from bookmark_studio.ui.bookmark_panel import GAP_COLUMN, LOOP_COLUMN, BookmarkPanel, _loop_label


def _wav(path: Path, seconds: float = 3.0, sr: int = 8000) -> Path:
    n = int(seconds * sr)
    samples = (np.sin(2 * np.pi * 440 * np.arange(n) / sr) * 0.3 * 32767).astype(np.int16)
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(samples.tobytes())
    return path


def _segment(media_id, playlist_id, **overrides) -> Bookmark:
    values = dict(
        id=uuid4(), playlist_id=playlist_id, media_id=media_id,
        scope=BookmarkScope.PLAYLIST_MEDIA if playlist_id else BookmarkScope.GLOBAL_MEDIA,
        lane_id=None, bookmark_type=BookmarkType.SEGMENT, name="b", start_us=0, end_us=300_000,
        loop_enabled=True, repeat_count=None, loop_gap_ms=0, completion_action=CompletionAction.CONTINUE,
    )
    values.update(overrides)
    return Bookmark(**values)


@pytest.fixture()
def make_app(qtbot, tmp_path):
    holder: dict = {}

    def _make(adapter, **kwargs):
        conn = connect(tmp_path / "t.db")
        migrate(conn)
        app = Application(conn=conn, adapter=adapter, ffmpeg_path="/nonexistent-ffmpeg",
                          waveform_cache_dir=tmp_path / "cache", **kwargs)
        qtbot.addWidget(app.window)
        holder["app"] = app
        return app

    yield _make
    if "app" in holder:
        holder["app"].stop()


def _two_song_app(make_app, qtbot, tmp_path, **kwargs):
    a = _wav(tmp_path / "a.wav")
    b = _wav(tmp_path / "b.wav", seconds=4.0)
    adapter = MockPlaybackAdapter([
        VlcPlaylistItem(1, a.resolve().as_uri(), "Song A", 3.0),
        VlcPlaylistItem(2, b.resolve().as_uri(), "Song B", 4.0),
    ])
    app = make_app(adapter, **kwargs)
    app.start()
    qtbot.waitUntil(
        lambda: app._current_media_id is not None and app.playlists.synchronizer.active_playlist_id is not None,
        timeout=5000,
    )
    qtbot.waitUntil(lambda: app.window._playlist_panel._tree.topLevelItemCount() == 2, timeout=5000)
    return app, adapter


# -- loops --


def test_loop_with_gap_resumes_by_itself(make_app, qtbot, tmp_path) -> None:
    """0.1.0: LoopController announced the gap and waited to be resumed, but nothing
    listened -- any bookmark with a Gap paused forever after its first pass."""
    app, adapter = _two_song_app(make_app, qtbot, tmp_path)
    bookmark = _segment(app._current_media_id, app.playlists.synchronizer.active_playlist_id, end_us=200_000, loop_gap_ms=100)
    app._bookmark_repository.insert(bookmark)
    iterations = []
    app._loop_controller.iteration_changed.connect(lambda remaining: iterations.append(remaining))
    app._on_loop_bookmark_requested(bookmark.id)
    qtbot.waitUntil(lambda: len(iterations) >= 2, timeout=4000)
    assert adapter.get_status().state == "playing"


def test_next_bookmark_completion_plays_the_next_bookmark(make_app, qtbot, tmp_path) -> None:
    app, adapter = _two_song_app(make_app, qtbot, tmp_path)
    playlist_id = app.playlists.synchronizer.active_playlist_id
    first = _segment(app._current_media_id, playlist_id, start_us=0, end_us=150_000, repeat_count=1,
                     completion_action=CompletionAction.NEXT_BOOKMARK)
    second = _segment(app._current_media_id, playlist_id, start_us=1_000_000, end_us=1_500_000,
                      loop_enabled=False, name="second")
    app._bookmark_repository.insert(first)
    app._bookmark_repository.insert(second)
    app._on_play_bookmark_requested(first.id)
    qtbot.waitUntil(lambda: adapter.get_status().time_us == 1_000_000, timeout=3000)
    assert adapter.get_status().state == "playing"


def test_stale_status_after_seek_back_does_not_count_a_pass_twice(make_app, qtbot, tmp_path) -> None:
    app, adapter = _two_song_app(make_app, qtbot, tmp_path)
    issued_before = time.monotonic_ns()
    app._on_loop_selection_requested(1_000_000, 2_000_000)
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.PLAYING, timeout=3000)
    remaining_before = app._loop_controller._remaining
    stale = PlaybackStatus(state="playing", time_us=2_050_000, position=0.0, rate=1.0,
                           current_playlist_item_id=1, duration_us=3_000_000, media_uri=None)
    app._on_status_result((issued_before, stale))  # sampled before the loop's seek landed
    assert app._loop_controller.state is LoopState.PLAYING
    assert app._loop_controller._remaining == remaining_before
    assert adapter.get_status().time_us == 1_000_000


def test_back_to_back_bookmarks_in_different_songs_each_switch_song(make_app, qtbot, tmp_path) -> None:
    """Found live from WSL: after playing a bookmark in song B, looping a bookmark of
    song A before the next status poll skipped the switch (the app still believed A was
    playing) and looped the wrong song."""
    app, adapter = _two_song_app(make_app, qtbot, tmp_path)
    (item_a, media_a), (item_b, media_b) = app.playlists.resolved
    playlist_id = app.playlists.synchronizer.active_playlist_id
    point_b = _segment(media_b.id, playlist_id, bookmark_type=BookmarkType.POINT, start_us=2_000_000,
                       end_us=None, loop_enabled=False)
    loop_a = _segment(media_a.id, playlist_id, start_us=1_000_000, end_us=1_300_000, repeat_count=1,
                      completion_action=CompletionAction.PAUSE)
    app._bookmark_repository.insert(point_b)
    app._bookmark_repository.insert(loop_a)
    app.session.status_timer.stop()  # worst case: no poll between the two commands
    app._on_play_bookmark_requested(point_b.id)
    app._on_play_bookmark_requested(loop_a.id)
    qtbot.waitUntil(lambda: app._loop_controller.state is LoopState.COMPLETED, timeout=3000)
    assert adapter.get_status().current_playlist_item_id == item_a.vlc_id


def test_finite_loop_with_fade_out_restores_volume(qtbot) -> None:
    adapter = MockPlaybackAdapter([VlcPlaylistItem(1, "file:///x.wav", "x", 60.0)])
    adapter.set_volume(200)
    controller = LoopController(adapter, PlaybackClock())
    controller.set_target_volume(200)
    controller.start(LoopSpec(start_us=0, end_us=300_000, repeat_count=1, gap_ms=0,
                              completion_action=CompletionAction.PAUSE, fade_out_ms=300))
    qtbot.waitUntil(lambda: controller.state is LoopState.COMPLETED, timeout=3000)
    assert adapter.get_status().volume == 200
    assert adapter.get_status().state == "paused"


def test_stop_mid_fade_restores_volume_and_ignores_the_ducked_level(qtbot) -> None:
    adapter = MockPlaybackAdapter([VlcPlaylistItem(1, "file:///x.wav", "x", 60.0)])
    adapter.set_volume(180)
    controller = LoopController(adapter, PlaybackClock())
    controller.set_target_volume(180)
    controller.start(LoopSpec(start_us=0, end_us=5_000_000, repeat_count=None, gap_ms=0,
                              completion_action=CompletionAction.CONTINUE, fade_in_ms=2_000))
    qtbot.wait(120)
    assert adapter.get_status().volume < 180
    controller.set_target_volume(adapter.get_status().volume)  # a poll mid-fade: ignored
    controller.stop()
    assert adapter.get_status().volume == 180
    controller.set_target_volume(90, sampled_at_ns=0)  # issued before our restore: ignored
    assert controller._target_volume == 180


def test_boundary_timer_accounts_for_playback_rate(qtbot) -> None:
    adapter = MockPlaybackAdapter([VlcPlaylistItem(1, "file:///x.wav", "x", 60.0)])
    clock = PlaybackClock()
    clock.update(PlaybackStatus(state="playing", time_us=0, position=0.0, rate=2.0,
                                current_playlist_item_id=1, duration_us=60_000_000, media_uri=None))
    controller = LoopController(adapter, clock)
    controller.start(LoopSpec(start_us=0, end_us=1_000_000, repeat_count=None, gap_ms=0,
                              completion_action=CompletionAction.CONTINUE))
    assert 450 <= controller._boundary_timer.interval() <= 550  # 1 s of media at 2x


def test_threaded_queue_keeps_order_and_reports_back(qtbot) -> None:
    queue = ThreadedCommandQueue()
    seen: list[int] = []
    done: list[int] = []
    for i in range(20):
        queue.submit(lambda i=i: seen.append(i), on_done=lambda _r, i=i: done.append(i), fences=("position",))
    qtbot.waitUntil(lambda: len(done) == 20, timeout=3000)
    assert seen == list(range(20)) and done == list(range(20))
    assert queue.fence_ns("position") > 0
    queue.shutdown()


def test_threaded_queue_coalesces_volume_ramps(qtbot) -> None:
    import threading

    queue = ThreadedCommandQueue()
    gate = threading.Event()
    queue.submit(gate.wait)  # hold the worker
    levels: list[int] = []
    for level in range(10):
        queue.submit(lambda level=level: levels.append(level), coalesce_key="volume")
    gate.set()
    qtbot.waitUntil(lambda: queue.is_idle(), timeout=3000)
    assert levels == [9]
    queue.shutdown()


# -- polling must not disturb the UI --


def test_poll_does_not_revert_text_being_typed_in_the_inspector(make_app, qtbot, tmp_path) -> None:
    app, adapter = _two_song_app(make_app, qtbot, tmp_path)
    bookmark = _segment(app._current_media_id, app.playlists.synchronizer.active_playlist_id, name="original")
    app._bookmark_repository.insert(bookmark)
    app._refresh_bookmark_views()
    app.window._bookmark_panel.select_bookmark(bookmark.id)
    app.window._inspector._name_edit.setText("half-typed new na")
    app._force_playlist_refresh()  # the worst case: a poll that does rebuild
    qtbot.wait(700)
    assert app.window._inspector._name_edit.text() == "half-typed new na"


def test_poll_does_not_yank_a_previewed_song_back(make_app, qtbot, tmp_path) -> None:
    app, adapter = _two_song_app(make_app, qtbot, tmp_path)
    media_a = app._current_media_id
    bookmark = _segment(media_a, app.playlists.synchronizer.active_playlist_id)
    app._bookmark_repository.insert(bookmark)
    app._refresh_bookmark_views()
    app.window._bookmark_panel.select_bookmark(bookmark.id)
    app.window._playlist_panel.select_item(2)
    media_b = app._current_media_id
    assert media_b != media_a
    app.window._waveform_scene.set_selection(Selection(start_us=500_000, end_us=1_500_000))
    app._force_playlist_refresh()
    qtbot.wait(2600)
    assert app._current_media_id == media_b
    assert app.window._waveform_scene.selection() is not None


def test_identical_refresh_does_not_rebuild_the_bookmark_list(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    bookmarks = [_segment(media_id, None)]
    panel.set_bookmarks(bookmarks, {media_id: "Song"})
    row = panel._tree.topLevelItem(0)
    panel._tree.setColumnWidth(1, 333)
    panel.set_bookmarks(list(bookmarks), {media_id: "Song"})
    assert panel._tree.topLevelItem(0) is row
    assert panel._tree.columnWidth(1) == 333


# -- bookmark list dropdowns --


def test_clicking_a_gap_cell_opens_its_dropdown(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    panel.resize(900, 300)
    panel.show()
    qtbot.waitExposed(panel)
    media_id = uuid4()
    panel.set_bookmarks([_segment(media_id, None, loop_gap_ms=750)], {media_id: "Song"})
    tree = panel._tree
    rect = tree.visualRect(tree.model().index(0, GAP_COLUMN))
    qtbot.mouseClick(tree.viewport(), Qt.LeftButton, pos=rect.center())
    qtbot.waitUntil(lambda: tree.findChild(QComboBox) is not None, timeout=2000)


def test_non_preset_values_survive_opening_the_dropdown(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    panel.resize(900, 300)
    panel.show()
    qtbot.waitExposed(panel)
    media_id = uuid4()
    panel.set_bookmarks([_segment(media_id, None, loop_gap_ms=750)], {media_id: "Song"})
    edits = []
    panel.gap_edited.connect(lambda _bid, ms: edits.append(ms))
    tree = panel._tree
    row = tree.topLevelItem(0)
    tree.setCurrentItem(row, GAP_COLUMN)
    qtbot.waitUntil(lambda: tree.findChild(QComboBox) is not None, timeout=2000)
    tree.setCurrentItem(row, 1)  # click away: commits whatever the editor shows
    qtbot.wait(50)
    assert edits == []
    assert row.text(GAP_COLUMN) == "750 ms"


def test_any_repeat_count_gets_its_own_label() -> None:
    assert _loop_label(True, 4) == "×4"
    assert _loop_label(True, None) == "∞"
    assert _loop_label(False, 4) == "Off"


def test_loop_column_edit_with_a_custom_count(qtbot) -> None:
    panel = BookmarkPanel()
    qtbot.addWidget(panel)
    media_id = uuid4()
    bookmark = _segment(media_id, None, loop_enabled=False)
    panel.set_bookmarks([bookmark], {media_id: "Song"})
    edits = []
    panel.loop_edited.connect(lambda *args: edits.append(args))
    panel._tree.topLevelItem(0).setText(LOOP_COLUMN, "×7")
    assert edits == [(bookmark.id, True, 7)]


# -- Inspector --


def test_tags_and_notes_are_saved_and_undoable(make_app, qtbot, tmp_path) -> None:
    app, adapter = _two_song_app(make_app, qtbot, tmp_path)
    bookmark = _segment(app._current_media_id, app.playlists.synchronizer.active_playlist_id)
    app._bookmark_repository.insert(bookmark)
    app.window._on_bookmark_activated(bookmark.id)
    inspector = app.window._inspector
    # 0.7.0: tags are picked from the tag list; the picker hands over its choice.
    inspector._tags_picker.tags_changed.emit(("verse", "practice", "verse"))
    inspector._notes_edit.setPlainText("watch the tempo")
    inspector._notes_edit.editingFinished.emit()
    saved = app._bookmark_repository.get(bookmark.id)
    assert saved.tags == ("practice", "verse")
    assert saved.notes == "watch the tempo"

    app.window._undo_stack.undo()  # notes
    assert app._bookmark_repository.get(bookmark.id).notes is None
    assert inspector._notes_edit.toPlainText() == ""


def test_undo_refreshes_the_inspector_and_uses_the_right_old_value(make_app, qtbot, tmp_path) -> None:
    app, adapter = _two_song_app(make_app, qtbot, tmp_path)
    bookmark = _segment(app._current_media_id, app.playlists.synchronizer.active_playlist_id, name="A")
    app._bookmark_repository.insert(bookmark)
    app.window._on_bookmark_activated(bookmark.id)
    inspector = app.window._inspector
    for name in ("B", "C"):
        inspector._name_edit.setText(name)
        inspector._on_name_committed()
    app.window._undo_stack.undo()
    assert app._bookmark_repository.get(bookmark.id).name == "B"
    assert inspector._name_edit.text() == "B"


def test_inspector_start_field_is_editable_after_loading_a_bookmark(qtbot) -> None:
    from bookmark_studio.ui.inspector import BookmarkInspector

    inspector = BookmarkInspector()
    qtbot.addWidget(inspector)
    inspector.load_bookmark(_segment(uuid4(), None))
    assert inspector._start_edit.isEnabled()


def test_clicking_a_bookmark_without_moving_it_adds_no_undo_step(make_app, qtbot, tmp_path) -> None:
    app, adapter = _two_song_app(make_app, qtbot, tmp_path)
    bookmark = _segment(app._current_media_id, app.playlists.synchronizer.active_playlist_id)
    app._bookmark_repository.insert(bookmark)
    count = app.window._undo_stack.count()
    app.window._on_bookmark_move_finished(bookmark.id, bookmark.start_us, bookmark.end_us)
    assert app.window._undo_stack.count() == count


# -- export / import --


def _db(tmp_path: Path, name: str = "t.db") -> sqlite3.Connection:
    conn = connect(tmp_path / name)
    migrate(conn)
    return conn


def test_export_contains_every_song_and_all_fields(make_app, qtbot, tmp_path) -> None:
    app, adapter = _two_song_app(make_app, qtbot, tmp_path)
    playlist_id = app.playlists.synchronizer.active_playlist_id
    (_item_a, media_a), (_item_b, media_b) = app.playlists.resolved
    app._bookmark_repository.insert(_segment(media_a.id, playlist_id, fade_in_ms=400, fade_out_ms=800, sort_index=3))
    app._bookmark_repository.insert(_segment(media_b.id, playlist_id, name="other song"))
    app.window.set_context(playlist_name="P", track_name="A", playlist_id=playlist_id,
                           media_id=media_a.id, bookmark_count=1)
    data = app.window.build_export_data()
    assert {b.media_id for b in data.bookmarks} == {media_a.id, media_b.id}
    assert {m.id for m in data.media} == {media_a.id, media_b.id}
    assert data.playlist_signatures and data.playlist_items

    from bookmark_studio.project.export_service import export_project
    from bookmark_studio.project.import_service import import_project

    archive = tmp_path / "p.vlcbmk"
    export_project(archive, data)
    other = _db(tmp_path, "other.db")
    import_project(other, archive)
    faded = [b for b in BookmarkRepository(other).list_for_playlist(playlist_id) if b.fade_in_ms]
    assert faded and (faded[0].fade_in_ms, faded[0].fade_out_ms, faded[0].sort_index) == (400, 800, 3)
    assert PlaylistRepository(other).list_item_media_ids(playlist_id) == [media_a.id, media_b.id]


def test_import_merges_instead_of_wiping_signatures(tmp_path: Path) -> None:
    from bookmark_studio.project.export_service import ProjectData, export_project
    from bookmark_studio.project.import_service import import_project

    conn = _db(tmp_path)
    repo = PlaylistRepository(conn)
    playlist = Playlist(id=uuid4(), name="P", source_uri=None, is_ad_hoc=True)
    repo.insert(playlist)
    repo.add_signature(playlist.id, "sig-abc")
    export_project(tmp_path / "x.vlcbmk", ProjectData(playlists=[playlist], media=[], bookmarks=[], lanes=[]))
    import_project(conn, tmp_path / "x.vlcbmk")
    assert repo.find_by_signature("sig-abc") is not None


def test_import_maps_media_and_playlists_onto_local_records(tmp_path: Path) -> None:
    """A project from another machine: same song (same fingerprint) and same playlist
    (same signature) under different ids must land on the local records."""
    from bookmark_studio.project.export_service import ProjectData, export_project
    from bookmark_studio.project.import_service import import_project

    conn = _db(tmp_path)
    local_media = Media(id=uuid4(), canonical_uri="file:///local/a.mp3", filename="a.mp3", title=None,
                        artist=None, album=None, duration_us=None, file_size=1, mtime_ns=1,
                        fast_fingerprint="fp-a")
    MediaRepository(conn).insert(local_media)
    local_playlist = Playlist(id=uuid4(), name="Local", source_uri=None, is_ad_hoc=True)
    PlaylistRepository(conn).insert(local_playlist)
    PlaylistRepository(conn).add_signature(local_playlist.id, "sig-shared")

    foreign_media = replace(local_media, id=uuid4(), canonical_uri="file:///elsewhere/a.mp3")
    foreign_playlist = Playlist(id=uuid4(), name="Theirs", source_uri=None, is_ad_hoc=True)
    bookmark = _segment(foreign_media.id, foreign_playlist.id)
    export_project(tmp_path / "x.vlcbmk", ProjectData(
        playlists=[foreign_playlist], media=[foreign_media], bookmarks=[bookmark], lanes=[],
        playlist_signatures=[(foreign_playlist.id, "sig-shared")],
    ))
    import_project(conn, tmp_path / "x.vlcbmk")
    imported = BookmarkRepository(conn).get(bookmark.id)
    assert imported.media_id == local_media.id
    assert imported.playlist_id == local_playlist.id


def test_version_1_archives_still_import(tmp_path: Path) -> None:
    from bookmark_studio.project.import_service import import_project
    from bookmark_studio.project.schema import bookmark_to_dict, media_to_dict

    media = Media(id=uuid4(), canonical_uri=None, filename=None, title=None, artist=None, album=None,
                  duration_us=None, file_size=None, mtime_ns=None, fast_fingerprint=None)
    bookmark_dict = bookmark_to_dict(_segment(media.id, None))
    for key in ("fade_in_ms", "fade_out_ms", "sort_index"):
        bookmark_dict.pop(key)
    archive = tmp_path / "v1.vlcbmk"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": "vlc-bookmark-studio", "format_version": 1}))
        z.writestr("bookmarks.json", json.dumps([bookmark_dict]))
        z.writestr("playlists.json", "[]")
        z.writestr("media.json", json.dumps([media_to_dict(media)]))
        z.writestr("lanes.json", "[]")
    plan = import_project(_db(tmp_path), archive)
    assert plan.bookmarks[0].fade_in_ms == 0


# -- playlist recognition across sessions --


def _media_ids(conn, count: int) -> list:
    repo = MediaRepository(conn)
    ids = []
    for i in range(count):
        media = Media(id=uuid4(), canonical_uri=f"file:///m{i}.mp3", filename=f"m{i}.mp3", title=None,
                      artist=None, album=None, duration_us=None, file_size=None, mtime_ns=None,
                      fast_fingerprint=None)
        repo.insert(media)
        ids.append(media.id)
    return ids


def _synchronizer(conn):
    repo = PlaylistRepository(conn)
    return PlaylistSynchronizer(repo, PlaylistRecognitionService(repo, repo.list_item_media_ids))


def test_an_edited_playlist_is_recognised_in_a_later_session(tmp_path: Path) -> None:
    """0.1.0 never stored playlist items, so adding one song between sessions created a
    new, empty bookmark context -- the old bookmarks seemed to vanish."""
    conn = _db(tmp_path)
    ids = _media_ids(conn, 21)
    first = _synchronizer(conn).on_snapshot(source_uri=None, ordered_media_ids=ids[:20])
    later = _synchronizer(conn).on_snapshot(source_uri=None, ordered_media_ids=ids)  # a new session
    assert later.action == SyncAction.MATCHED
    assert later.playlist_id == first.playlist_id


def test_launching_from_the_same_m3u_finds_the_playlist_and_names_it(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    ids = _media_ids(conn, 4)
    source = (tmp_path / "Guitar Practice.m3u").resolve().as_uri()
    first = _synchronizer(conn).on_snapshot(source_uri=source, ordered_media_ids=ids[:2])
    later = _synchronizer(conn).on_snapshot(source_uri=source, ordered_media_ids=ids[2:])
    assert later.playlist_id == first.playlist_id
    assert PlaylistRepository(conn).get(first.playlist_id).playlist.name == "Guitar Practice"


@pytest.mark.parametrize("answer", [True, False])
def test_similar_playlist_asks_the_user(make_app, qtbot, tmp_path, answer) -> None:
    """10 songs vs. a known 9-song playlist: 90% similar, inside the "ask" band."""
    questions = []
    items = [VlcPlaylistItem(i, (tmp_path / f"s{i}.wav").resolve().as_uri(), f"S{i}", 1.0) for i in range(1, 11)]
    adapter = MockPlaybackAdapter(items[:9])
    app = make_app(adapter, ask_playlist_match=lambda name, score: questions.append((name, score)) or answer)
    app.start()
    qtbot.waitUntil(lambda: app.playlists.synchronizer.active_playlist_id is not None, timeout=5000)
    original = app.playlists.synchronizer.active_playlist_id
    app._swap_adapter(MockPlaybackAdapter(items), mute_on_connect=False, new_vlc_process=None)
    qtbot.waitUntil(lambda: app.playlists.synchronizer.active_playlist_id is not None, timeout=5000)
    assert len(questions) == 1
    assert (app.playlists.synchronizer.active_playlist_id == original) is answer


# -- media identity --


def test_replaced_file_gets_a_new_fingerprint_but_keeps_its_bookmarks(tmp_path: Path) -> None:
    from bookmark_studio.media.resolver import MediaResolver

    conn = _db(tmp_path)
    song = _wav(tmp_path / "song.wav", seconds=1.0)
    resolver = MediaResolver(MediaRepository(conn))
    first = resolver.resolve(song.resolve().as_uri())
    _wav(song, seconds=2.5)  # same path, different content
    second = resolver.resolve(song.resolve().as_uri())
    assert second.id == first.id
    assert second.fast_fingerprint != first.fast_fingerprint


# -- persistence --


def test_a_failing_migration_leaves_no_partial_schema(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_base.sql").write_text("CREATE TABLE t (a INTEGER);", encoding="utf-8")
    (migrations / "002_broken.sql").write_text(
        "ALTER TABLE t ADD COLUMN b INTEGER;\nALTER TABLE missing ADD COLUMN c INTEGER;", encoding="utf-8"
    )
    conn = sqlite3.connect(tmp_path / "m.db")
    with pytest.raises(sqlite3.Error):
        migrate(conn, migrations)
    assert current_version(conn) == 1
    columns = [row[1] for row in conn.execute("PRAGMA table_info(t)")]
    assert columns == ["a"]


def test_listing_many_bookmarks_loads_their_tags(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    media_id = _media_ids(conn, 1)[0]
    repo = BookmarkRepository(conn)
    for i in range(30):
        repo.insert(_segment(media_id, None, tags=(f"t{i}", "shared"), start_us=i * 1000, end_us=i * 1000 + 500))
    listed = repo.list_global_for_media(media_id)
    assert len(listed) == 30
    assert all("shared" in b.tags and len(b.tags) == 2 for b in listed)


# -- waveform pipeline --


def test_ffmpeg_flooding_stderr_does_not_deadlock(monkeypatch) -> None:
    from bookmark_studio.waveform import ffmpeg_decoder

    script = (
        "import sys\n"
        "for _ in range(20000): sys.stderr.write('[mp3] Header missing\\n')\n"
        "sys.stderr.flush()\n"
        "sys.stdout.buffer.write(b'\\x00' * 64000)\n"
    )
    monkeypatch.setattr(ffmpeg_decoder, "build_ffmpeg_args", lambda _ff, _media: [sys.executable, "-c", script])
    started = time.monotonic()
    raw = ffmpeg_decoder.decode_media_to_pcm("ffmpeg", "x.mp3")
    assert len(raw) == 64000
    assert time.monotonic() - started < 20


def test_waveform_service_keeps_no_results_after_a_decode(tmp_path: Path, monkeypatch) -> None:
    from bookmark_studio.waveform import service as service_module
    from bookmark_studio.waveform.service import WaveformKey, WaveformService

    monkeypatch.setattr(service_module, "stream_media_pcm",
                        lambda *_a, **_k: iter([np.zeros(8000, dtype="<f4").tobytes()]))
    svc = WaveformService(ffmpeg_path="ffmpeg", cache_dir=tmp_path)
    generated = svc.generate(WaveformKey(uuid4(), "fp"), "x.wav")
    assert generated.pyramid.duration_us == 1_000_000
    assert svc._inflight == {}
    assert not hasattr(svc, "_results")


def test_prefetch_does_not_load_cached_waveforms(tmp_path: Path, qtbot, monkeypatch) -> None:
    from bookmark_studio.app import waveform_orchestrator as orch_module
    from bookmark_studio.app.waveform_orchestrator import WaveformOrchestrator
    from bookmark_studio.persistence.waveform_repository import WaveformCacheEntry, WaveformCacheRepository
    from bookmark_studio.waveform.service import WaveformService

    conn = _db(tmp_path)
    media_id = _media_ids(conn, 1)[0]
    repo = WaveformCacheRepository(conn)
    cache_file = tmp_path / "c.npz"
    cache_file.write_bytes(b"not really loaded")
    cache_id = WaveformService.compute_cache_key("fp", 8000, "mono")
    repo.put(WaveformCacheEntry(cache_id, media_id, 1, 8000, "mono", str(cache_file), duration_us=1_234_000))
    monkeypatch.setattr(orch_module, "load_pyramid", lambda *_a: pytest.fail("prefetch loaded a pyramid"))
    orchestrator = WaveformOrchestrator(service=WaveformService(ffmpeg_path="x", cache_dir=tmp_path), repository=repo)
    durations = []
    orchestrator.duration_known.connect(lambda mid, d: durations.append((mid, d)))
    orchestrator.prefetch(media_id, "fp", "x.wav")
    assert durations == [(media_id, 1_234_000)]


# -- VLC HTTP adapter --


def test_http_adapter_uses_the_target_songs_length_right_after_a_switch() -> None:
    """0.1.0 converted a seek to a percentage of the PREVIOUS song's length until the
    next status poll, so playing a bookmark in another song landed at the wrong time."""


    with FakeVlc([(3, "file:///long.mp3", 300.0), (4, "file:///short.mp3", 150.0)]) as vlc:
        adapter = vlc.adapter()
        adapter.get_playlist()
        adapter.get_status()  # current: item 3, 300 s
        adapter.goto_item(4)
        adapter.seek_absolute_us(60_000_000)
        assert abs(vlc.last_seek_percent - 40.0) < 0.01  # 60 s of 150 s, not of 300 s


def test_a_status_poll_in_flight_during_a_song_switch_does_not_misdirect_the_seek() -> None:
    """Found live: a poll sent just before goto_item() answered just after it, still
    describing the old song; the adapter adopted it as current, so the next seek was
    computed against the old song's length (VLC then ran off the end and stopped)."""
    import threading

    with FakeVlc([(3, "file:///long.mp3", 300.0), (4, "file:///short.mp3", 150.0)]) as vlc:
        adapter = vlc.adapter()
        adapter.get_playlist()
        adapter.get_status()
        vlc.delay_next_status_s = 0.4
        poller = threading.Thread(target=adapter.get_status)
        poller.start()
        time.sleep(0.1)  # the poll has reached "VLC" and will describe song 3
        adapter.goto_item(4)
        poller.join()  # ...and answers after the switch
        adapter.seek_absolute_us(60_000_000)
        assert abs(vlc.last_seek_percent - 40.0) < 0.01


def test_http_adapter_prefers_the_exact_decoded_length() -> None:


    with FakeVlc([(3, "file:///a.mp3", 205.8)]) as vlc:
        adapter = vlc.adapter()
        adapter.get_status()  # VLC says length 205 (whole seconds)
        adapter.seek_absolute_us(200_000_000)
        truncated = vlc.last_seek_percent
        adapter.set_exact_duration(3, 205_800_000)
        adapter.seek_absolute_us(200_000_000)
        assert abs(truncated - 200 / 205 * 100) < 0.01
        assert abs(vlc.last_seek_percent - 200 / 205.8 * 100) < 0.01


def test_http_adapter_reads_only_the_playlist_node() -> None:


    with FakeVlc([(3, "file:///a.mp3", -1.0)], media_library=[(9, "file:///ml.mp3", 10.0)]) as vlc:
        items = vlc.adapter().get_playlist()
        assert [i.vlc_id for i in items] == [3]
        assert items[0].duration_s is None  # VLC's -1 = not parsed yet


# -- a tiny stand-in for VLC's HTTP interface, stateful enough for goto/seek --

class FakeVlc:
    def __init__(self, items, media_library=()):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlparse

        fake = self
        self.items = list(items)
        self.media_library = list(media_library)
        self.current = self.items[0][0] if self.items else -1
        self.last_seek_percent: float | None = None
        self.delay_next_status_s = 0.0  # the next plain status poll answers late, describing the moment it arrived

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if parsed.path == "/requests/status.json":
                    command = query.get("command")
                    if command == "pl_play" and "id" in query:
                        fake.current = int(query["id"])
                    elif command == "seek" and query.get("val", "").endswith("%"):
                        fake.last_seek_percent = float(query["val"].rstrip("%"))
                    current = fake.current
                    if command is None and fake.delay_next_status_s:
                        delay, fake.delay_next_status_s = fake.delay_next_status_s, 0.0
                        time.sleep(delay)
                    length = next((d for i, _u, d in fake.items if i == current), -1)
                    body = json.dumps({
                        "state": "playing", "time": 0, "position": 0.0, "rate": 1.0,
                        "currentplid": current, "length": int(length) if length > 0 else -1,
                        "volume": 256,
                    }).encode()
                    content_type = "application/json"
                elif parsed.path == "/requests/playlist.xml":
                    def leaves(entries):
                        return "".join(
                            f'<leaf id="{i}" uri="{u}" name="n{i}" duration="{int(d) if d > 0 else -1}"/>'
                            for i, u, d in entries
                        )
                    body = (
                        '<node id="1" name="Undefined">'
                        f'<node id="2" name="Playlist">{leaves(fake.items)}</node>'
                        f'<node id="3" name="Media Library">{leaves(fake.media_library)}</node>'
                        "</node>"
                    ).encode()
                    content_type = "text/xml"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def adapter(self):
        from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter

        return StandardHttpPlaybackAdapter("127.0.0.1", self._server.server_address[1], "pw",
                                           goto_settle_timeout_s=1.0)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
