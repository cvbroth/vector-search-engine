"""Local-only Unix-socket API for temporary PDF indexing and retrieval."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote

from kb_service import _prepare_socket_path, parse_socket_mode
from session_documents import (
    DEFAULT_LIMITS, DEFAULT_ROOT, SessionDocumentError, SessionDocumentKey,
    SessionDocumentLimits, SessionDocumentStore,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_SOCKET = Path("/run/knowledge-session-doc/query.sock")
MAX_JSON_BYTES = 8192


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SessionDocumentError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SessionDocumentError(f"invalid JSON constant: {value}")


class SessionDocumentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    store: SessionDocumentStore

    def setup(self) -> None:
        self.request.settimeout(30)
        super().setup()

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("client: " + format, *args)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
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

    def _body(self, maximum: int, content_type: str) -> bytes:
        if self.headers.get("Transfer-Encoding") is not None:
            raise SessionDocumentError("transfer encoding is not supported")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            raise SessionDocumentError("one valid Content-Length is required")
        length = int(lengths[0])
        if length < 1 or length > maximum:
            raise SessionDocumentError("request body exceeds limit")
        actual_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if actual_type != content_type:
            raise SessionDocumentError("unsupported request content type")
        body = self.rfile.read(length)
        if len(body) != length:
            raise SessionDocumentError("incomplete request body")
        return body

    def _json(self) -> dict[str, Any]:
        value = json.loads(
            self._body(MAX_JSON_BYTES, "application/json").decode("utf-8"),
            object_pairs_hook=_unique_fields, parse_constant=_reject_constant,
        )
        if not isinstance(value, dict):
            raise SessionDocumentError("request must be a JSON object")
        return value

    def _header(self, name: str) -> str:
        values = self.headers.get_all(name, [])
        if len(values) != 1 or not values[0] or len(values[0]) > 512:
            raise SessionDocumentError("missing or invalid request identity")
        return values[0]

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:
        try:
            if self.path == "/v1/index":
                key = SessionDocumentKey(
                    self._header("X-Session-Agent"), self._header("X-Session-Hash"),
                    self._header("X-Attachment-Id"),
                )
                filename = unquote(self._header("X-Attachment-Filename"), errors="strict")
                data = self._body(self.store.limits.max_attachment_bytes, "application/pdf")
                status = self.store.submit(key, filename, data)
                self._send(200, {"status": status, "attachment_id": key.attachment_id})
                return
            if self.path not in {"/v1/list", "/v1/query"}:
                self._send(404, {"error": "not_found"})
                return
            payload = self._json()
            expected = {"agent_id", "session_hash"} if self.path == "/v1/list" else {
                "agent_id", "session_hash", "attachment_id", "query", "top_k",
            }
            if set(payload) != expected:
                raise SessionDocumentError("invalid request fields")
            if type(payload["agent_id"]) is not str or type(payload["session_hash"]) is not str:
                raise SessionDocumentError("invalid request identity")
            if self.path == "/v1/list":
                # Validate both dimensions even when no document exists.
                SessionDocumentKey(payload["agent_id"], payload["session_hash"], "0" * 32)
                self._send(200, {"documents": self.store.list_documents(
                    payload["agent_id"], payload["session_hash"],
                )})
                return
            if type(payload["attachment_id"]) is not str or type(payload["query"]) is not str:
                raise SessionDocumentError("invalid query")
            key = SessionDocumentKey(
                payload["agent_id"], payload["session_hash"], payload["attachment_id"],
            )
            self._send(200, self.store.query(key, payload["query"], payload["top_k"]))
        except (SessionDocumentError, UnicodeError, ValueError, OverflowError) as exc:
            LOGGER.info("rejected session document request: %s", type(exc).__name__)
            self._send(400, {"error": "invalid_request"})
        except TimeoutError:
            self._send(408, {"error": "request_timeout"})
        except Exception:
            LOGGER.exception("session document service error")
            self._send(500, {"error": "service_failure"})

    def do_PUT(self) -> None:
        self._send(405, {"error": "method_not_allowed"})

    do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_PUT


if hasattr(socketserver, "UnixStreamServer"):
    class SessionDocumentServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        allow_reuse_address = False
        daemon_threads = True
else:
    class SessionDocumentServer:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("Unix sockets are unavailable")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Temporary session PDF service")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--socket-mode", type=parse_socket_mode, default=0o660)
    parser.add_argument("--ttl-hours", type=int, default=DEFAULT_LIMITS.ttl_hours)
    parser.add_argument("--cleanup", action="store_true", help="clean expired/interrupted jobs and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    limits = SessionDocumentLimits(ttl_hours=args.ttl_hours)
    store = SessionDocumentStore(args.root, limits=limits)
    removed = store.cleanup()
    LOGGER.info("session document cleanup removed %d jobs", removed)
    if args.cleanup:
        return 0
    _prepare_socket_path(args.socket)
    SessionDocumentHandler.store = store
    stop_cleanup = threading.Event()

    def periodic_cleanup() -> None:
        while not stop_cleanup.wait(3600):
            try:
                store.cleanup()
            except Exception:
                LOGGER.exception("periodic session document cleanup failed")

    threading.Thread(target=periodic_cleanup, daemon=True).start()
    with SessionDocumentServer(str(args.socket), SessionDocumentHandler) as server:
        bound = args.socket.lstat()
        try:
            os.chmod(args.socket, args.socket_mode)
            LOGGER.info("session document service listening")
            server.serve_forever(poll_interval=0.5)
        finally:
            stop_cleanup.set()
            current = args.socket.lstat()
            if (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                args.socket.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
