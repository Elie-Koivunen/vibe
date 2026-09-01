"""Multiset-Jaccard + adjacency similarity scoring for evolving playlists (spec #12)."""
from __future__ import annotations

from collections import Counter
from uuid import UUID

SIMILARITY_AUTO_MATCH_THRESHOLD = 0.95
SIMILARITY_AUTO_MATCH_MARGIN = 0.10
SIMILARITY_ASK_USER_THRESHOLD = 0.75
JACCARD_WEIGHT = 0.70
ADJACENCY_WEIGHT = 0.30

# Weighted-average scores land on float sums like 0.7*0.75 + 0.3*0.75 == 0.7499999999999999,
# so an exact-boundary case (e.g. a single item inserted into a 3-item playlist) would
# otherwise flip sides of a threshold purely from binary floating-point rounding, not from
# any real similarity difference. All threshold comparisons below tolerate this epsilon.
FLOAT_EPSILON = 1e-9


def multiset_jaccard(a: list[UUID], b: list[UUID]) -> float:
    """Jaccard similarity over multisets, so duplicate media are respected (spec #12)."""
    if not a and not b:
        return 1.0
    counts_a, counts_b = Counter(a), Counter(b)
    keys = set(counts_a) | set(counts_b)
    intersection = sum(min(counts_a[k], counts_b[k]) for k in keys)
    union = sum(max(counts_a[k], counts_b[k]) for k in keys)
    return intersection / union if union else 1.0


def _lcs_length(a: list[UUID], b: list[UUID]) -> int:
    """Longest common subsequence length, O(len(a)*len(b)) time, O(min) space.

    Chosen over an adjacent-pair (bigram) Jaccard: a single item inserted in the middle
    of a playlist breaks *every* adjacent pair after it, which under bigram Jaccard
    scores a one-item insertion as barely more similar than a wholesale reshuffle --
    directly contradicting spec #12's own worked example ("A,B,C,D" -> "A,B,X,C,D" "must
    not automatically become a completely unrelated bookmark project"). LCS instead
    measures how much of the relative order survived, so a single insertion barely
    dents the score while an actual reshuffle still lowers it correctly.
    """
    if len(a) < len(b):
        a, b = b, a
    previous = [0] * (len(b) + 1)
    for item_a in a:
        current = [0] * (len(b) + 1)
        for j, item_b in enumerate(b, start=1):
            if item_a == item_b:
                current[j] = previous[j - 1] + 1
            else:
                current[j] = max(previous[j], current[j - 1])
        previous = current
    return previous[-1]


def adjacency_similarity(a: list[UUID], b: list[UUID]) -> float:
    """Ordering similarity: LCS length over the longer sequence's length (spec #12)."""
    if not a and not b:
        return 1.0
    longest = max(len(a), len(b))
    return _lcs_length(a, b) / longest if longest else 1.0


def similarity_score(a: list[UUID], b: list[UUID]) -> float:
    """Combined score per spec #12: 0.70 * multiset Jaccard + 0.30 * adjacency similarity."""
    return JACCARD_WEIGHT * multiset_jaccard(a, b) + ADJACENCY_WEIGHT * adjacency_similarity(a, b)


class SimilarityDecision:
    AUTO_MATCH = "auto_match"
    ASK_USER = "ask_user"
    NEW_CONTEXT = "new_context"


def decide(scores: list[tuple[UUID, float]]) -> tuple[str, UUID | None]:
    """Applies the thresholds from spec #12 to a list of (playlist_id, score) candidates.

    Auto-match requires the top score to clear SIMILARITY_AUTO_MATCH_THRESHOLD AND lead
    the runner-up by at least SIMILARITY_AUTO_MATCH_MARGIN (spec #12's "unique candidate
    margin"), so two near-identical candidates never get silently auto-matched.
    """
    if not scores:
        return SimilarityDecision.NEW_CONTEXT, None

    ranked = sorted(scores, key=lambda pair: pair[1], reverse=True)
    top_id, top_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0.0

    if top_score >= SIMILARITY_AUTO_MATCH_THRESHOLD - FLOAT_EPSILON and (
        top_score - runner_up_score
    ) >= SIMILARITY_AUTO_MATCH_MARGIN - FLOAT_EPSILON:
        return SimilarityDecision.AUTO_MATCH, top_id
    if top_score >= SIMILARITY_ASK_USER_THRESHOLD - FLOAT_EPSILON:
        return SimilarityDecision.ASK_USER, top_id
    return SimilarityDecision.NEW_CONTEXT, None
