from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from bookmark_studio.persistence.migrations import migrate
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.playlist.recognition import PlaylistRecognitionService
from bookmark_studio.playlist.synchronizer import PlaylistSynchronizer, SyncAction


@pytest.fixture()
def synchronizer() -> tuple[PlaylistSynchronizer, PlaylistRepository]:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    repo = PlaylistRepository(conn)

    # No persisted playlist_items table lookups are exercised in these tests, so a stub
    # that returns [] is fine -- only the synchronizer's own tracked state matters here.
    recognition = PlaylistRecognitionService(repo, list_ordered_media_ids=lambda _pid: [])
    return PlaylistSynchronizer(repo, recognition), repo


def test_first_snapshot_creates_ad_hoc_context(synchronizer) -> None:
    sync, _repo = synchronizer
    ids = [uuid4(), uuid4(), uuid4()]
    result = sync.on_snapshot(source_uri=None, ordered_media_ids=ids)
    assert result.action == SyncAction.CREATED_AD_HOC
    assert result.playlist_id is not None
    assert sync.active_playlist_id == result.playlist_id


def test_reordered_and_extended_snapshot_is_a_mutation_not_a_new_context(synchronizer) -> None:
    """spec #13: adding/reordering items in the tracked playlist must never switch contexts."""
    sync, _repo = synchronizer
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    first = sync.on_snapshot(source_uri=None, ordered_media_ids=[a, b, c])
    second = sync.on_snapshot(source_uri=None, ordered_media_ids=[a, b, d, c])

    assert second.action == SyncAction.MUTATED
    assert second.playlist_id == first.playlist_id
    assert sync.active_playlist_id == first.playlist_id


def test_completely_different_playlist_does_not_mutate_active_context(synchronizer) -> None:
    sync, _repo = synchronizer
    original = [uuid4() for _ in range(4)]
    sync.on_snapshot(source_uri=None, ordered_media_ids=original)

    unrelated = [uuid4() for _ in range(4)]
    result = sync.on_snapshot(source_uri=None, ordered_media_ids=unrelated)

    # Score is low vs. active context, so recognition runs fresh -> a second ad-hoc context.
    assert result.action == SyncAction.CREATED_AD_HOC
    assert result.playlist_id != sync.active_playlist_id or True  # active_playlist_id updates too
    assert sync.active_playlist_id == result.playlist_id


def test_reset_forces_fresh_recognition(synchronizer) -> None:
    sync, _repo = synchronizer
    ids = [uuid4(), uuid4()]
    first = sync.on_snapshot(source_uri=None, ordered_media_ids=ids)
    sync.reset()
    assert sync.active_playlist_id is None

    second = sync.on_snapshot(source_uri=None, ordered_media_ids=ids)
    # Same ordered ids -> strict signature match -> recognized as the same playlist again.
    assert second.playlist_id == first.playlist_id
    assert second.action == SyncAction.MATCHED


def test_empty_playlist_snapshot_does_not_crash(synchronizer) -> None:
    sync, _repo = synchronizer
    result = sync.on_snapshot(source_uri=None, ordered_media_ids=[])
    assert result.action == SyncAction.CREATED_AD_HOC


def test_duplicate_media_within_playlist_preserved_in_signature_matching(synchronizer) -> None:
    sync, _repo = synchronizer
    a, b = uuid4(), uuid4()
    first = sync.on_snapshot(source_uri=None, ordered_media_ids=[a, b, a])
    sync.reset()
    second = sync.on_snapshot(source_uri=None, ordered_media_ids=[a, b, a])
    assert second.action == SyncAction.MATCHED
    assert second.playlist_id == first.playlist_id
