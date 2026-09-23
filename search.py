"""Hybrid FTS5 + sqlite-vec retrieval with reciprocal rank fusion."""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from config import (
    DEFAULT_SCOPE,
    EMBEDDING_DIMENSION,
    KNOWLEDGE_SCOPES,
    KnowledgeScope,
    configure_logging,
    get_scope,
)
from database import connect_database
from embeddings import EmbeddingError, embed_texts

LOGGER = logging.getLogger("knowledge.search")
RRF_K = 60


@dataclass(frozen=True, slots=True)
class SearchResult:
    scope: str
    rank: int
    fused_score: float
    source_path: str
    filename: str
    page: int | None
    chunk_index: int
    snippet: str


def _snippet(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:280]


def _path_is_in_scope(source_path: str, scope: KnowledgeScope) -> bool:
    path = Path(source_path)
    return (
        path.is_absolute()
        and ".." not in path.parts
        and path != scope.source_dir
        and path.is_relative_to(scope.source_dir)
    )


def _fts_query(question: str) -> str | None:
    """Make safe trigram-friendly Chinese and Latin terms for FTS5 MATCH."""
    terms: list[str] = []
    for word in re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9_]+", question):
        if "\u3400" <= word[0] <= "\u9fff":
            terms.extend(word[i : i + 3] for i in range(len(word) - 2))
        elif len(word) >= 3:
            terms.append(word)
    unique = list(dict.fromkeys(terms))[:32]
    return " OR ".join(f'"{term}"' for term in unique) if unique else None


def _lexical_ids(connection: sqlite3.Connection, question: str, limit: int) -> list[int]:
    query = _fts_query(question)
    if query is None:
        return []
    return [
        int(row[0])
        for row in connection.execute(
            """
            SELECT rowid FROM chunks_fts
            WHERE chunks_fts MATCH ?
            ORDER BY bm25(chunks_fts)
            LIMIT ?
            """,
            (query, limit),
        )
    ]


def _semantic_ids(connection: sqlite3.Connection, question: str, limit: int) -> list[int]:
    vector = embed_texts([question])[0]
    packed = struct.pack(f"<{EMBEDDING_DIMENSION}f", *vector)
    return [
        int(row[0])
        for row in connection.execute(
            """
            SELECT rowid FROM chunks_vec
            WHERE embedding MATCH ?
            ORDER BY distance
            LIMIT ?
            """,
            (packed, limit),
        )
    ]


def hybrid_search(
    connection: sqlite3.Connection, question: str, top_k: int, scope: KnowledgeScope
) -> list[tuple[float, sqlite3.Row]]:
    if not question.strip():
        raise ValueError("question must not be empty")
    if not 1 <= top_k <= 50:
        raise ValueError("top-k must be between 1 and 50")
    candidate_count = min(200, max(30, top_k * 5))
    lexical = _lexical_ids(connection, question, candidate_count)
    semantic = _semantic_ids(connection, question, candidate_count)

    scores: dict[int, float] = {}
    for ranking in (lexical, semantic):
        for position, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + position)
    if not scores:
        return []

    placeholders = ",".join("?" for _ in scores)
    rows = connection.execute(
        f"""
        SELECT id, source_path, filename, page, chunk_index, text
        FROM chunks WHERE id IN ({placeholders})
        """,
        tuple(scores),
    ).fetchall()
    allowed = {
        int(row["id"]): row
        for row in rows
        if _path_is_in_scope(str(row["source_path"]), scope)
    }
    ranked_ids = sorted(
        allowed,
        key=lambda chunk_id: (-scores[chunk_id], chunk_id),
    )[:top_k]
    return [(scores[chunk_id], allowed[chunk_id]) for chunk_id in ranked_ids]


def search_scopes(query: str, scopes: list[str], top_k: int) -> list[SearchResult]:
    """Fuse per-database hybrid rankings without comparing their raw scores."""
    if not query.strip():
        raise ValueError("question must not be empty")
    if not 1 <= top_k <= 50:
        raise ValueError("top-k must be between 1 and 50")
    selected = [get_scope(name) for name in scopes]
    if not selected or len({scope.name for scope in selected}) != len(selected):
        raise ValueError("scopes must be non-empty and unique")

    candidates: list[tuple[int, int, str, sqlite3.Row]] = []
    for scope_order, scope in enumerate(selected):
        connection = connect_database(scope, create=False)
        try:
            ranked = hybrid_search(connection, query, top_k, scope)
        finally:
            connection.close()
        for local_rank, (_, row) in enumerate(ranked, start=1):
            candidates.append((local_rank, scope_order, scope.name, row))

    # Every database contributes one ranked list. Equal ranks use the caller's
    # scope order, then the chunk ID, so results are deterministic.
    candidates.sort(key=lambda item: (item[0], item[1], int(item[3]["id"])))
    return [
        SearchResult(
            scope=name,
            rank=global_rank,
            fused_score=1.0 / (RRF_K + local_rank),
            source_path=str(row["source_path"]),
            filename=str(row["filename"]),
            page=row["page"],
            chunk_index=int(row["chunk_index"]),
            snippet=_snippet(str(row["text"])),
        )
        for global_rank, (local_rank, _, name, row) in enumerate(
            candidates[:top_k], start=1
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Search one local knowledge scope")
    parser.add_argument("question", help="question or search terms")
    parser.add_argument("--top-k", type=int, default=5, help="results to show (1-50)")
    parser.add_argument(
        "--scope", choices=KNOWLEDGE_SCOPES, default=DEFAULT_SCOPE,
        help="knowledge scope (default: chen)",
    )
    args = parser.parse_args()
    scope = get_scope(args.scope)
    try:
        configure_logging(scope, "search.log")
    except (OSError, ValueError) as exc:
        print(f"cannot open log directory: {exc}", file=sys.stderr)
        return 1
    try:
        connection = connect_database(scope, create=False)
        try:
            results = hybrid_search(connection, args.question, args.top_k, scope)
        finally:
            connection.close()
    except (OSError, sqlite3.Error, EmbeddingError, ValueError) as exc:
        LOGGER.error("search failed: %s", exc)
        return 1

    if not results:
        print("No results.")
        return 0
    for rank, (score, row) in enumerate(results, start=1):
        page = row["page"] if row["page"] is not None else "-"
        print(
            f"scope: {scope.name}\n"
            f"rank: {rank}\n"
            f"fused score: {score:.6f}\n"
            f"source path: {row['source_path']}\n"
            f"filename: {row['filename']}\n"
            f"page: {page}\n"
            f"chunk index: {row['chunk_index']}\n"
            f"snippet: {_snippet(str(row['text']))}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
