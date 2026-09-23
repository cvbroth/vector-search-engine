"""Read-only RAG Context API over a local Unix domain socket only."""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import signal
import socket
import socketserver
import sqlite3
import stat
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Sequence

from config import KNOWLEDGE_SCOPES, reject_symlink_components
from embeddings import EmbeddingError
from rag_context import SCHEMA_VERSION, build_rag_context

DEFAULT_SOCKET_PATH = Path("/run/knowledge-base/kb.sock")
DEFAULT_SOCKET_MODE = 0o660
MAX_BODY_BYTES = 64 * 1024
MAX_QUERY_CHARS = 4096
MAX_TOP_K = 50
REQUEST_TIMEOUT_SECONDS = 60

LOGGER = logging.getLogger(__name__)


class RequestValidationError(ValueError):
    """The client supplied an invalid context request."""


def validate_context_request(payload: Any) -> tuple[str, list[str], int]:
    """Accept only the fixed query parameters; never accept file or DB paths."""
    if not isinstance(payload, dict):
        raise RequestValidationError("JSON body must be an object")
    if set(payload) - {"query", "scopes", "top_k"}:
        raise RequestValidationError("unknown request field")

    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise RequestValidationError("query must be a non-empty string")
    if len(query) > MAX_QUERY_CHARS:
        raise RequestValidationError(f"query exceeds {MAX_QUERY_CHARS} characters")

    scopes = payload.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        raise RequestValidationError("scopes must be a non-empty array")
    if any(not isinstance(scope, str) or scope not in KNOWLEDGE_SCOPES for scope in scopes):
        raise RequestValidationError("scopes must contain only configured knowledge scopes")
    if len(set(scopes)) != len(scopes):
        raise RequestValidationError("scopes must be unique")

    top_k = payload.get("top_k", 5)
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise RequestValidationError(f"top_k must be an integer from 1 to {MAX_TOP_K}")
    return query, scopes, top_k


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RequestValidationError("duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise RequestValidationError(f"invalid JSON constant: {value}")


class KnowledgeQueryHandler(BaseHTTPRequestHandler):
    """Exactly one read-only context route, plus a process-health route."""

    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        self.request.settimeout(REQUEST_TIMEOUT_SECONDS)
        super().setup()

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("client: " + format, *args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def do_GET(self) -> None:
        if self.path != "/health":
            self._error(404, "not_found", "unknown route")
            return
        self._send_json(200, {"status": "ok", "schema_version": SCHEMA_VERSION})

    def do_POST(self) -> None:
        if self.path != "/v1/context":
            self._error(404, "not_found", "unknown route")
            return

        if self.headers.get("Transfer-Encoding") is not None:
            self._error(400, "invalid_request", "transfer encoding is not supported")
            return
        content_lengths = self.headers.get_all("Content-Length", [])
        if (
            len(content_lengths) != 1
            or not content_lengths[0].isascii()
            or not content_lengths[0].isdecimal()
        ):
            self._error(400, "invalid_request", "one valid Content-Length is required")
            return
        if len(content_lengths[0]) > 5:
            self._error(413, "payload_too_large", "request body exceeds 64 KiB")
            return
        body_length = int(content_lengths[0])
        if body_length > MAX_BODY_BYTES:
            self._error(413, "payload_too_large", "request body exceeds 64 KiB")
            return
        if body_length == 0:
            self._error(400, "invalid_request", "request body is empty")
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._error(415, "unsupported_media_type", "Content-Type must be application/json")
            return

        try:
            raw = self.rfile.read(body_length)
            if len(raw) != body_length:
                raise RequestValidationError("incomplete request body")
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
            query, scopes, top_k = validate_context_request(payload)
        except (ValueError, RecursionError) as exc:
            self._error(400, "invalid_request", str(exc))
            return
        except TimeoutError:
            self._error(408, "request_timeout", "request body timed out")
            return

        try:
            context = build_rag_context(query, scopes, top_k)
            response = context.to_dict()
            self._send_json(200, response)
        except EmbeddingError:
            LOGGER.exception("embedding service failed")
            self._error(503, "embedding_unavailable", "local embedding service unavailable")
        except (sqlite3.Error, OSError, ImportError):
            LOGGER.exception("knowledge query failed")
            self._error(500, "query_failed", "knowledge query failed")
        except Exception:
            LOGGER.exception("unexpected knowledge query failure")
            self._error(500, "internal_error", "internal service error")

    def do_PUT(self) -> None:
        self._error(405, "method_not_allowed", "method not allowed")

    do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_PUT


if hasattr(socketserver, "UnixStreamServer"):
    class KnowledgeQueryServer(socketserver.UnixStreamServer):
        allow_reuse_address = False
else:
    class KnowledgeQueryServer:
        """Permit import-only tests on platforms without AF_UNIX (e.g. Windows)."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("Unix domain sockets are not available on this platform")


def _prepare_socket_path(path: Path) -> None:
    """Never replace a live listener or any non-socket filesystem entry."""
    if not path.is_absolute() or ".." in path.parts:
        raise OSError(f"socket path must be absolute without traversal: {path}")
    reject_symlink_components(path.parent)
    if not path.parent.is_dir():
        raise OSError(f"socket directory does not exist: {path.parent}; use systemd RuntimeDirectory")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(existing.st_mode):
        raise OSError(f"refusing to replace a non-socket path: {path}")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise OSError(f"cannot verify stale socket: {path}") from exc
        else:
            raise OSError(f"another service is already listening: {path}")
    current = path.lstat()
    if not stat.S_ISSOCK(current.st_mode) or (current.st_dev, current.st_ino) != (existing.st_dev, existing.st_ino):
        raise OSError(f"socket path changed during startup: {path}")
    path.unlink()


def parse_socket_mode(value: str) -> int:
    try:
        mode = int(value, 8)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("socket mode must be octal, e.g. 0660") from exc
    if not 0o600 <= mode <= 0o770 or mode & 0o007 or mode & ~0o777:
        raise argparse.ArgumentTypeError("socket mode must grant owner access and no world access")
    if mode & 0o600 != 0o600:
        raise argparse.ArgumentTypeError("socket mode must grant owner read/write")
    return mode


def run_service(path: Path, mode: int = DEFAULT_SOCKET_MODE) -> None:
    _prepare_socket_path(path)
    with KnowledgeQueryServer(str(path), KnowledgeQueryHandler) as server:
        bound = path.lstat()
        try:
            os.chmod(path, mode)
            LOGGER.info("listening on Unix socket %s (mode %04o)", path, mode)
            server.serve_forever(poll_interval=0.5)
        finally:
            try:
                current = path.lstat()
                if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local read-only knowledge query service")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--socket-mode", type=parse_socket_mode, default=DEFAULT_SOCKET_MODE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")

    def stop_on_sigterm(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        run_service(args.socket, args.socket_mode)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        LOGGER.error("cannot start knowledge query service: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
