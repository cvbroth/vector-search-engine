"""SQLite schema and transactional document replacement/deletion."""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlite_vec

from chunker import Chunk
from config import DATABASE_PATH, EMBEDDING_DIMENSION


def connect_database(*, create: bool) -> sqlite3.Connection:
    if create:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        target = str(DATABASE_PATH)
    else:
        if not DATABASE_PATH.is_file():
            raise FileNotFoundError(f"index does not exist: {DATABASE_PATH}")
        target = DATABASE_PATH.as_uri() + "?mode=ro"
    connection = sqlite3.connect(target, uri=not create, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
        return connection
    except Exception:
        connection.close()
        raise


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                mtime INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                indexed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                page INTEGER,
                text TEXT NOT NULL,
                source_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                UNIQUE(document_id, chunk_index)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS chunks_document_id ON chunks(document_id)"
        )
        # Plain FTS5 storage permits direct INSERT/DELETE with the same rowid as chunks.id.
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING "
            "fts5(text, tokenize='trigram')"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING "
            "vec0(embedding float[768] distance_metric=cosine)"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def get_document(connection: sqlite3.Connection, source_path: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, file_size, mtime, sha256 FROM documents WHERE source_path = ?",
        (source_path,),
    ).fetchone()


def list_document_paths(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT source_path FROM documents")
    }


def _delete_chunks(connection: sqlite3.Connection, document_id: str) -> None:
    ids = [
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM chunks WHERE document_id = ?", (document_id,)
        )
    ]
    for chunk_id in ids:
        connection.execute("DELETE FROM chunks_vec WHERE rowid = ?", (chunk_id,))
        connection.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
    connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))


def replace_document(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    source_path: str,
    filename: str,
    file_type: str,
    file_size: int,
    mtime: int,
    sha256: str,
    chunks: Sequence[Chunk],
    vectors: Sequence[Sequence[float]],
) -> None:
    """Replace all three index views atomically; rollback keeps the old version."""
    if not chunks or len(chunks) != len(vectors):
        raise ValueError("each chunk must have exactly one embedding")
    if any(len(vector) != EMBEDDING_DIMENSION for vector in vectors):
        raise ValueError("embedding dimension mismatch")
    if any(chunk.document_id != document_id for chunk in chunks):
        raise ValueError("chunk belongs to a different document")

    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT id FROM documents WHERE source_path = ?", (source_path,)
        ).fetchone()
        if existing:
            if existing["id"] != document_id:
                raise ValueError("document ID conflict")
            _delete_chunks(connection, document_id)
            connection.execute(
                """
                UPDATE documents
                SET filename=?, file_type=?, file_size=?, mtime=?, sha256=?, indexed_at=?
                WHERE id=?
                """,
                (
                    filename, file_type, file_size, mtime, sha256,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"), document_id,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO documents
                    (id, source_path, filename, file_type, file_size, mtime, sha256, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id, source_path, filename, file_type, file_size,
                    mtime, sha256, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
        for chunk, vector in zip(chunks, vectors, strict=True):
            cursor = connection.execute(
                """
                INSERT INTO chunks
                    (document_id, chunk_index, page, text, source_path, filename)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id, chunk.chunk_index, chunk.page, chunk.text,
                    chunk.source_path, chunk.filename,
                ),
            )
            chunk_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
                (chunk_id, chunk.text),
            )
            connection.execute(
                "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                (chunk_id, struct.pack(f"<{EMBEDDING_DIMENSION}f", *vector)),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def delete_document(connection: sqlite3.Connection, source_path: str) -> bool:
    """Remove a vanished document and all associated rows in one transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT id FROM documents WHERE source_path = ?", (source_path,)
        ).fetchone()
        if row is None:
            connection.commit()
            return False
        _delete_chunks(connection, str(row["id"]))
        connection.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
