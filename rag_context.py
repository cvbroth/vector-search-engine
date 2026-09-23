"""Stable JSON evidence from the existing gated retrieval APIs."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from config import KNOWLEDGE_SCOPES, get_scope
from embeddings import EmbeddingError
from relevance import RelevanceDecision

if TYPE_CHECKING:
    from search import SearchResult

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class RagEvidence:
    scope: str
    rank: int
    fused_score: float
    semantic_score: float | None
    semantic_distance: float | None
    lexical_match: bool
    lexical_score: float | None
    relevance_decision: RelevanceDecision
    source_path: str
    filename: str
    page: int | None
    chunk_index: int
    text: str

    @classmethod
    def from_search_result(cls, result: SearchResult) -> RagEvidence:
        return cls(
            scope=result.scope,
            rank=result.rank,
            fused_score=result.fused_score,
            semantic_score=result.semantic_score,
            semantic_distance=result.semantic_distance,
            lexical_match=result.lexical_match,
            lexical_score=result.lexical_score,
            relevance_decision=result.relevance_decision,
            source_path=result.source_path,
            filename=result.filename,
            page=result.page,
            chunk_index=result.chunk_index,
            text=result.text,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "rank": self.rank,
            "fused_score": self.fused_score,
            "semantic_score": self.semantic_score,
            "semantic_distance": self.semantic_distance,
            "lexical_match": self.lexical_match,
            "lexical_score": self.lexical_score,
            "relevance_decision": self.relevance_decision.value,
            "source_path": self.source_path,
            "filename": self.filename,
            "page": self.page,
            "chunk_index": self.chunk_index,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class RagContext:
    query: str
    scopes: tuple[str, ...]
    retrieval_status: RelevanceDecision
    evidence: tuple[RagEvidence, ...]

    @property
    def evidence_count(self) -> int:
        return len(self.evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "query": self.query,
            "scopes": list(self.scopes),
            "retrieval_status": self.retrieval_status.value,
            "evidence_count": self.evidence_count,
            "evidence": [item.to_dict() for item in self.evidence],
        }


def build_rag_context(
    query: str, scopes: list[str], top_k: int = 5
) -> RagContext:
    """Reuse scoped search and its gate; status is retrieval-only, not answerability."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must not be empty")
    if type(top_k) is not int or not 1 <= top_k <= 50:
        raise ValueError("top-k must be between 1 and 50")
    if not isinstance(scopes, list) or not scopes:
        raise ValueError("scopes must be a non-empty list")
    selected = [get_scope(name).name for name in scopes]
    if len(set(selected)) != len(selected):
        raise ValueError("scopes must be unique")

    # Import lazily so pure JSON/argument tests do not require the server's
    # sqlite-vec and document-parser packages.
    from search import search_scope, search_scopes

    if len(selected) == 1:
        results = search_scope(query, selected[0], top_k, relevance_gate=True)
    else:
        results = search_scopes(query, selected, top_k, relevance_gate=True)

    evidence = tuple(RagEvidence.from_search_result(result) for result in results)
    if not evidence:
        status = RelevanceDecision.REJECT
    elif any(item.relevance_decision is RelevanceDecision.ACCEPT for item in evidence):
        status = RelevanceDecision.ACCEPT
    else:
        status = RelevanceDecision.UNCERTAIN
    return RagContext(query, tuple(selected), status, evidence)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Output gated RAG evidence as JSON")
    parser.add_argument("query", help="question to retrieve evidence for")
    parser.add_argument(
        "--scope", action="append", choices=KNOWLEDGE_SCOPES, required=True,
        help="scope to search; repeat for an explicit multi-scope search",
    )
    parser.add_argument("--top-k", type=int, default=5, help="results to keep (1-50)")
    parser.add_argument("--output", type=Path, help="also write the JSON to this file")
    args = parser.parse_args(argv)

    try:
        context = build_rag_context(args.query, args.scope, args.top_k)
        payload = json.dumps(context.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)
        if args.output is not None:
            args.output.write_text(payload + "\n", encoding="utf-8")
    except (ImportError, OSError, sqlite3.Error, EmbeddingError, ValueError, TypeError) as exc:
        print(f"rag context failed: {exc}", file=sys.stderr)
        return 1

    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
