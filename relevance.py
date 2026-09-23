"""Provisional retrieval-only decisions; not an answerability judgment."""

from __future__ import annotations

import math
from enum import StrEnum

from config import (
    RELEVANCE_ACCEPT_THRESHOLD,
    RELEVANCE_REJECT_THRESHOLD,
    validate_relevance_thresholds,
)


class RelevanceDecision(StrEnum):
    ACCEPT = "ACCEPT"
    UNCERTAIN = "UNCERTAIN"
    REJECT = "REJECT"


def classify_relevance(
    semantic_score: float | None,
    *,
    reject_threshold: float = RELEVANCE_REJECT_THRESHOLD,
    accept_threshold: float = RELEVANCE_ACCEPT_THRESHOLD,
) -> RelevanceDecision:
    """Classify cosine similarity without consulting lexical or RRF scores."""
    validate_relevance_thresholds(reject_threshold, accept_threshold)
    if semantic_score is None:
        return RelevanceDecision.UNCERTAIN
    if type(semantic_score) not in (int, float):
        raise ValueError("semantic_score must be finite or None")
    try:
        finite = math.isfinite(semantic_score)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("semantic_score must be finite or None")
    if semantic_score < reject_threshold:
        return RelevanceDecision.REJECT
    if semantic_score < accept_threshold:
        return RelevanceDecision.UNCERTAIN
    return RelevanceDecision.ACCEPT
