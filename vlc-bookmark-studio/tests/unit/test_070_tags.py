"""0.7.0: the tag catalog -- a ready-made, editable list of tags -- and how renaming or
removing a tag keeps every bookmark (and other machines, undo, old files) consistent."""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PySide6.QtGui import QUndoStack

from bookmark_studio.app.commands import EditBookmarkFieldsCommand
from bookmark_studio.domain.bookmark import Bookmark
from bookmark_studio.domain.enums import BookmarkScope, BookmarkType, CompletionAction
from bookmark_studio.domain.media import Media
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.database import connect
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.persistence.tag_repository import InvalidTagName, TagRepository, clean_tag_name
from bookmark_studio.project.export_service import ProjectData, export_project
from bookmark_studio.project.import_service import apply_import_plan, read_import_plan
from bookmark_studio.project.sync_service import SyncService

SEEDED = ["intro", "build-up", "drop", "peak", "breakdown", "outro", "game start", "game end"]


@pytest.fixture()
def dbs(tmp_path: Path):
    conns = []

    def _open(name: str):
        conn = connect(tmp_path / f"{name}.db")
        migrate(conn)
        conns.append(conn)
        return conn

    yield _open
    for conn in conns:
        conn.close()


def _media(conn, fingerprint: str = "fp-1") -> Media:
    media = Media(id=uuid4(), canonical_uri=f"file:///{fingerprint}.mp3", filename=f"{fingerprint}.mp3", title=None,
                  artist=None, album=None, duration_us=180_000_000, file_size=1, mtime_ns=None,
                  fast_fingerprint=fingerprint)
    MediaRepository(conn).insert(media)
    return media


def _bookmark(media_id: UUID, *tags: str, **overrides) -> Bookmark:
    values = dict(
        id=uuid4(), playlist_id=None, media_id=media_id, scope=BookmarkScope.GLOBAL_MEDIA, lane_id=None,
        bookmark_type=BookmarkType.SEGMENT, name="k3x9qa", start_us=1_000_000, end_us=2_000_000,
        loop_enabled=False, repeat_count=None, loop_gap_ms=0, completion_action=CompletionAction.CONTINUE,
        tags=tuple(tags),
    )
    values.update(overrides)
    return Bookmark(**values)


def _tags(conn, bookmark_id: UUID) -> set[str]:
    return set(BookmarkRepository(conn).get(bookmark_id).tags)


# -- the catalog --


def test_the_catalog_comes_ready_with_tags_and_keeps_existing_ones(dbs) -> None:
    conn = dbs("a")
    assert TagRepository(conn).names() == SEEDED
    media = _media(conn)
    repo = BookmarkRepository(conn)
    bookmark = _bookmark(media.id, "Drop", "verse")  # free text from before 0.7.0
    repo.insert(bookmark)
    assert _tags(conn, bookmark.id) == {"drop", "verse"}  # the catalog's spelling; "verse" joined it
    assert TagRepository(conn).names()[-1] == "verse"


def test_tag_names_are_checked() -> None:
    assert clean_tag_name("  game   start ") == "game start"
    for bad in ("", "   ", "a,b", "x" * 41):
        with pytest.raises(InvalidTagName):
            clean_tag_name(bad)


def test_add_move_and_no_duplicates(dbs) -> None:
    tags = TagRepository(dbs("a"))
    tags.add("crowd chant")
    with pytest.raises(InvalidTagName, match="already"):
        tags.add("CROWD CHANT")
    tags.move("crowd chant", -100)
    assert tags.names()[0] == "crowd chant"
    tags.move("intro", 1)
    assert tags.names()[:3] == ["crowd chant", "build-up", "intro"]


def test_renaming_a_tag_renames_it_on_every_bookmark(dbs) -> None:
    conn = dbs("a")
    repo, tags = BookmarkRepository(conn), TagRepository(conn)
    song_one, song_two = _media(conn, "one"), _media(conn, "two")
    first = _bookmark(song_one.id, "intro", "drop")
    second = _bookmark(song_two.id, "intro")
    other = _bookmark(song_two.id, "peak")
    for b in (first, second, other):
        repo.insert(b)
    before = repo.updated_at_by_id()
    time.sleep(0.02)  # a later moment than the inserts, as in real use
    assert tags.rename("intro", "opening") == 2
    assert _tags(conn, first.id) == {"opening", "drop"}
    assert _tags(conn, second.id) == {"opening"}
    assert _tags(conn, other.id) == {"peak"}
    assert "intro" not in tags.names() and tags.names()[0] == "opening"  # keeps its place
    after = repo.updated_at_by_id()  # changed bookmarks will sync
    assert after[str(first.id)] > before[str(first.id)] and after[str(other.id)] == before[str(other.id)]


