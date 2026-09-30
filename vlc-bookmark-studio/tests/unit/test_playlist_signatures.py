from __future__ import annotations

from uuid import uuid4

from bookmark_studio.playlist.signatures import strict_signature


def test_signature_is_order_sensitive() -> None:
    a, b = uuid4(), uuid4()
    assert strict_signature([a, b]) != strict_signature([b, a])


def test_signature_preserves_duplicates() -> None:
    a, b = uuid4(), uuid4()
    assert strict_signature([a, a, b]) != strict_signature([a, b])


def test_signature_is_deterministic() -> None:
    ids = [uuid4(), uuid4(), uuid4()]
    assert strict_signature(ids) == strict_signature(list(ids))
