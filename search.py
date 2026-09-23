"""Hybrid FTS5 + sqlite-vec retrieval with reciprocal rank fusion."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import struct
import sys
from pathlib import Path

from config import EMBEDDING_DIMENSION, SOURCE_ROOT, configure_logging
from database import connect_database
from embeddings import EmbeddingError, embed_texts

LOGGER = logging.getLogger("knowledge.search")
RRF_K = 60


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
    connection: sqlite3.Connection, question: str, top_k: int
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
        if Path(os.path.normpath(row["source_path"])).is_relative_to(SOURCE_ROOT)
    }
    ranked_ids = sorted(
        allowed,
        key=lambda chunk_id: (-scores[chunk_id], chunk_id),
    )[:top_k]
    return [(scores[chunk_id], allowed[chunk_id]) for chunk_id in ranked_ids]


def main() -> int:
    parser = argparse.ArgumentParser(description="Search Chen's private local knowledge base")
    parser.add_argument("question", help="question or search terms")
    parser.add_argument("--top-k", type=int, default=5, help="results to show (1-50)")
    args = parser.parse_args()
    try:
        configure_logging("search.log")
    except OSError as exc:
        print(f"cannot open private log directory: {exc}", file=sys.stderr)
        return 1
    try:
        connection = connect_database(create=False)
        try:
            results = hybrid_search(connection, args.question, args.top_k)
        finally:
            connection.close()
    except (OSError, sqlite3.Error, EmbeddingError, ValueError) as exc:
        LOGGER.error("search failed: %s", exc)
        return 1

    if not results:
        print("No results.")
        return 0
    for rank, (score, row) in enumerate(results, start=1):
        snippet = re.sub(r"\s+", " ", row["text"]).strip()[:280]
        page = row["page"] if row["page"] is not None else "-"
        print(
            f"rank: {rank}\n"
            f"fused score: {score:.6f}\n"
            f"source path: {row['source_path']}\n"
            f"filename: {row['filename']}\n"
            f"page: {page}\n"
            f"chunk index: {row['chunk_index']}\n"
            f"snippet: {snippet}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