def test_renaming_into_an_existing_tag_merges_them(dbs) -> None:
    conn = dbs("a")
    repo, tags = BookmarkRepository(conn), TagRepository(conn)
    media = _media(conn)
    both = _bookmark(media.id, "peak", "drop")
    one = _bookmark(media.id, "drop")
    repo.insert(both)
    repo.insert(one)
    assert tags.rename("drop", "Peak") == 2
    assert _tags(conn, both.id) == {"peak"} and _tags(conn, one.id) == {"peak"}
    assert "drop" not in tags.names()


def test_removing_a_tag_takes_it_off_every_bookmark(dbs) -> None:
    conn = dbs("a")
    repo, tags = BookmarkRepository(conn), TagRepository(conn)
    media = _media(conn)
    bookmark = _bookmark(media.id, "outro", "peak")
    repo.insert(bookmark)
    assert tags.remove("outro") == 1
    assert _tags(conn, bookmark.id) == {"peak"} and "outro" not in tags.names()
    assert tags.usage_counts() == {"peak": 1}


def test_stale_names_never_come_back(dbs) -> None:
    """Undo of an edit made before a rename, or any write still holding the old names,
    must not resurrect them."""
    conn = dbs("a")
    repo, tags = BookmarkRepository(conn), TagRepository(conn)
    media = _media(conn)
    bookmark = _bookmark(media.id, "intro")
    repo.insert(bookmark)
    stack = QUndoStack()
    stack.push(EditBookmarkFieldsCommand(repo, bookmark.id, "Edit bookmark tags",
                                         old={"tags": ("intro",)}, new={"tags": ("intro", "outro")}))
    tags.rename("intro", "opening")
    tags.remove("outro")
    stack.undo()  # writes ("intro",) back
    assert _tags(conn, bookmark.id) == {"opening"}
    stack.redo()  # writes ("intro", "outro")
    assert _tags(conn, bookmark.id) == {"opening"}
    repo.update(replace(repo.get(bookmark.id), tags=("INTRO", "outro", "drop")))
    assert _tags(conn, bookmark.id) == {"opening", "drop"}
    assert "intro" not in tags.names() and "outro" not in tags.names()


def test_rename_chains_and_reusing_an_old_name(dbs) -> None:
    conn = dbs("a")
    repo, tags = BookmarkRepository(conn), TagRepository(conn)
    media = _media(conn)
    tags.rename("intro", "opening")
    tags.rename("opening", "start")
    bookmark = _bookmark(media.id, "intro")  # two renames old
    repo.insert(bookmark)
    assert _tags(conn, bookmark.id) == {"start"}
    tags.add("intro")  # the old name as a new, separate tag
    fresh = _bookmark(media.id, "intro")
    repo.insert(fresh)
    assert _tags(conn, fresh.id) == {"intro"}
    assert _tags(conn, bookmark.id) == {"start"}


# -- other machines and old files --


def test_a_rename_reaches_another_machine_through_folder_sync(dbs, tmp_path) -> None:
    a, b = dbs("a"), dbs("b")
    folder = tmp_path / "sync"
    media_a = _media(a, "same-song")
    bookmark = _bookmark(media_a.id, "intro", "drop")
    BookmarkRepository(a).insert(bookmark)
    sync_a, sync_b = SyncService(a, folder), SyncService(b, folder)
    sync_a.sync_now()
    sync_b.sync_now()
    assert _tags(b, bookmark.id) == {"intro", "drop"}

    # B gets another bookmark with "intro" before hearing of A's rename.
    media_b = MediaRepository(b).list_all()[0]
    local_b = _bookmark(media_b.id, "intro", start_us=5_000_000, end_us=6_000_000)
    BookmarkRepository(b).insert(local_b)
    TagRepository(a).rename("intro", "opening")
    TagRepository(a).remove("drop")
    sync_a.sync_now()
    sync_b.sync_now()
    assert _tags(b, bookmark.id) == {"opening"}
    assert _tags(b, local_b.id) == {"opening"}  # B's own bookmark follows the rename too
    names_b = TagRepository(b).names()
    assert "opening" in names_b and "intro" not in names_b and "drop" not in names_b

    sync_a.sync_now()  # and B's bookmark arrives at A under the new name
    assert _tags(a, local_b.id) == {"opening"}


