"""0.4.0: the crash when adjusting loop settings (fades) in the Inspector, and bookmark
names that carry their playback range."""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from uuid import uuid4

import pytest
from PySide6.QtGui import QUndoStack

from bookmark_studio.app.commands import ChangeLoopCommand, MoveBookmarkCommand, ResizeBookmarkCommand
from bookmark_studio.domain.bookmark import Bookmark, default_bookmark_name, is_automatic_name, with_range
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.domain.media import Media
from bookmark_studio.domain.playlist import Playlist
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.ui.bookmark_panel import FADE_IN_COLUMN, FADE_OUT_COLUMN
from bookmark_studio.ui.main_window import MainWindow

CREATED = datetime(2026, 9, 30)


@pytest.fixture()
def repo() -> BookmarkRepository:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return BookmarkRepository(conn)


def _media(repo: BookmarkRepository) -> Media:
    media = Media(id=uuid4(), canonical_uri="file:///song.mp3", filename="song.mp3", title="Song", artist=None,
                  album=None, duration_us=200_000_000, file_size=1, mtime_ns=1, fast_fingerprint="fp")
    MediaRepository(repo.connection).insert(media)
    return media


def _segment(media_id, name=None, start_us=1_000_000, end_us=3_000_000, **overrides) -> Bookmark:
    values = dict(
        id=uuid4(), playlist_id=None, media_id=media_id, scope=BookmarkScope.GLOBAL_MEDIA, lane_id=None,
        bookmark_type=BookmarkType.SEGMENT if end_us is not None else BookmarkType.POINT,
        name=name or default_bookmark_name(start_us, end_us, created=CREATED, suffix="k3x9qa"),
        start_us=start_us, end_us=end_us, loop_enabled=end_us is not None, repeat_count=None, loop_gap_ms=0,
        completion_action=CompletionAction.CONTINUE,
    )
    values.update(overrides)
    return Bookmark(**values)


# -- the crash: loop settings from the Inspector --


@pytest.fixture()
def window_with_bookmark(qtbot):
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    repo = BookmarkRepository(conn)
    media = _media(repo)
    playlist = Playlist(id=uuid4(), name="Mix", source_uri=None, is_ad_hoc=True)
    PlaylistRepository(conn).insert(playlist)
    bookmark = _segment(media.id, playlist_id=playlist.id, scope=BookmarkScope.PLAYLIST_MEDIA)
    repo.insert(bookmark)
    window = MainWindow(repo)
    qtbot.addWidget(window)
    window.set_context(playlist_name="Mix", track_name="Song", playlist_id=playlist.id, media_id=media.id,
                       bookmark_count=1, duration_us=media.duration_us)
    window.load_all_bookmarks([bookmark], {media.id: "Song"})
    window.show()  # the list only opens its in-place editors when visible
    qtbot.waitExposed(window)
    window._bookmark_panel.select_bookmark(bookmark.id)
    qtbot.waitUntil(lambda: window._inspector.current_bookmark() is not None, timeout=3000)
    return window, repo, bookmark


def test_stepping_fades_in_the_inspector_saves_and_merges_into_one_undo_step(window_with_bookmark) -> None:
    """Every loop setting changed in the Inspector used to crash the app: the combo box
    handed back "continue" (a str), saving failed inside QUndoCommand.redo(), and the
    escaped exception left PySide6 broken until an access violation."""
    window, repo, bookmark = window_with_bookmark
    inspector = window._inspector
    for _ in range(5):
        inspector._fade_in_spin.stepBy(100)
    for _ in range(3):
        inspector._fade_out_spin.stepBy(250)
    inspector._gap_spin.stepBy(40)

    saved = repo.get(bookmark.id)
    assert (saved.fade_in_ms, saved.fade_out_ms, saved.loop_gap_ms) == (500, 750, 40)
    assert saved.completion_action is CompletionAction.CONTINUE
    assert window._undo_stack.count() == 1  # consecutive loop edits merge
    window._undo_stack.undo()
    restored = repo.get(bookmark.id)
    assert (restored.fade_in_ms, restored.fade_out_ms, restored.loop_gap_ms) == (0, 0, 0)


def test_changing_after_loop_in_the_inspector_stores_the_enum(window_with_bookmark) -> None:
    window, repo, bookmark = window_with_bookmark
    combo = window._inspector._completion_combo
    combo.setCurrentIndex(combo.findData(CompletionAction.PAUSE.value))
    saved = repo.get(bookmark.id)
    assert saved.completion_action is CompletionAction.PAUSE
    window._inspector._fade_in_spin.stepBy(10)  # the next edit still works
    assert repo.get(bookmark.id).fade_in_ms == 10


