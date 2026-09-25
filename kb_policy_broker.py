"""Central, fail-closed policy broker for the local knowledge query service."""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import logging
import math
import os
import re
import signal
import socket
import socketserver
import stat
import struct
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from config import KNOWLEDGE_SCOPES, get_scope, reject_symlink_components

POLICY_PATH = Path("/etc/knowledge-broker/policy.json")
BACKEND_SOCKET_PATH = Path("/run/knowledge-base/backend.sock")
BROKER_SOCKET_PATH = Path("/run/knowledge-broker/query.sock")
SOCKET_MODE = 0o660
MAX_POLICY_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_QUERY_CHARS = 4096
MAX_TOP_K = 10
BACKEND_TIMEOUT_SECONDS = 8
REQUEST_TIMEOUT_SECONDS = 10
SCHEMA_VERSION = "1.0"
AGENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")

LOGGER = logging.getLogger(__name__)


class PolicyError(ValueError):
    """Missing or invalid policy prevents broker startup."""


class RequestError(ValueError):
    """Invalid caller input; safe to classify as HTTP 400."""


class BackendError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    private_scope: str | None
    shared: bool


@dataclass(frozen=True, slots=True)
class BrokerPolicy:
    shared_scope: str
    agents: Mapping[str, AgentPolicy]

    def resolve(self, agent_id: str, kind: str) -> str | None:
        rule = self.agents.get(agent_id)
        if rule is None:
            return None
        if kind == "PRIVATE":
            return rule.private_scope
        if kind == "SHARED" and rule.shared:
            return self.shared_scope
        return None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_json(raw: bytes) -> Any:
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
    )


