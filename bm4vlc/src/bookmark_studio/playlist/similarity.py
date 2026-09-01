"""Multiset-Jaccard + adjacency similarity scoring for evolving playlists (spec #12)."""
from __future__ import annotations

SIMILARITY_AUTO_MATCH_THRESHOLD = 0.95
SIMILARITY_AUTO_MATCH_MARGIN = 0.10
SIMILARITY_ASK_USER_THRESHOLD = 0.75
JACCARD_WEIGHT = 0.70
ADJACENCY_WEIGHT = 0.30