def test_fade_columns_in_the_bookmark_list_save(window_with_bookmark, qtbot) -> None:
    window, repo, bookmark = window_with_bookmark
    tree = window._bookmark_panel._tree
    for column, expected in ((FADE_IN_COLUMN, "fade_in_ms"), (FADE_OUT_COLUMN, "fade_out_ms")):
        item = tree.topLevelItem(0)
        tree.setCurrentItem(item, 0)
        tree.setCurrentItem(item, column)
        editor = tree.indexWidget(tree.currentIndex())
        editor.hidePopup()
        editor.setCurrentIndex(2)
        value = int(editor.currentText().split()[0])
        delegate = tree.itemDelegate()
        delegate.commitData.emit(editor)
        delegate.closeEditor.emit(editor)
        qtbot.waitUntil(lambda: getattr(repo.get(bookmark.id), expected) == value, timeout=3000)


def test_bookmark_accepts_enum_values_given_as_strings() -> None:
    bookmark = _segment(uuid4(), completion_action="pause", scope="global_media", bookmark_type="segment")
    assert bookmark.completion_action is CompletionAction.PAUSE
    assert bookmark.scope is BookmarkScope.GLOBAL_MEDIA
    assert bookmark.bookmark_type is BookmarkType.SEGMENT
    with pytest.raises(ValueError):
        _segment(uuid4(), completion_action="explode")


def test_a_failing_command_is_logged_not_raised_into_qt(repo, caplog) -> None:
    media = _media(repo)
    bookmark = _segment(media.id)
    repo.insert(bookmark)
    stack = QUndoStack()

    class _Broken(BookmarkRepository):
        def update(self, _bookmark):  # noqa: ANN001
            raise RuntimeError("disk on fire")

    broken = _Broken(repo.connection)
    with caplog.at_level(logging.ERROR, logger="bookmark_studio"):
        stack.push(ChangeLoopCommand(broken, bookmark.id, old=(True, None, 0, CompletionAction.CONTINUE, 0, 0),
                                     new=(True, None, 0, CompletionAction.CONTINUE, 100, 0)))
    assert "Change loop settings failed" in caplog.text
    # The stack keeps working afterwards.
    stack.push(ChangeLoopCommand(repo, bookmark.id, old=(True, None, 0, CompletionAction.CONTINUE, 0, 0),
                                 new=(True, None, 0, CompletionAction.CONTINUE, 200, 0)))
    assert repo.get(bookmark.id).fade_in_ms == 200


# -- names: <date>-<random>-<start>[-<end>] --


def test_automatic_name_is_recognised_only_for_its_own_range() -> None:
    name = default_bookmark_name(1_000_000, 3_000_000, created=CREATED, suffix="k3x9qa")
    assert name == "20260930-k3x9qa-00:00:01.000-00:00:03.000"
    assert is_automatic_name(name, 1_000_000, 3_000_000)
    assert not is_automatic_name(name, 1_000_000, 4_000_000)
    assert not is_automatic_name(name, 1_000_000, None)
    assert not is_automatic_name("Chorus", 1_000_000, 3_000_000)
    assert not is_automatic_name("bookmark-20260930-k3x9qa", 1_000_000, 3_000_000)  # 0.3 names stay


def test_with_range_renames_automatic_names_and_keeps_typed_ones() -> None:
    auto = _segment(uuid4())
    moved = with_range(auto, 5_500_000, 8_250_000)
    assert moved.name == "20260930-k3x9qa-00:00:05.500-00:00:08.250"
    assert (moved.start_us, moved.end_us) == (5_500_000, 8_250_000)

    typed = _segment(uuid4(), name="Chorus")
    assert with_range(typed, 5_500_000, 8_250_000).name == "Chorus"

    point = _segment(uuid4(), start_us=2_000_000, end_us=None)
    assert with_range(point, 4_000_000, None).name == "20260930-k3x9qa-00:00:04.000"


def test_dragging_renames_an_automatic_name_and_undo_restores_it(repo) -> None:
    media = _media(repo)
    auto = _segment(media.id)
    typed = _segment(media.id, name="Solo", start_us=10_000_000, end_us=12_000_000)
    repo.insert(auto)
    repo.insert(typed)
    stack = QUndoStack()
    # A drag is many small moves that Qt merges into one undo step.
    for delta in (100_000, 200_000, 300_000):
        stack.push(MoveBookmarkCommand(repo, auto.id, 1_000_000, 3_000_000, 1_000_000 + delta, 3_000_000 + delta))
    stack.push(ResizeBookmarkCommand(repo, typed.id, "end", 12_000_000, 13_500_000))

    assert repo.get(auto.id).name == "20260930-k3x9qa-00:00:01.300-00:00:03.300"
    assert repo.get(typed.id).name == "Solo"
    assert (repo.get(typed.id).start_us, repo.get(typed.id).end_us) == (10_000_000, 13_500_000)

    stack.undo()  # the resize
    stack.undo()  # the whole drag
    assert repo.get(auto.id).name == "20260930-k3x9qa-00:00:01.000-00:00:03.000"
    assert (repo.get(auto.id).start_us, repo.get(auto.id).end_us) == (1_000_000, 3_000_000)
    stack.redo()
    assert repo.get(auto.id).name == "20260930-k3x9qa-00:00:01.300-00:00:03.300"