def load_policy(path: Path = POLICY_PATH) -> BrokerPolicy:
    """Read a fixed-format policy once, before opening the broker socket."""
    try:
        reject_symlink_components(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_POLICY_BYTES:
            raise PolicyError("policy must be a regular file no larger than 64 KiB")
        with path.open("rb") as source:
            raw = source.read(MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise PolicyError("policy exceeds 64 KiB")
        value = _parse_json(raw)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise PolicyError(f"cannot load knowledge broker policy: {exc}") from exc

    if not isinstance(value, dict) or set(value) != {"schema_version", "shared_scope", "agents"}:
        raise PolicyError("policy must contain only schema_version, shared_scope, agents")
    if value["schema_version"] != SCHEMA_VERSION:
        raise PolicyError("unsupported broker policy schema_version")
    shared_scope = value["shared_scope"]
    if shared_scope != "family" or shared_scope not in KNOWLEDGE_SCOPES:
        raise PolicyError("shared_scope must be the configured family scope")
    if get_scope(shared_scope).area != "shared":
        raise PolicyError("shared_scope is not a shared knowledge scope")
    raw_agents = value["agents"]
    if not isinstance(raw_agents, dict):
        raise PolicyError("agents must be an object")
    agents: dict[str, AgentPolicy] = {}
    for agent_id, rule in raw_agents.items():
        if not isinstance(agent_id, str) or AGENT_ID_PATTERN.fullmatch(agent_id) is None:
            raise PolicyError("invalid agent ID in policy")
        if not isinstance(rule, dict) or set(rule) != {"private_scope", "shared"}:
            raise PolicyError("each agent must have private_scope and shared only")
        private_scope = rule["private_scope"]
        if private_scope is not None:
            if not isinstance(private_scope, str) or private_scope not in KNOWLEDGE_SCOPES:
                raise PolicyError("policy refers to an unknown private scope")
            if get_scope(private_scope).area != "private":
                raise PolicyError("private_scope must be a private knowledge scope")
        if type(rule["shared"]) is not bool:
            raise PolicyError("shared must be a boolean")
        agents[agent_id] = AgentPolicy(private_scope, rule["shared"])
    return BrokerPolicy(shared_scope, MappingProxyType(agents))


def validate_request(value: Any) -> tuple[str, str, int]:
    if not isinstance(value, dict) or set(value) - {"agent_id", "query", "top_k"}:
        raise RequestError("request must contain only agent_id, query, top_k")
    agent_id = value.get("agent_id")
    if not isinstance(agent_id, str) or AGENT_ID_PATTERN.fullmatch(agent_id) is None:
        raise RequestError("agent_id is required")
    query = value.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise RequestError("query must contain 1 to 4096 Unicode characters")
    try:
        query.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RequestError("query must be valid Unicode") from exc
    top_k = value.get("top_k", 5)
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise RequestError("top_k must be an integer from 1 to 10")
    return agent_id, query, top_k


def _is_number(value: Any) -> bool:
    return type(value) in (float, int) and math.isfinite(value)


def validate_backend_context(value: Any, query: str, scope: str, top_k: int) -> dict[str, Any]:
    top_keys = {"schema_version", "query", "scopes", "retrieval_status", "evidence_count", "evidence"}
    evidence_keys = {
        "scope", "rank", "fused_score", "semantic_score", "semantic_distance",
        "lexical_match", "lexical_score", "relevance_decision", "source_path",
        "filename", "page", "chunk_index", "text",
    }
    if not isinstance(value, dict) or set(value) != top_keys:
        raise BackendError(502, "invalid_backend_response")
    items = value["evidence"]
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["query"] != query
        or value["scopes"] != [scope]
        or not isinstance(value["retrieval_status"], str)
        or value["retrieval_status"] not in {"ACCEPT", "UNCERTAIN", "REJECT"}
        or type(value["evidence_count"]) is not int
        or not isinstance(items, list)
        or value["evidence_count"] != len(items)
        or len(items) > top_k
    ):
        raise BackendError(502, "invalid_backend_response")
    has_accept = False
    for rank, item in enumerate(items, 1):
        if not isinstance(item, dict) or set(item) != evidence_keys:
            raise BackendError(502, "invalid_backend_response")
        if (
            item["scope"] != scope or type(item["rank"]) is not int or item["rank"] != rank
            or not _is_number(item["fused_score"])
            or any(item[field] is not None and not _is_number(item[field]) for field in
                   ("semantic_score", "semantic_distance", "lexical_score"))
            or type(item["lexical_match"]) is not bool
            or not isinstance(item["relevance_decision"], str)
            or item["relevance_decision"] not in {"ACCEPT", "UNCERTAIN"}
            or not isinstance(item["source_path"], str)
            or not isinstance(item["filename"], str)
            or (item["page"] is not None and (type(item["page"]) is not int or item["page"] < 1))
            or type(item["chunk_index"]) is not int or item["chunk_index"] < 0
            or not isinstance(item["text"], str)
        ):
            raise BackendError(502, "invalid_backend_response")
        has_accept |= item["relevance_decision"] == "ACCEPT"
    expected = "REJECT" if not items else "ACCEPT" if has_accept else "UNCERTAIN"
    if value["retrieval_status"] != expected:
        raise BackendError(502, "invalid_backend_response")
    return value


def to_model_context(value: Any, query: str, scope: str, top_k: int) -> dict[str, Any]:
    """Validate the internal protocol, then project only model-facing fields."""
    internal = validate_backend_context(value, query, scope, top_k)
    evidence = [
        {
            "rank": item["rank"],
            "fused_score": item["fused_score"],
            "semantic_score": item["semantic_score"],
            "semantic_distance": item["semantic_distance"],
            "lexical_match": item["lexical_match"],
            "lexical_score": item["lexical_score"],
            "relevance_decision": item["relevance_decision"],
            "filename": item["filename"],
            "page": item["page"],
            "chunk_index": item["chunk_index"],
            "text": item["text"],
        }
        for item in internal["evidence"]
    ]
    return {
        "schema_version": internal["schema_version"],
        "query": internal["query"],
        "retrieval_status": internal["retrieval_status"],
        "evidence_count": internal["evidence_count"],
        "evidence": evidence,
    }


class UnixBackendConnection(http.client.HTTPConnection):
    def __init__(self) -> None:
        super().__init__("localhost", timeout=BACKEND_TIMEOUT_SECONDS)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(BACKEND_TIMEOUT_SECONDS)
        self.sock.connect(str(BACKEND_SOCKET_PATH))


def forward_context(query: str, scope: str, top_k: int) -> dict[str, Any]:
    """The only broker-to-backend request; caller never supplies a socket or path."""
    body = json.dumps(
        {"query": query, "scopes": [scope], "top_k": top_k},
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    connection = UnixBackendConnection()
    try:
        connection.request("POST", "/v1/context", body, {
            "Content-Type": "application/json", "Accept": "application/json",
        })
        response = connection.getresponse()
        if response.status != 200:
            if response.status == 503:
                raise BackendError(503, "backend_unavailable")
            if response.status >= 500:
                raise BackendError(500, "backend_failure")
            raise BackendError(502, "backend_protocol_error")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BackendError(502, "backend_response_too_large")
        try:
            value = _parse_json(raw)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise BackendError(502, "invalid_backend_json") from exc
        return validate_backend_context(value, query, scope, top_k)
    except TimeoutError as exc:
        raise BackendError(504, "backend_timeout") from exc
    except OSError as exc:
        raise BackendError(503, "backend_unavailable") from exc
    except http.client.HTTPException as exc:
        raise BackendError(502, "backend_protocol_error") from exc
    finally:
        connection.close()


def _peer_credentials(connection: socket.socket) -> dict[str, int | None]:
    peer: dict[str, int | None] = {"peer_pid": None, "peer_uid": None, "peer_gid": None}
    if not hasattr(socket, "SO_PEERCRED"):
        return peer
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
        peer.update(peer_pid=pid, peer_uid=uid, peer_gid=gid)
    except (OSError, AttributeError, TypeError):
        pass
    return peer


class BrokerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        self.request.settimeout(REQUEST_TIMEOUT_SECONDS)
        super().setup()

    def log_message(self, _format: str, *_args: object) -> None:
        pass  # Structured audit below is the only request log; never log URL/query text.

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _error(self, status: int, code: str) -> None:
        self._send_json(status, {"error": code})

    def _audit(self, record: dict[str, Any]) -> None:
        record["latency_ms"] = round((time.perf_counter() - record.pop("started")) * 1000, 3)
        LOGGER.info("audit %s", json.dumps(record, ensure_ascii=True, sort_keys=True))

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "schema_version": SCHEMA_VERSION})
        else:
            self._error(404, "not_found")

    def do_POST(self) -> None:
        kind = {"/v1/private-context": "PRIVATE", "/v1/shared-context": "SHARED"}.get(self.path)
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": str(uuid.uuid4()),
            "agent_id": None,
            "access_kind": kind,
            "resolved_scope": None,
            "decision": "ERROR",
            "retrieval_status": None,
            "evidence_count": None,
            "query_length": None,
            "query_sha256_prefix": None,
            "started": time.perf_counter(),
            **_peer_credentials(self.request),
        }
        try:
            if kind is None:
                self._error(404, "not_found")
                return
            if self.headers.get("Transfer-Encoding") is not None:
                raise RequestError("transfer encoding not supported")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                raise RequestError("one valid Content-Length is required")
            if len(lengths[0]) > 5 or int(lengths[0]) > MAX_REQUEST_BYTES:
                self._error(413, "payload_too_large")
                return
            if int(lengths[0]) == 0:
                raise RequestError("empty body")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._error(415, "unsupported_media_type")
                return
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise RequestError("incomplete request body")
            value = _parse_json(raw)
            if isinstance(value, dict):
                agent_id = value.get("agent_id")
                if isinstance(agent_id, str) and AGENT_ID_PATTERN.fullmatch(agent_id) is not None:
                    record["agent_id"] = agent_id
                query = value.get("query")
                if isinstance(query, str):
                    record["query_length"] = len(query)
                    try:
                        record["query_sha256_prefix"] = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
                    except UnicodeError:
                        pass
            agent_id, query, top_k = validate_request(value)
            policy: BrokerPolicy = self.server.policy  # type: ignore[attr-defined]
            scope = policy.resolve(agent_id, kind)
            if scope is None:
                record["decision"] = "DENY"
                self._error(403, "private_knowledge_not_authorized" if kind == "PRIVATE" else "shared_knowledge_not_authorized")
                return
            record["resolved_scope"] = scope
            context = to_model_context(forward_context(query, scope, top_k), query, scope, top_k)
            record["decision"] = "ALLOW"
            record["retrieval_status"] = context["retrieval_status"]
            record["evidence_count"] = context["evidence_count"]
            self._send_json(200, context)
        except (ValueError, RecursionError):
            self._error(400, "invalid_request")
        except TimeoutError:
            self._error(408, "request_timeout")
        except BackendError as exc:
            self._error(exc.status, exc.code)
        except Exception:
            LOGGER.exception("broker request failed")
            self._error(500, "broker_failure")
        finally:
            self._audit(record)

    def do_PUT(self) -> None:
        self._error(405, "method_not_allowed")

    do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_PUT


