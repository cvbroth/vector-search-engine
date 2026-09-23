"""Incrementally index only /srv/storage/knowledge/private/chen."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from chunker import chunk_blocks
from config import SOURCE_ROOT, SUPPORTED_SUFFIXES, configure_logging
from database import (
    connect_database,
    delete_document,
    get_document,
    initialize_schema,
    list_document_paths,
    replace_document,
)
from embeddings import EmbeddingError, embed_texts
from parsers import ParseError, parse_document

LOGGER = logging.getLogger("knowledge.ingest")


@dataclass(frozen=True, slots=True)
class Snapshot:
    data: bytes
    size: int
    mtime_ns: int
    sha256: str


def discover_source_files() -> list[Path]:
    """Complete scan or error; never follow symlink directories or files."""
    if not SOURCE_ROOT.is_dir() or SOURCE_ROOT.resolve(strict=True) != SOURCE_ROOT:
        raise OSError(f"source root is missing or redirects elsewhere: {SOURCE_ROOT}")
    found: list[Path] = []

    def fail(error: OSError) -> None:
        raise error

    for directory, subdirs, filenames in os.walk(
        SOURCE_ROOT, topdown=True, followlinks=False, onerror=fail
    ):
        parent = Path(directory)
        subdirs[:] = [name for name in subdirs if not (parent / name).is_symlink()]
        for name in filenames:
            candidate = parent / name
            if candidate.suffix.lower() in SUPPORTED_SUFFIXES and not candidate.is_symlink():
                found.append(candidate)
    return sorted(found)


def read_snapshot(path: Path) -> Snapshot:
    """Open each path component relative to the fixed root without following links."""
    relative = path.relative_to(SOURCE_ROOT)
    if not relative.parts or any(part in (".", "..") for part in relative.parts):
        raise OSError(f"invalid source path: {path}")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(SOURCE_ROOT, directory_flags)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        with os.fdopen(file_fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OSError(f"not a regular file: {path}")
            if before.st_nlink != 1:
                raise OSError(f"hard-linked source file is unsupported: {path}")
            data = source.read()
            after = os.fstat(source.fileno())
    finally:
        os.close(fd)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError(f"file changed during read: {path}")
    if len(data) != after.st_size:
        raise OSError(f"incomplete read: {path}")
    return Snapshot(data, after.st_size, after.st_mtime_ns, hashlib.sha256(data).hexdigest())


def ingest_one(connection: sqlite3.Connection, path: Path) -> str:
    snapshot = read_snapshot(path)
    source_path = str(path)
    existing = get_document(connection, source_path)
    if existing and (
        existing["file_size"] == snapshot.size
        and existing["mtime"] == snapshot.mtime_ns
        and existing["sha256"] == snapshot.sha256
    ):
        return "skipped"

    document_id = hashlib.sha256(source_path.encode("utf-8")).hexdigest()
    blocks = parse_document(path, snapshot.data)
    chunks = chunk_blocks(blocks, document_id, path)
    if not chunks:
        raise ParseError(f"no indexable chunks in {path}")
    vectors = embed_texts([chunk.text for chunk in chunks])
    replace_document(
        connection,
        document_id=document_id,
        source_path=source_path,
        filename=path.name,
        file_type=path.suffix.lower(),
        file_size=snapshot.size,
        mtime=snapshot.mtime_ns,
        sha256=snapshot.sha256,
        chunks=chunks,
        vectors=vectors,
    )
    return "updated" if existing else "added"


def main() -> int:
    try:
        configure_logging("ingest.log")
    except OSError as exc:
        print(f"cannot open private log directory: {exc}", file=sys.stderr)
        return 1
    try:
        files = discover_source_files()
        connection = connect_database(create=True)
        try:
            initialize_schema(connection)
            seen = {str(path) for path in files}
            counts = {"added": 0, "updated": 0, "skipped": 0, "deleted": 0, "failed": 0}
            for path in files:
                try:
                    status = ingest_one(connection, path)
                    counts[status] += 1
                    LOGGER.info("%s: %s", status, path)
                except (OSError, ParseError, EmbeddingError, sqlite3.Error, ValueError) as exc:
                    counts["failed"] += 1
                    LOGGER.error("failed: %s: %s", path, exc)
            for source_path in sorted(list_document_paths(connection) - seen):
                try:
                    if delete_document(connection, source_path):
                        counts["deleted"] += 1
                        LOGGER.info("deleted: %s", source_path)
                except (sqlite3.Error, ValueError) as exc:
                    counts["failed"] += 1
                    LOGGER.error("delete failed: %s: %s", source_path, exc)
            LOGGER.info("summary: %s", counts)
            return 1 if counts["failed"] else 0
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        LOGGER.error("ingest aborted: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