def test_a_later_decision_here_wins_over_an_older_one_there(dbs, tmp_path) -> None:
    a, b = dbs("a"), dbs("b")
    folder = tmp_path / "sync"
    sync_a, sync_b = SyncService(a, folder), SyncService(b, folder)
    TagRepository(a).rename("peak", "climax")
    TagRepository(b).rename("peak", "top")  # later
    sync_a.sync_now()
    sync_b.sync_now()
    sync_a.sync_now()
    for conn in (a, b):
        names = TagRepository(conn).names()
        assert "top" in names and "peak" not in names


def test_an_old_project_file_cannot_bring_back_removed_or_renamed_tags(dbs, tmp_path) -> None:
    conn = dbs("a")
    media = _media(conn)
    bookmark = _bookmark(media.id, "intro", "outro")
    path = tmp_path / "old.vlcbmk"
    export_project(path, ProjectData(playlists=[], media=[media], bookmarks=[bookmark], lanes=[]))  # no tags.json
    TagRepository(conn).rename("intro", "opening")
    TagRepository(conn).remove("outro")
    apply_import_plan(conn, read_import_plan(path))
    assert _tags(conn, bookmark.id) == {"opening"}
    names = TagRepository(conn).names()
    assert "intro" not in names and "outro" not in names


# -- the tag picker (Bookmark tab) --


def test_the_picker_ticks_several_tags_and_commits_once_when_closed(qtbot) -> None:
    from bookmark_studio.ui.tag_picker import EDIT_TAGS_TEXT, TagPicker

    picker = TagPicker()
    qtbot.addWidget(picker)
    picker.set_catalog(SEEDED)
    picker.set_tags(("peak", "INTRO"))
    assert picker.tags() == ("intro", "peak")  # the catalog's order and spelling
    seen: list[tuple] = []
    picker.tags_changed.connect(seen.append)
    edits: list[bool] = []
    picker.edit_catalog_requested.connect(lambda: edits.append(True))

    menu = picker.menu()
    menu.aboutToShow.emit()
    actions = {a.text(): a for a in menu.actions()}
    assert [a.text() for a in menu.actions() if a.isCheckable()] == SEEDED
    assert actions["intro"].isChecked() and actions["peak"].isChecked()
    actions["drop"].trigger()
    actions["intro"].trigger()
    assert seen == []  # still open
    menu.aboutToHide.emit()
    assert seen == [("drop", "peak")]
    assert picker._button.text() == "drop, peak"
    actions[EDIT_TAGS_TEXT].trigger()
    assert edits == [True]


def test_the_picker_never_hides_a_tag_the_list_lacks(qtbot) -> None:
    from bookmark_studio.ui.tag_picker import TagPicker

    picker = TagPicker()
    qtbot.addWidget(picker)
    picker.set_catalog(["intro"])
    picker.set_tags(("intro", "legacy"))
    picker.menu().aboutToShow.emit()
    texts = [a.text() for a in picker.menu().actions() if a.isCheckable()]
    assert texts == ["intro", "legacy (not in the list)"]


# -- the Edit tags window --


def _dialog(qtbot, conn, answers=(True,), new_name=None):
    from bookmark_studio.ui.dialogs.tag_catalog_dialog import TagCatalogDialog

    questions: list[str] = []
    errors: list[str] = []
    replies = list(answers)
    dialog = TagCatalogDialog(
        TagRepository(conn), confirm=lambda _title, q: (questions.append(q), replies.pop(0) if replies else True)[1],
        ask_text=lambda *_args: new_name, show_error=errors.append,
    )
    qtbot.addWidget(dialog)
    return dialog, questions, errors


