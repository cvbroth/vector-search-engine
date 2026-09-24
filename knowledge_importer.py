"""Policy-routed Inbox import. Source files remain the only knowledge truth."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import stat
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from config import KNOWLEDGE_SCOPES, SUPPORTED_SUFFIXES, reject_symlink_components
from ingest import ingest_scope
from parsers import ParseError, parse_document
from scope_lock import scope_file_lock

POLICY_PATH = Path("/etc/knowledge-import/policy.json")
INBOX_ROOT = Path("/srv/storage/ai-inbox")
STATE_DB_PATH = Path("/var/lib/knowledge-import/imports.db")
LOCK_PATH = Path("/run/knowledge-import/import.lock")
POLICY_MAX_BYTES = 64 * 1024
READ_BLOCK_BYTES = 1024 * 1024
DEFAULT_SETTLE_SECONDS = 2.0
SCHEMA_VERSION = "1.0"
KINDS = ("private", "shared")
USER_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
LOGGER = logging.getLogger("knowledge.import")


class ImportPolicyError(ValueError):
    """Policy is missing or invalid; no import may start."""


class ImporterBusy(RuntimeError):
    """Another importer holds the exclusive process lock."""


class UnsafeInput(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class UnstableInput(RuntimeError):
    """An uploader changed the file between inspection and use."""


@dataclass(frozen=True, slots=True)
class ImportRoute:
    enabled: bool
    scope: str | None = None
    destination: Path | None = None


@dataclass(frozen=True, slots=True)
class ImportPolicy:
    max_file_size_bytes: int
    routes: Mapping[str, Mapping[str, ImportRoute]]


@dataclass(slots=True)
class ImportRecord:
    import_id: str
    uploader: str
    access_kind: str
    scope: str | None
    original_filename: str
    final_filename: str | None
    source_inbox_path: str
    destination_path: str | None
    sha256: str | None
    size_bytes: int
    status: str
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    indexed_at: str | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    path: Path
    uploader: str
    kind: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def load_policy(
    path: Path = POLICY_PATH,
    *,
    inbox_root: Path = INBOX_ROOT,
    allowed_destinations: Mapping[str, Path] | None = None,
) -> ImportPolicy:
    """Validate exact routes; injected roots are for local tests, not CLI options."""
    if allowed_destinations is None:
        allowed_destinations = {name: scope.source_dir for name, scope in KNOWLEDGE_SCOPES.items()}
    try:
        reject_symlink_components(path)
        reject_symlink_components(inbox_root)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > POLICY_MAX_BYTES:
            raise ImportPolicyError("policy must be a regular file no larger than 64 KiB")
        with path.open("rb") as source:
            raw = source.read(POLICY_MAX_BYTES + 1)
            if len(raw) > POLICY_MAX_BYTES:
                raise ImportPolicyError("policy exceeds 64 KiB")
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ImportPolicyError(f"cannot load import policy: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "max_file_size_bytes", "routes"}:
        raise ImportPolicyError("policy requires schema_version, max_file_size_bytes, routes")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ImportPolicyError("unsupported import policy schema_version")
    maximum = value["max_file_size_bytes"]
    if type(maximum) is not int or maximum <= 0:
        raise ImportPolicyError("max_file_size_bytes must be a positive integer")
    raw_routes = value["routes"]
    if not isinstance(raw_routes, dict) or not raw_routes:
        raise ImportPolicyError("routes must be a nonempty object")
    routes: dict[str, dict[str, ImportRoute]] = {}
    for user, user_routes in raw_routes.items():
        if USER_PATTERN.fullmatch(user) is None or not isinstance(user_routes, dict) or set(user_routes) != set(KINDS):
            raise ImportPolicyError("each valid uploader needs private and shared routes")
        resolved: dict[str, ImportRoute] = {}
        for kind in KINDS:
            raw_route = user_routes[kind]
            if not isinstance(raw_route, dict) or type(raw_route.get("enabled")) is not bool:
                raise ImportPolicyError("route requires a boolean enabled field")
            if not raw_route["enabled"]:
                if set(raw_route) != {"enabled"}:
                    raise ImportPolicyError("disabled route may contain enabled only")
                resolved[kind] = ImportRoute(False)
                continue
            if set(raw_route) != {"enabled", "scope", "destination"}:
                raise ImportPolicyError("enabled route requires scope and destination")
            scope_name = raw_route["scope"]
            target_name = raw_route["destination"]
            if not isinstance(scope_name, str) or scope_name not in KNOWLEDGE_SCOPES:
                raise ImportPolicyError("route names an unknown scope")
            if KNOWLEDGE_SCOPES[scope_name].area != kind:
                raise ImportPolicyError("route access kind does not match scope")
            if not isinstance(target_name, str):
                raise ImportPolicyError("destination must be an absolute path")
            destination = Path(target_name)
            if not destination.is_absolute() or ".." in destination.parts:
                raise ImportPolicyError("destination must be absolute without traversal")
            expected = allowed_destinations.get(scope_name)
            if expected is None or destination != expected or destination.is_relative_to(inbox_root):
                raise ImportPolicyError("destination must equal the configured source root outside Inbox")
            try:
                reject_symlink_components(destination)
            except OSError as exc:
                raise ImportPolicyError("destination path contains a symlink") from exc
            resolved[kind] = ImportRoute(True, scope_name, destination)
        routes[user] = resolved
    return ImportPolicy(maximum, MappingProxyType({
        user: MappingProxyType(user_routes) for user, user_routes in routes.items()
    }))


class ImportStore:
    """Separate metadata-only audit DB; never stores extracted text or chunks."""

    def __init__(self, path: Path) -> None:
        reject_symlink_components(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        reject_symlink_components(path)
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS imports (
                import_id TEXT PRIMARY KEY,
                uploader TEXT NOT NULL,
                access_kind TEXT NOT NULL,
                scope TEXT,
                original_filename TEXT NOT NULL,
                final_filename TEXT,
                source_inbox_path TEXT NOT NULL,
                destination_path TEXT,
                sha256 TEXT,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'DISCOVERED','VALIDATING','DUPLICATE','CONFLICT','REJECTED',
                    'IMPORTED','INDEXING','INDEXED','INDEX_ERROR')),
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                indexed_at TEXT
            )
        """)
        self.connection.execute("CREATE INDEX IF NOT EXISTS imports_pending ON imports(scope, status)")

    def close(self) -> None:
        self.connection.close()

    def insert(self, record: ImportRecord) -> None:
        self.connection.execute(
            "INSERT INTO imports VALUES (:import_id,:uploader,:access_kind,:scope,:original_filename,"
            ":final_filename,:source_inbox_path,:destination_path,:sha256,:size_bytes,:status,"
            ":error_code,:error_message,:created_at,:updated_at,:indexed_at)",
            asdict(record),
        )

    def update(self, record: ImportRecord) -> None:
        record.updated_at = _now()
        self.connection.execute(
            "UPDATE imports SET scope=:scope,final_filename=:final_filename,"
            "destination_path=:destination_path,sha256=:sha256,size_bytes=:size_bytes,"
            "status=:status,error_code=:error_code,error_message=:error_message,"
            "updated_at=:updated_at,indexed_at=:indexed_at WHERE import_id=:import_id",
            asdict(record),
        )

    def pending(self, uploader: str | None = None) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        query = (
            "SELECT scope,import_id FROM imports WHERE status IN "
            "('IMPORTED','INDEXING','INDEX_ERROR')"
        )
        if uploader is not None:
            query += " AND uploader=?"
        query += " ORDER BY created_at"
        for row in self.connection.execute(
            query, () if uploader is None else (uploader,)
        ):
            if row["scope"] in KNOWLEDGE_SCOPES:
                result.setdefault(row["scope"], []).append(row["import_id"])
        return result

    def set_index_status(self, ids: list[str], status: str, error_code: str | None) -> None:
        now = _now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for import_id in ids:
                self.connection.execute(
                    "UPDATE imports SET status=?,error_code=?,error_message=?,updated_at=?,indexed_at=? "
                    "WHERE import_id=? AND status IN ('IMPORTED','INDEX_ERROR','INDEXING')",
                    (status, error_code, None if error_code is None else "incremental ingest failed",
                     now, now if status == "INDEXED" else None, import_id),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


class ImporterLock:
    """Advisory process lock. The service account must control the lock directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> ImporterLock:
        reject_symlink_components(self.path)
        if not self.path.parent.is_dir():
            raise OSError(f"lock directory missing: {self.path.parent}")
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o660)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("lock path must be a regular, single-linked file")
            if os.name == "nt":
                import msvcrt

                if info.st_size == 0:
                    os.write(self.fd, b"\0")
                os.lseek(self.fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise ImporterBusy("another importer is running") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ImporterBusy("another importer is running") from exc
            return self
        except Exception:
            os.close(self.fd)
            self.fd = None
            raise

    def __exit__(self, _kind: object, _value: object, _traceback: object) -> None:
        if self.fd is None:
            return
        if os.name == "nt":
            import msvcrt

            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.fd = None


def _open_beneath(root: Path, relative: Path) -> int:
    """On Linux, traverse every component with directory FDs and O_NOFOLLOW."""
    if not relative.parts or any(part in (".", "..") for part in relative.parts):
        raise OSError("invalid relative source path")
    reject_symlink_components(root)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if os.open not in os.supports_dir_fd:
        path = root / relative
        reject_symlink_components(path)
        return os.open(path, file_flags)
    parent_fd = os.open(root, directory_flags)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        return os.open(relative.name, file_flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _same_candidate(info: os.stat_result, candidate: Candidate) -> bool:
    return (
        info.st_dev == candidate.device and info.st_ino == candidate.inode
        and info.st_mode == candidate.mode and info.st_size == candidate.size
        and info.st_mtime_ns == candidate.mtime_ns
    )


def _read_candidate(candidate: Candidate, maximum: int, *, collect: bool = True) -> tuple[bytes, str]:
    fd = _open_beneath(candidate.path.parent, Path(candidate.path.name))
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not _same_candidate(before, candidate):
            raise UnstableInput("candidate changed before read")
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise UnsafeInput("NOT_REGULAR", "only single-linked regular files are supported")
        if before.st_size == 0:
            raise UnsafeInput("EMPTY_FILE", "file is empty")
        if before.st_size > maximum:
            raise UnsafeInput("TOO_LARGE", "file exceeds policy size limit")
        sha = hashlib.sha256()
        parts: list[bytes] = []
        total = 0
        while block := source.read(READ_BLOCK_BYTES):
            total += len(block)
            if total > maximum:
                raise UnsafeInput("TOO_LARGE", "file exceeds policy size limit")
            sha.update(block)
            if collect:
                parts.append(block)
        after = os.fstat(source.fileno())
    if not _same_candidate(after, candidate) or total != candidate.size:
        raise UnstableInput("candidate changed during read")
    return b"".join(parts), sha.hexdigest()


def _hash_existing(root: Path, path: Path) -> str:
    fd = _open_beneath(root, path.relative_to(root))
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OSError("non-regular or linked file in destination source")
        sha = hashlib.sha256()
        while block := source.read(READ_BLOCK_BYTES):
            sha.update(block)
        after = os.fstat(source.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise OSError("destination source changed while hashing")
    return sha.hexdigest()


def _duplicate_or_conflict(destination: Path, filename: str, sha256: str) -> str | None:
    """Compare actual source files, including those predating the import audit DB."""
    reject_symlink_components(destination)
    if not destination.is_dir():
        raise UnsafeInput("DESTINATION_MISSING", "configured source directory is missing")
    named_target = destination / filename
    try:
        named_target.lstat()
    except FileNotFoundError:
        pass
    else:
        if named_target.is_symlink() or _hash_existing(destination, named_target) != sha256:
            return "CONFLICT"
        return "DUPLICATE"

    def fail(error: OSError) -> None:
        raise error

    for current, subdirs, filenames in os.walk(destination, topdown=True, followlinks=False, onerror=fail):
        parent = Path(current)
        subdirs[:] = [name for name in subdirs if not (parent / name).is_symlink()]
        for name in filenames:
            existing = parent / name
            if name.startswith(".import-") or existing.is_symlink():
                continue
            info = existing.lstat()
            if stat.S_ISREG(info.st_mode):
                if _hash_existing(destination, existing) == sha256:
                    return "DUPLICATE"
    return None


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class KnowledgeImporter:
    """Reusable import core; CLI, a timer, or a future trusted broker can call run()."""

    def __init__(
        self,
        policy: ImportPolicy,
        *,
        uploader: str | None = None,
        inbox_root: Path = INBOX_ROOT,
        state_db_path: Path | None = None,
        lock_path: Path | None = None,
        publication_lock_paths: Mapping[str, Path] | None = None,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        dry_run: bool = False,
        ingest_function: Callable[[str], Mapping[str, int]] = ingest_scope,
        sleep_function: Callable[[float], None] = time.sleep,
    ) -> None:
        if not math.isfinite(settle_seconds) or not 0 <= settle_seconds <= 3600:
            raise ValueError("settle_seconds must be between 0 and 3600")
        if uploader is not None:
            if not isinstance(uploader, str) or USER_PATTERN.fullmatch(uploader) is None or uploader not in policy.routes:
                raise ImportPolicyError("uploader must be a valid name present in policy routes")
        self.policy = policy
        self.uploader = uploader
        self.inbox_root = inbox_root
        self.state_db_path = state_db_path or (
            STATE_DB_PATH.parent / uploader / STATE_DB_PATH.name if uploader else STATE_DB_PATH
        )
        self.lock_path = lock_path or (
            LOCK_PATH.parent / uploader / LOCK_PATH.name if uploader else LOCK_PATH
        )
        self.publication_lock_paths = (
            {name: scope.state_dir / "publish.lock" for name, scope in KNOWLEDGE_SCOPES.items()}
            if publication_lock_paths is None else publication_lock_paths
        )
        self.settle_seconds = settle_seconds
        self.dry_run = dry_run
        self.ingest_function = ingest_function
        self.sleep_function = sleep_function

    def _scan(self) -> list[Candidate]:
        reject_symlink_components(self.inbox_root)
        candidates: list[Candidate] = []
        users = (self.uploader,) if self.uploader is not None else sorted(self.policy.routes)
        for user in users:
            for kind in KINDS:
                folder = self.inbox_root / user / kind
                reject_symlink_components(folder)
                if not folder.exists():
                    continue
                if not folder.is_dir():
                    raise OSError(f"Inbox input is not a directory: {folder}")
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if entry.name.startswith(".") or entry.name.endswith(("~", ".tmp", ".part")):
                            continue
                        try:
                            info = Path(entry.path).lstat()
                        except FileNotFoundError:
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            continue  # Never recurse; rejected/ and destination are not inputs.
                        candidates.append(Candidate(
                            Path(entry.path), user, kind, info.st_dev, info.st_ino,
                            info.st_mode, info.st_size, info.st_mtime_ns,
                        ))
        return sorted(candidates, key=lambda item: str(item.path))

    def _stable(self, candidate: Candidate) -> bool:
        try:
            info = candidate.path.lstat()
        except FileNotFoundError:
            return False
        return _same_candidate(info, candidate)

    def _new_record(self, candidate: Candidate) -> ImportRecord:
        now = _now()
        route = self.policy.routes[candidate.uploader][candidate.kind]
        return ImportRecord(
            import_id=str(uuid.uuid4()), uploader=candidate.uploader,
            access_kind=candidate.kind, scope=route.scope,
            original_filename=candidate.path.name, final_filename=None,
            source_inbox_path=str(candidate.path), destination_path=None,
            sha256=None, size_bytes=candidate.size, status="DISCOVERED",
            error_code=None, error_message=None, created_at=now, updated_at=now,
        )

    def _decide(self, candidate: Candidate, record: ImportRecord) -> tuple[str, str | None, bytes | None]:
        path = candidate.path
        if not stat.S_ISREG(candidate.mode):
            return "REJECTED", "NOT_REGULAR", None
        if candidate.size == 0:
            record.sha256 = hashlib.sha256(b"").hexdigest()
            return "REJECTED", "EMPTY_FILE", None
        if candidate.size > self.policy.max_file_size_bytes:
            return "REJECTED", "TOO_LARGE", None
        route = self.policy.routes[candidate.uploader][candidate.kind]
        try:
            data, sha = _read_candidate(candidate, self.policy.max_file_size_bytes)
            record.sha256 = sha
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                return "REJECTED", "UNSUPPORTED_TYPE", None
            if not route.enabled:
                return "REJECTED", "ROUTE_DISABLED", None
            assert route.scope is not None and route.destination is not None
            # pypdf may log raw header bytes for malformed input; never emit document bytes.
            pdf_logger = logging.getLogger("pypdf")
            reader_logger = logging.getLogger("pypdf._reader")
            previous_levels = (pdf_logger.level, reader_logger.level)
            pdf_logger.setLevel(logging.CRITICAL)
            reader_logger.setLevel(logging.CRITICAL)
            try:
                parse_document(path, data)
            finally:
                pdf_logger.setLevel(previous_levels[0])
                reader_logger.setLevel(previous_levels[1])
            collision = _duplicate_or_conflict(route.destination, path.name, sha)
            if collision:
                return collision, collision, None
            return "IMPORTED", None, data
        except ParseError as exc:
            if path.suffix.lower() == ".pdf" and "no extractable text" in str(exc).lower():
                return "REJECTED", "NO_EXTRACTABLE_TEXT", None
            return "REJECTED", "PARSER_ERROR", None
        except UnsafeInput as exc:
            return "REJECTED", exc.code, None
        except OSError:
            return "REJECTED", "READ_OR_SOURCE_ERROR", None

    def _quarantine(self, candidate: Candidate, record: ImportRecord) -> Path:
        folder = self.inbox_root / candidate.uploader / "rejected" / candidate.kind / record.status.lower()
        reject_symlink_components(folder)
        folder.mkdir(parents=True, exist_ok=True)
        reject_symlink_components(folder)
        target = folder / f"{record.import_id}__{candidate.path.name}"
        if target.exists() or target.is_symlink():
            raise FileExistsError("quarantine target unexpectedly exists")
        if not self._stable(candidate):
            raise UnstableInput("candidate changed before quarantine")
        os.rename(candidate.path, target)
        _fsync_directory(folder)
        _fsync_directory(candidate.path.parent)
        return target

    def _publish(self, candidate: Candidate, data: bytes, sha256: str, destination: Path, import_id: str) -> Path:
        """Fsync a same-directory temp, then atomically link without clobbering final."""
        if not self._stable(candidate):
            raise UnstableInput("candidate changed before publish")
        _unused, current_sha = _read_candidate(candidate, self.policy.max_file_size_bytes, collect=False)
        if current_sha != sha256:
            raise UnstableInput("candidate contents changed before publish")
        reject_symlink_components(destination)
        final = destination / candidate.path.name
        temporary = destination / f".import-{import_id}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o640)
        try:
            with os.fdopen(fd, "wb") as target:
                for offset in range(0, len(data), READ_BLOCK_BYTES):
                    target.write(data[offset:offset + READ_BLOCK_BYTES])
                target.flush()
                if os.name != "nt":
                    os.fchmod(target.fileno(), 0o640)
                os.fsync(target.fileno())
            os.link(temporary, final)
            _fsync_directory(destination)
        finally:
            temporary.unlink(missing_ok=True)
        _fsync_directory(destination)
        if self._stable(candidate):
            candidate.path.unlink()
            _fsync_directory(candidate.path.parent)
        else:
            LOGGER.warning("source Inbox entry changed after publication; leaving it in place")
        return final

    def _audit(self, record: ImportRecord, started: float) -> None:
        LOGGER.info("audit %s", json.dumps({
            "import_id": record.import_id, "uploader": record.uploader,
            "access_kind": record.access_kind, "scope": record.scope,
            "filename": record.original_filename, "size": record.size_bytes,
            "sha256_prefix": record.sha256[:12] if record.sha256 else None,
            "status": record.status, "error_code": record.error_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }, ensure_ascii=True, sort_keys=True))

    def _process(self, candidate: Candidate, store: ImportStore | None) -> ImportRecord:
        route = self.policy.routes[candidate.uploader][candidate.kind]
        if self.dry_run or not route.enabled:
            return self._process_candidate(candidate, store)
        assert route.scope is not None
        # Held across source duplicate check and publication, then released
        # before _index_pending() calls ingest_scope() and its own scope lock.
        with scope_file_lock(self.publication_lock_paths[route.scope]):
            return self._process_candidate(candidate, store)

    def _process_candidate(self, candidate: Candidate, store: ImportStore | None) -> ImportRecord:
        started = time.perf_counter()
        record = self._new_record(candidate)
        if store is not None:
            store.insert(record)
            record.status = "VALIDATING"
            store.update(record)
        try:
            status, code, data = self._decide(candidate, record)
            record.status = ("WOULD_IMPORT" if status == "IMPORTED" else f"WOULD_{status}") if self.dry_run else status
            record.error_code = code
            if self.dry_run:
                return record
            if status == "IMPORTED":
                route = self.policy.routes[candidate.uploader][candidate.kind]
                assert route.destination is not None and data is not None and record.sha256 is not None
                try:
                    final = self._publish(candidate, data, record.sha256, route.destination, record.import_id)
                except FileExistsError:
                    record.status = _duplicate_or_conflict(route.destination, candidate.path.name, record.sha256) or "CONFLICT"
                    record.error_code = record.status
                except OSError:
                    final = route.destination / candidate.path.name
                    try:
                        published = final.is_file() and _hash_existing(route.destination, final) == record.sha256
                    except OSError:
                        published = False
                    if published:
                        record.final_filename = final.name
                        record.destination_path = str(final)
                        record.error_message = "source published; Inbox cleanup or durability check failed"
                        store.update(record)
                        return record
                    record.status = "REJECTED"
                    record.error_code = "PUBLISH_ERROR"
                else:
                    record.final_filename = final.name
                    record.destination_path = str(final)
                    store.update(record)
                    return record
            if record.status in {"REJECTED", "DUPLICATE", "CONFLICT"}:
                try:
                    rejected = self._quarantine(candidate, record)
                    record.destination_path = str(rejected)
                except (OSError, UnstableInput):
                    record.error_message = "could not quarantine; original Inbox entry retained"
                store.update(record)
            return record
        except UnstableInput:
            record.status = "REJECTED"
            record.error_code = "CHANGED_DURING_IMPORT"
            record.error_message = "input changed during import; original Inbox entry retained"
            if store is not None:
                store.update(record)
            return record
        finally:
            self._audit(record, started)

    def _index_pending(self, store: ImportStore, fresh: list[ImportRecord]) -> None:
        current = {record.import_id: record for record in fresh}
        for scope, ids in sorted(store.pending(self.uploader).items()):
            started = time.perf_counter()
            store.set_index_status(ids, "INDEXING", None)
            try:
                counts = self.ingest_function(scope)
                if type(counts.get("failed")) is not int or counts["failed"] != 0:
                    raise RuntimeError("incremental ingest reported failed files")
            except Exception:
                LOGGER.error("incremental ingest failed for scope %s", scope)
                status, code = "INDEX_ERROR", "INGEST_FAILED"
            else:
                status, code = "INDEXED", None
            store.set_index_status(ids, status, code)
            for import_id in ids:
                if import_id in current:
                    current[import_id].status = status
                    current[import_id].error_code = code
                    current[import_id].indexed_at = _now() if status == "INDEXED" else None
                    self._audit(current[import_id], started)

    def run(self) -> list[ImportRecord]:
        from contextlib import nullcontext

        guard = nullcontext() if self.dry_run else ImporterLock(self.lock_path)
        with guard:
            store = None if self.dry_run else ImportStore(self.state_db_path)
            try:
                candidates = self._scan()
                if candidates:
                    self.sleep_function(self.settle_seconds)  # Exactly one settle wait per scan.
                results: list[ImportRecord] = []
                for candidate in candidates:
                    if not self._stable(candidate):
                        LOGGER.info("skipped unstable Inbox entry: %s", candidate.path.name)
                        continue
                    results.append(self._process(candidate, store))
                if store is not None:
                    self._index_pending(store, results)
                return results
            finally:
                if store is not None:
                    store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import settled Inbox files into policy-routed knowledge sources")
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    parser.add_argument("--uploader", help="scan only this configured Unix/Inbox uploader")
    parser.add_argument("--once", action="store_true", help="run one scan (the default)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--settle-seconds", type=float, default=DEFAULT_SETTLE_SECONDS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    try:
        policy = load_policy(args.policy)
        importer = KnowledgeImporter(
            policy, uploader=args.uploader, settle_seconds=args.settle_seconds, dry_run=args.dry_run,
        )
        results = importer.run()
    except (ImportPolicyError, ImporterBusy, OSError, sqlite3.Error, ValueError) as exc:
        LOGGER.error("import aborted: %s", exc)
        return 1
    for record in results:
        print(json.dumps({
            "import_id": record.import_id, "uploader": record.uploader,
            "access_kind": record.access_kind, "filename": record.original_filename,
            "status": record.status, "error_code": record.error_code,
        }, ensure_ascii=False))
    return 1 if any(record.status in {"INDEX_ERROR", "REJECTED"} for record in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