def test_resizing_either_edge_updates_the_matching_part(repo) -> None:
    media = _media(repo)
    auto = _segment(media.id)
    repo.insert(auto)
    stack = QUndoStack()
    stack.push(ResizeBookmarkCommand(repo, auto.id, "start", 1_000_000, 500_000))
    stack.push(ResizeBookmarkCommand(repo, auto.id, "end", 3_000_000, 61_000_000))
    assert repo.get(auto.id).name == "20260930-k3x9qa-00:00:00.500-00:01:01.000"


def test_a_new_bookmark_from_the_selection_is_named_after_its_range(qtbot) -> None:
    from bookmark_studio.domain.selection import Selection

    conn = sqlite3.connect(":memory:")
    migrate(conn)
    repo = BookmarkRepository(conn)
    media = _media(repo)
    window = MainWindow(repo)
    qtbot.addWidget(window)
    window.set_context(playlist_name="", track_name="Song", playlist_id=None, media_id=media.id,
                       bookmark_count=0, duration_us=media.duration_us)
    window._waveform_scene.set_selection(Selection(start_us=83_456_000, end_us=105_000_000))
    window._bookmark_selection_button.click()
    (created,) = repo.list_global_for_media(media.id)
    assert re.fullmatch(r"\d{8}-[a-z0-9]{6}-00:01:23\.456-00:01:45\.000", created.name)


# -- rebrand (bm4vlc -> VLC Bookmark Studio): what existed before keeps working --


def test_new_environment_names_win_and_old_ones_still_work(monkeypatch) -> None:
    from bookmark_studio import platform_support as ps

    monkeypatch.delenv("VLC_BOOKMARK_STUDIO_LIBVLC_DIR", raising=False)
    monkeypatch.setenv("BM4VLC_LIBVLC_DIR", "/old")
    assert ps.env_setting("LIBVLC_DIR") == "/old"
    monkeypatch.setenv("VLC_BOOKMARK_STUDIO_LIBVLC_DIR", "/new")
    assert ps.env_setting("LIBVLC_DIR") == "/new"
    monkeypatch.delenv("VLC_BOOKMARK_STUDIO_DATA_DIR", raising=False)
    monkeypatch.setenv("BM4VLC_DATA_DIR", "/tmp/old-data")
    assert str(ps.user_data_dir()).replace("\\", "/").endswith("/tmp/old-data")


def test_sync_files_from_0_3_installations_are_still_merged(tmp_path) -> None:
    from bookmark_studio.project.sync_service import LEGACY_SYNC_FILE_PREFIX, SyncService

    folder = tmp_path / "shared"
    old_conn, new_conn = sqlite3.connect(tmp_path / "old.db"), sqlite3.connect(tmp_path / "new.db")
    for conn in (old_conn, new_conn):
        migrate(conn)
    old_repo = BookmarkRepository(old_conn)
    bookmark = _segment(_media(old_repo).id)
    old_repo.insert(bookmark)
    old = SyncService(old_conn, folder)
    old.export_now()
    legacy_file = folder / f"{LEGACY_SYNC_FILE_PREFIX}{old.machine_id}.vlcbmk"
    old.own_path.rename(legacy_file)  # what a 0.3 installation wrote

    new = SyncService(new_conn, folder)
    (folder / f"{LEGACY_SYNC_FILE_PREFIX}{new.machine_id}.vlcbmk").write_bytes(b"our own old file: ignored")
    assert [p.name for p in new.other_files()] == [legacy_file.name]
    assert new.sync_now() is True
    assert BookmarkRepository(new_conn).get(bookmark.id) is not None
    assert new.own_path.name.startswith("vlc-bookmark-studio-sync-")


def test_old_settings_are_adopted_once_and_left_in_place(tmp_path) -> None:
    from PySide6.QtCore import QSettings

    from bookmark_studio.settings.settings_service import _adopt_legacy_settings

    legacy = QSettings(str(tmp_path / "old.ini"), QSettings.Format.IniFormat)
    legacy.setValue("vlc/path", r"C:\VLC\vlc.exe")
    legacy.setValue("sync/dir", "/mnt/c/sync")
    legacy.sync()
    new = QSettings(str(tmp_path / "new.ini"), QSettings.Format.IniFormat)
    _adopt_legacy_settings(new, legacy)
    assert new.value("vlc/path") == r"C:\VLC\vlc.exe" and new.value("sync/dir") == "/mnt/c/sync"
    assert legacy.value("vlc/path") == r"C:\VLC\vlc.exe"  # never removed

    new.setValue("vlc/path", r"D:\VLC\vlc.exe")
    legacy.setValue("vlc/path", r"E:\changed-later\vlc.exe")
    _adopt_legacy_settings(new, legacy)  # the new location is in use: nothing copied again
    assert new.value("vlc/path") == r"D:\VLC\vlc.exe"


def test_cli_and_self_test_use_the_new_name(capsys) -> None:
    from bookmark_studio import bootstrap

    assert bootstrap.build_arg_parser().prog == "vlc-bookmark-studio"
    with pytest.raises(SystemExit):
        bootstrap.main(["x", "--version"])
    assert capsys.readouterr().out.startswith("vlc-bookmark-studio ")