if hasattr(socketserver, "UnixStreamServer"):
    class BrokerServer(socketserver.UnixStreamServer):
        allow_reuse_address = False

        def __init__(self, path: Path, policy: BrokerPolicy) -> None:
            self.policy = policy
            super().__init__(str(path), BrokerHandler)
else:
    class BrokerServer:
        def __init__(self, _path: Path, _policy: BrokerPolicy) -> None:
            raise OSError("Unix domain sockets are not available on this platform")


def _prepare_socket(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("broker socket path must be absolute without traversal")
    reject_symlink_components(path.parent)
    if not path.parent.is_dir():
        raise OSError(f"broker socket directory missing: {path.parent}; use systemd RuntimeDirectory")
    try:
        previous = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(previous.st_mode):
        raise OSError("refusing to replace a non-socket broker path")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise OSError("cannot verify stale broker socket") from exc
        else:
            raise OSError("another broker is already listening")
    current = path.lstat()
    if not stat.S_ISSOCK(current.st_mode) or (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
        raise OSError("broker socket changed during startup")
    path.unlink()


def run_broker(policy: BrokerPolicy, path: Path = BROKER_SOCKET_PATH) -> None:
    _prepare_socket(path)
    with BrokerServer(path, policy) as server:
        bound = path.lstat()
        try:
            os.chmod(path, SOCKET_MODE)
            LOGGER.info("knowledge broker listening on Unix socket %s", path)
            server.serve_forever(poll_interval=0.5)
        finally:
            try:
                current = path.lstat()
                if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    try:
        policy = load_policy(POLICY_PATH)
    except PolicyError as exc:
        LOGGER.error("broker startup refused: %s", exc)
        return 1

    def stop_on_sigterm(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        run_broker(policy)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        LOGGER.error("cannot start knowledge broker: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
