"""Temporary, agent/session/attachment-bound PDF indexes; never a knowledge scope."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import multiprocessing
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import sqlite_vec

from chunker import chunk_blocks
from config import EMBEDDING_BATCH_SIZE, PRIVATE_SCOPE_BY_AGENT, reject_symlink_components
from database import initialize_schema, replace_document
from embeddings import embed_texts
from parsers import ParseError, parse_document
from search import hybrid_document_hits

LOGGER = logging.getLogger(__name__)
DEFAULT_ROOT = Path("/var/lib/knowledge-base/session-documents")
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
HEX_32 = re.compile(r"[0-9a-f]{32}\Z")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


@dataclass(frozen=True, slots=True)
class SessionDocumentLimits:
    ttl_hours: int = 72
    max_attachment_bytes: int = 64 * 1024 * 1024
    max_session_attachments: int = 4
    max_session_source_bytes: int = 256 * 1024 * 1024
    max_total_documents: int = 32
    max_extracted_chars: int = 4_000_000
    max_chunks: int = 4_000
    max_index_bytes: int = 128 * 1024 * 1024
    index_timeout_seconds: int = 600
    max_top_k: int = 10
    max_concurrent_indexes: int = 2

    def __post_init__(self) -> None:
        if any(value <= 0 for value in (
            self.ttl_hours, self.max_attachment_bytes, self.max_session_attachments,
            self.max_session_source_bytes, self.max_total_documents,
            self.max_extracted_chars, self.max_chunks,
            self.max_index_bytes, self.index_timeout_seconds, self.max_top_k,
            self.max_concurrent_indexes,
        )):
            raise ValueError("session document limits must be positive")
        if self.max_attachment_bytes > self.max_session_source_bytes:
            raise ValueError("attachment limit exceeds session limit")


DEFAULT_LIMITS = SessionDocumentLimits()


class SessionDocumentError(ValueError):
    """A rejected request or an unusable temporary document."""


@dataclass(frozen=True, slots=True)
class SessionDocumentKey:
    agent_id: str
    session_hash: str
    attachment_id: str

    def __post_init__(self) -> None:
        if self.agent_id not in PRIVATE_SCOPE_BY_AGENT:
            raise SessionDocumentError("unsupported agent")
        if not HEX_64.fullmatch(self.session_hash) or not HEX_32.fullmatch(self.attachment_id):
            raise SessionDocumentError("invalid session document identity")


def session_hash(session_key: str) -> str:
    if not session_key or len(session_key) > 4096:
        raise SessionDocumentError("invalid trusted session key")
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()


def source_name(key: SessionDocumentKey, filename: str) -> str:
    return f"/session-documents/{key.attachment_id}/{filename}"


class SessionDocumentStore:
    """One service-owned root; authorization always precedes filesystem access."""

    def __init__(
        self, root: Path = DEFAULT_ROOT, *, limits: SessionDocumentLimits = DEFAULT_LIMITS,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not root.is_absolute() or ".." in root.parts:
            raise SessionDocumentError("session store root must be absolute")
        self.root = root
        self.limits = limits
        self.now = now
        self._guard = threading.RLock()
        self._jobs: dict[SessionDocumentKey, multiprocessing.Process] = {}
        reject_symlink_components(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        reject_symlink_components(root)
        if os.name == "posix":
            os.chmod(root, 0o700)

    def _directory(self, key: SessionDocumentKey) -> Path:
        path = self.root / key.agent_id / key.session_hash / key.attachment_id
        reject_symlink_components(path)
        return path

    def _ensure_directory(self, key: SessionDocumentKey) -> Path:
        """Create every identity component privately, regardless of process umask."""
        directory = self._directory(key)
        current = self.root
        for component in (key.agent_id, key.session_hash, key.attachment_id):
            current = current / component
            reject_symlink_components(current)
            current.mkdir(exist_ok=True, mode=0o700)
            reject_symlink_components(current)
            if os.name == "posix":
                os.chmod(current, 0o700)
        return directory

    def _metadata(self, key: SessionDocumentKey) -> dict[str, Any] | None:
        path = self._directory(key) / "state.json"
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise SessionDocumentError("invalid state file")
            data = json.loads(path.read_text(encoding="utf-8"),
                              parse_constant=_reject_json_constant)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as exc:
            raise SessionDocumentError("corrupt session document state") from exc
        if (not isinstance(data, dict) or set(data) != {
            "status", "filename", "size_bytes", "created_at", "expires_at", "error"
        } or data["status"] not in {"INDEXING", "READY", "FAILED", "NO_SEARCHABLE_TEXT"}
            or not isinstance(data["filename"], str)
            or not _valid_filename(data["filename"])
            or type(data["size_bytes"]) is not int or data["size_bytes"] < 0
            or type(data["created_at"]) not in (int, float)
            or type(data["expires_at"]) not in (int, float)
            or not math.isfinite(data["created_at"])
            or not math.isfinite(data["expires_at"])
            or data["expires_at"] <= data["created_at"]
            or data["error"] is not None and not isinstance(data["error"], str)):
            raise SessionDocumentError("corrupt session document state")
        return data

    def _write_metadata(self, key: SessionDocumentKey, data: dict[str, Any]) -> None:
        directory = self._ensure_directory(key)
        temp = directory / f"state-{os.getpid()}-{threading.get_ident()}.tmp"
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, directory / "state.json")
        finally:
            temp.unlink(missing_ok=True)

    def _active_metadata(self, key: SessionDocumentKey) -> dict[str, Any] | None:
        metadata = self._metadata(key)
        if metadata is None:
            return None
        if self.now() >= metadata["expires_at"]:
            return None
        return metadata

    def list_documents(self, agent_id: str, session_digest: str) -> list[dict[str, Any]]:
        probe = SessionDocumentKey(agent_id, session_digest, "0" * 32)
        directory = self._directory(probe).parent
        if not directory.exists():
            return []
        results = []
        for child in directory.iterdir():
            if not HEX_32.fullmatch(child.name):
                continue
            key = SessionDocumentKey(agent_id, session_digest, child.name)
            try:
                metadata = self._active_metadata(key)
            except SessionDocumentError:
                continue  # Corrupt documents are never visible.
            if metadata:
                results.append({
                    "attachment_id": key.attachment_id,
                    "filename": metadata["filename"],
                    "status": metadata["status"],
                })
        return sorted(results, key=lambda row: row["attachment_id"])

    def _active_document_count(self) -> int:
        count = 0
        for agent_dir in self.root.iterdir():
            if agent_dir.name not in PRIVATE_SCOPE_BY_AGENT or not stat.S_ISDIR(agent_dir.lstat().st_mode):
                continue
            for session_dir in agent_dir.iterdir():
                if HEX_64.fullmatch(session_dir.name) and stat.S_ISDIR(session_dir.lstat().st_mode):
                    count += len(self.list_documents(agent_dir.name, session_dir.name))
        return count

    def submit(self, key: SessionDocumentKey, filename: str, data: bytes, *, launch: bool = True) -> str:
        if not _valid_filename(filename) or not filename.lower().endswith(".pdf"):
            raise SessionDocumentError("only trusted PDF filenames are supported")
        if not data or len(data) > self.limits.max_attachment_bytes:
            raise SessionDocumentError("PDF exceeds the attachment size limit")
        with self._guard:
            existing = self._active_metadata(key)
            if existing is not None:
                return existing["status"]
            if self._metadata(key) is not None:
                self._remove_document(key)  # Expired exact target only.
            if len(self._jobs) >= self.limits.max_concurrent_indexes and launch:
                raise SessionDocumentError("session indexer is busy")
            if self._active_document_count() >= self.limits.max_total_documents:
                raise SessionDocumentError("global session document limit reached")
            documents = self.list_documents(key.agent_id, key.session_hash)
            if len(documents) >= self.limits.max_session_attachments:
                raise SessionDocumentError("session attachment limit reached")
            size = sum(
                self._metadata(SessionDocumentKey(key.agent_id, key.session_hash, item["attachment_id"]))["size_bytes"]
                for item in documents
            )
            if size + len(data) > self.limits.max_session_source_bytes:
                raise SessionDocumentError("session source byte limit reached")
            directory = self._ensure_directory(key)
            source = directory / "source.pdf"
            descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                source.unlink(missing_ok=True)
                raise
            created = self.now()
            try:
                self._write_metadata(key, {
                    "status": "INDEXING", "filename": filename, "size_bytes": len(data),
                    "created_at": created, "expires_at": created + self.limits.ttl_hours * 3600,
                    "error": None,
                })
                if launch:
                    self._launch(key)
            except BaseException:
                self._discard_work_files(key)
                raise
            return "INDEXING"

    def _launch(self, key: SessionDocumentKey) -> None:
        process = multiprocessing.get_context("spawn").Process(
            target=_index_worker, args=(self.root, self.limits, key), daemon=True
        )
        process.start()
        self._jobs[key] = process
        threading.Thread(target=self._watch, args=(key, process), daemon=True).start()

    def _watch(self, key: SessionDocumentKey, process: multiprocessing.Process) -> None:
        process.join(self.limits.index_timeout_seconds)
        if process.is_alive():
            process.terminate()
            process.join(5)
        with self._guard:
            self._jobs.pop(key, None)
            try:
                metadata = self._metadata(key)
                if metadata and metadata["status"] == "INDEXING":
                    metadata["status"] = "FAILED"
                    metadata["error"] = "indexing failed or timed out"
                    self._write_metadata(key, metadata)
                    self._discard_work_files(key)
            except (OSError, SessionDocumentError):
                LOGGER.exception("cannot settle session index job")

    def index_once(self, key: SessionDocumentKey) -> None:
        """Worker body, also callable directly in deterministic local tests."""
        metadata = self._active_metadata(key)
        if metadata is None or metadata["status"] != "INDEXING":
            raise SessionDocumentError("document is not awaiting indexing")
        directory = self._directory(key)
        source = directory / "source.pdf"
        temporary_db = directory / "index.tmp.db"
        try:
            if not stat.S_ISREG(source.lstat().st_mode):
                raise SessionDocumentError("source snapshot is missing")
            data = source.read_bytes()
            if len(data) != metadata["size_bytes"] or len(data) > self.limits.max_attachment_bytes:
                raise SessionDocumentError("source snapshot changed")
            blocks = parse_document(Path(metadata["filename"]), data)
            if sum(len(block.text) for block in blocks) > self.limits.max_extracted_chars:
                raise SessionDocumentError("extracted text limit reached")
            source_path = source_name(key, metadata["filename"])
            chunks = chunk_blocks(blocks, key.attachment_id, PurePosixPath(source_path))
            if not chunks or len(chunks) > self.limits.max_chunks:
                raise SessionDocumentError("chunk limit reached")
            vectors = []
            for start in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
                vectors.extend(embed_texts([item.text for item in chunks[start:start + EMBEDDING_BATCH_SIZE]]))
            connection = _connect_index(temporary_db, create=True)
            try:
                page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
                connection.execute(f"PRAGMA max_page_count = {self.limits.max_index_bytes // page_size}")
                initialize_schema(connection)
                replace_document(
                    connection, document_id=key.attachment_id, source_path=source_path,
                    filename=metadata["filename"], file_type=".pdf", file_size=len(data),
                    mtime=0, sha256=hashlib.sha256(data).hexdigest(), chunks=chunks, vectors=vectors,
                )
            finally:
                connection.close()
            if temporary_db.stat().st_size > self.limits.max_index_bytes:
                raise SessionDocumentError("index byte limit reached")
            os.replace(temporary_db, directory / "index.db")
            metadata["status"] = "READY"
            self._write_metadata(key, metadata)
        except Exception as exc:
            LOGGER.warning("session document index failed: %s", type(exc).__name__)
            metadata["status"] = "NO_SEARCHABLE_TEXT" if isinstance(exc, ParseError) else "FAILED"
            metadata["error"] = "no searchable text" if isinstance(exc, ParseError) else "indexing failed"
            self._write_metadata(key, metadata)
        finally:
            self._discard_work_files(key)

    def _discard_work_files(self, key: SessionDocumentKey) -> None:
        for filename in ("source.pdf", "index.tmp.db", "index.tmp.db-journal"):
            path = self._directory(key) / filename
            try:
                if stat.S_ISREG(path.lstat().st_mode):
                    path.unlink()
            except FileNotFoundError:
                pass

    def query(self, key: SessionDocumentKey, question: str, top_k: int) -> dict[str, Any]:
        if not question.strip() or len(question) > 4096:
            raise SessionDocumentError("query must contain 1 to 4096 characters")
        if type(top_k) is not int or not 1 <= top_k <= self.limits.max_top_k:
            raise SessionDocumentError("top_k is out of range")
        try:
            metadata = self._active_metadata(key)
        except SessionDocumentError:
            return {"status": "FAILED", "evidence": []}
        if metadata is None:
            return {"status": "NOT_FOUND", "evidence": []}
        if metadata["status"] != "READY":
            return {"status": metadata["status"], "evidence": []}
        index = self._directory(key) / "index.db"
        if not index.is_file() or not stat.S_ISREG(index.lstat().st_mode):
            return {"status": "FAILED", "evidence": []}
        try:
            connection = _connect_index(index, create=False)
            try:
                hits = hybrid_document_hits(
                    connection, question, top_k, source_name(key, metadata["filename"])
                )
            finally:
                connection.close()
        except (sqlite3.Error, OSError):
            LOGGER.warning("corrupt or unreadable session index")
            return {"status": "FAILED", "evidence": []}
        return {
            "status": "READY", "attachment_id": key.attachment_id,
            "filename": metadata["filename"],
            "evidence": [{
                "rank": rank, "page": hit.row["page"],
                "chunk_index": int(hit.row["chunk_index"]),
                "text": str(hit.row["text"]),
                "fused_score": hit.fused_score,
                "semantic_score": hit.semantic_score,
                "lexical_match": hit.lexical_match,
            } for rank, hit in enumerate(hits, start=1)],
        }

    def cleanup(self) -> int:
        """Idempotently remove expired/corrupt jobs and abandoned crash snapshots."""
        removed = 0
        with self._guard:
            for agent_dir in self.root.iterdir():
                if agent_dir.name not in PRIVATE_SCOPE_BY_AGENT or not stat.S_ISDIR(agent_dir.lstat().st_mode):
                    continue
                for session_dir in agent_dir.iterdir():
                    if not HEX_64.fullmatch(session_dir.name) or not stat.S_ISDIR(session_dir.lstat().st_mode):
                        continue
                    for document_dir in session_dir.iterdir():
                        if (not HEX_32.fullmatch(document_dir.name)
                            or not stat.S_ISDIR(document_dir.lstat().st_mode)):
                            continue
                        key = SessionDocumentKey(agent_dir.name, session_dir.name, document_dir.name)
                        if key in self._jobs:
                            continue
                        try:
                            metadata = self._metadata(key)
                        except SessionDocumentError:
                            metadata = None
                        if metadata is None or self.now() >= metadata["expires_at"]:
                            self._remove_document(key)
                            removed += 1
                        elif metadata["status"] == "INDEXING":
                            metadata["status"] = "FAILED"
                            metadata["error"] = "interrupted index"
                            self._write_metadata(key, metadata)
                            self._discard_work_files(key)
                    if not any(session_dir.iterdir()):
                        session_dir.rmdir()
                if not any(agent_dir.iterdir()):
                    agent_dir.rmdir()
        return removed

    def _remove_document(self, key: SessionDocumentKey) -> None:
        directory = self._directory(key)
        # Resolve and check the exact child before any recursive removal (also on Windows).
        root_resolved = self.root.resolve(strict=True)
        target_resolved = directory.resolve(strict=False)
        if not target_resolved.is_relative_to(root_resolved) or target_resolved == root_resolved:
            raise SessionDocumentError("unsafe cleanup target")
        reject_symlink_components(directory)
        shutil.rmtree(directory)


def _valid_filename(filename: str) -> bool:
    return bool(filename and filename not in {".", ".."} and len(filename) <= 255
                and not any(char in filename for char in "/\\\0\r\n"))


def _connect_index(path: Path, *, create: bool) -> sqlite3.Connection:
    reject_symlink_components(path)
    target = str(path) if create else path.as_uri() + "?mode=ro"
    connection = sqlite3.connect(target, uri=not create, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
        return connection
    except Exception:
        connection.close()
        raise


def _index_worker(root: Path, limits: SessionDocumentLimits, key: SessionDocumentKey) -> None:
    if os.name == "posix":
        import resource

        # The parent also enforces a wall-clock deadline by terminating this process.
        memory_cap = 1536 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_cap, memory_cap))
        resource.setrlimit(resource.RLIMIT_CPU, (limits.index_timeout_seconds, limits.index_timeout_seconds + 5))
    SessionDocumentStore(root, limits=limits).index_once(key)
