from __future__ import annotations

from uuid import uuid4

from bookmark_studio.playlist.similarity import (
    SimilarityDecision,
    adjacency_similarity,
    decide,
    multiset_jaccard,
    similarity_score,
)


def test_multiset_jaccard_identical() -> None:
    ids = [uuid4() for _ in range(4)]
    assert multiset_jaccard(ids, list(ids)) == 1.0


def test_multiset_jaccard_disjoint() -> None:
    a = [uuid4() for _ in range(3)]
    b = [uuid4() for _ in range(3)]
    assert multiset_jaccard(a, b) == 0.0


def test_multiset_jaccard_respects_duplicates() -> None:
    x, y = uuid4(), uuid4()
    a = [x, x, y]
    b = [x, y]
    # intersection multiset: x:1, y:1 = 2 ; union multiset: x:2, y:1 = 3
    assert multiset_jaccard(a, b) == 2 / 3


def test_multiset_jaccard_both_empty() -> None:
    assert multiset_jaccard([], []) == 1.0


def test_adjacency_similarity_reordered_breaks_pairs() -> None:
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    original = [a, b, c, d]
    reordered = [a, c, b, d]
    # same multiset, but adjacency drops since neighbor pairs differ
    assert adjacency_similarity(original, reordered) < 1.0
    assert multiset_jaccard(original, reordered) == 1.0


def test_similarity_score_one_item_inserted() -> None:
    """spec #12 example: A,B,C,D vs A,B,X,C,D should score high, not become unrelated."""
    a, b, c, d, x = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    previous = [a, b, c, d]
    current = [a, b, x, c, d]
    score = similarity_score(previous, current)
    assert score > 0.5


def test_similarity_score_completely_unrelated_is_low() -> None:
    previous = [uuid4() for _ in range(5)]
    current = [uuid4() for _ in range(5)]
    assert similarity_score(previous, current) < 0.1


def test_decide_auto_match_requires_margin() -> None:
    a, b = uuid4(), uuid4()
    # top score clears threshold but runner-up is too close -> ask_user, not auto match
    decision, candidate = decide([(a, 0.96), (b, 0.90)])
    assert decision == SimilarityDecision.ASK_USER


def test_decide_auto_match_with_clear_margin() -> None:
    a, b = uuid4(), uuid4()
    decision, candidate = decide([(a, 0.98), (b, 0.10)])
    assert decision == SimilarityDecision.AUTO_MATCH
    assert candidate == a


def test_decide_below_ask_threshold_is_new_context() -> None:
    a = uuid4()
    decision, candidate = decide([(a, 0.5)])
    assert decision == SimilarityDecision.NEW_CONTEXT
    assert candidate is None


def test_decide_no_candidates_is_new_context() -> None:
    decision, candidate = decide([])
    assert decision == SimilarityDecision.NEW_CONTEXT
    assert candidate is None