def test_the_window_adds_renames_removes_and_reorders(dbs, qtbot) -> None:
    conn = dbs("a")
    media = _media(conn)
    repo = BookmarkRepository(conn)
    bookmark = _bookmark(media.id, "intro", "drop")
    repo.insert(bookmark)
    dialog, questions, errors = _dialog(qtbot, conn)
    labels = [dialog._list.item(i).text() for i in range(dialog._list.count())]
    assert labels[0].startswith("intro") and "(1 bookmark)" in labels[0]  # usage shown

    assert dialog.add_tag("crowd chant") and dialog.names()[-1] == "crowd chant"
    assert not dialog.add_tag("Intro") and "already" in errors[-1]
    assert not dialog.add_tag("a, b") and "comma" in errors[-1]

    assert dialog.rename_tag("intro", "opening")
    assert "1 bookmark" in questions[-1]  # asked first, because a bookmark carries it
    assert _tags(conn, bookmark.id) == {"opening", "drop"}

    assert dialog.rename_tag("drop", "peak")  # into an existing tag: merge, after asking
    assert "Merge" in questions[-1]
    assert _tags(conn, bookmark.id) == {"opening", "peak"}

    assert dialog.remove_tag("peak")
    assert _tags(conn, bookmark.id) == {"opening"} and "peak" not in dialog.names()
    dialog.move_tag("crowd chant", -100)
    assert dialog.names()[0] == "crowd chant" and dialog.changed


def test_saying_no_changes_nothing(dbs, qtbot) -> None:
    conn = dbs("a")
    media = _media(conn)
    bookmark = _bookmark(media.id, "outro")
    BookmarkRepository(conn).insert(bookmark)
    dialog, questions, _errors = _dialog(qtbot, conn, answers=(False, False))
    assert not dialog.remove_tag("outro")
    assert not dialog.rename_tag("outro", "ending")
    assert _tags(conn, bookmark.id) == {"outro"} and "outro" in dialog.names() and not dialog.changed


def test_the_rename_button_asks_for_the_new_name(dbs, qtbot) -> None:
    conn = dbs("a")
    dialog, _questions, _errors = _dialog(qtbot, conn, new_name="Game Over")
    dialog._list.item(dialog.names().index("game end")).setSelected(True)
    dialog._rename_button.click()
    assert "Game Over" in dialog.names() and "game end" not in dialog.names()


# -- in the app --


def test_editing_tags_in_the_window_updates_every_view(qtbot, tmp_path, monkeypatch) -> None:
    from bookmark_studio.ui.dialogs import tag_catalog_dialog
    from tests.unit.test_050_ui import _bookmark as app_bookmark
    from tests.unit.test_050_ui import _make_app

    app = _make_app(qtbot, tmp_path)
    first = app_bookmark(app, tags=("intro",))
    second = app_bookmark(app, start_us=3_000_000, end_us=4_000_000, tags=("intro", "peak"))
    app.window._on_bookmark_activated(first.id)
    assert app.window._inspector._tags_picker.tags() == ("intro",)

    def fake_exec(dialog):  # the real window, driven as a user would, without blocking
        dialog._confirm = lambda *_args: True
        dialog.rename_tag("intro", "opening")
        return 0

    monkeypatch.setattr(tag_catalog_dialog.TagCatalogDialog, "exec", fake_exec)
    assert app.window.edit_tags()
    assert app.window._inspector._tags_picker.tags() == ("opening",)  # the Bookmark tab
    assert "opening" in app.window._inspector._tags_picker._catalog
    panel = app.window._bookmark_panel
    from bookmark_studio.ui.bookmark_panel import TAGS_COLUMN

    shown = {panel._tree.topLevelItem(i).text(TAGS_COLUMN) for i in range(panel._tree.topLevelItemCount())}
    assert shown == {"opening", "opening, peak"}  # the list, every row
    assert set(app._bookmark_repository.get(second.id).tags) == {"opening", "peak"}


def test_picking_tags_in_the_bookmark_tab_saves_them_with_undo(qtbot, tmp_path) -> None:
    from tests.unit.test_050_ui import _bookmark as app_bookmark
    from tests.unit.test_050_ui import _make_app

    app = _make_app(qtbot, tmp_path)
    bookmark = app_bookmark(app)
    app.window._on_bookmark_activated(bookmark.id)
    picker = app.window._inspector._tags_picker
    picker.menu().aboutToShow.emit()
    actions = {a.text(): a for a in picker.menu().actions()}
    actions["game start"].trigger()
    actions["drop"].trigger()
    picker.menu().aboutToHide.emit()
    assert set(app._bookmark_repository.get(bookmark.id).tags) == {"drop", "game start"}
    app.window._undo_stack.undo()
    assert app._bookmark_repository.get(bookmark.id).tags == ()
    assert picker.tags() == ()
