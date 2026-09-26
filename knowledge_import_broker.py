"""Narrow Unix-socket broker: stream trusted tool attachments into user Inbox only."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import signal
import socket
import socketserver
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import unquote

from config import PRIVATE_SCOPE_BY_AGENT, PRIVATE_SCOPE_BY_UPLOADER, reject_symlink_components

POLICY_PATH = Path("/etc/knowledge-import-broker/policy.json")
SOCKET_PATH = Path("/run/knowledge-import-broker/import.sock")
INBOX_ROOT = Path("/srv/storage/ai-inbox")
MAX_POLICY_BYTES = 64 * 1024
MAX_FILENAME_BYTES = 180
MAX_HEADER_BYTES = 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
READ_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30
TRANSFER_DEADLINE_SECONDS = 120
SOCKET_MODE = 0o660
NAME = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
SUFFIXES = frozenset({".pdf", ".docx", ".md", ".txt"})
META_HEADERS = frozenset({
    "x-knowledge-agent", "x-knowledge-filename", "x-knowledge-content-type",
    "x-knowledge-message-id",
})
LOGGER = logging.getLogger(__name__)


class PolicyError(ValueError):
    """Bad policy prevents startup."""


class RequestError(ValueError):
    """Client metadata or transfer is invalid."""


class TooLargeError(RequestError):
    """Attachment exceeds the broker's policy limit."""


@dataclass(frozen=True, slots=True)
class AgentRule:
    uploader: str
    private: bool
    shared: bool


@dataclass(frozen=True, slots=True)
class ImportBrokerPolicy:
    max_file_size_bytes: int
    agents: Mapping[str, AgentRule]

    def resolve(self, agent_id: str, kind: str) -> str | None:
        rule = self.agents.get(agent_id)
        if rule is None or not getattr(rule, kind, False):
            return None
        return rule.uploader


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError("duplicate policy key")
        result[key] = value
    return result


def load_policy(path: Path = POLICY_PATH) -> ImportBrokerPolicy:
    try:
        reject_symlink_components(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_POLICY_BYTES:
            raise PolicyError("policy must be a small regular file")
        if sys.platform == "linux" and stat.S_IMODE(info.st_mode) & 0o022:
            raise PolicyError("policy must not be group/world writable")
        if sys.platform == "linux" and os.geteuid() == 0 and info.st_uid != 0:
            raise PolicyError("privileged broker policy must be root owned")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=lambda _value: (_ for _ in ()).throw(PolicyError("invalid constant")))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise PolicyError("cannot load import broker policy") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "max_file_size_bytes", "agents"}:
        raise PolicyError("invalid policy fields")
    if value["schema_version"] != "1.0":
        raise PolicyError("unsupported policy schema")
    maximum = value["max_file_size_bytes"]
    if type(maximum) is not int or not 0 < maximum <= MAX_FILE_BYTES:
        raise PolicyError("invalid max_file_size_bytes")
    raw_agents = value["agents"]
    if not isinstance(raw_agents, dict) or not raw_agents:
        raise PolicyError("agents must be a nonempty object")
    agents: dict[str, AgentRule] = {}
    for agent_id, item in raw_agents.items():
        if not isinstance(agent_id, str) or not NAME.fullmatch(agent_id):
            raise PolicyError("invalid agent ID")
        if agent_id not in PRIVATE_SCOPE_BY_AGENT:
            raise PolicyError("unknown agent ID in policy")
        if not isinstance(item, dict) or set(item) != {"uploader", "private", "shared"}:
            raise PolicyError("invalid agent rule")
        uploader = item["uploader"]
        if not isinstance(uploader, str) or not NAME.fullmatch(uploader):
            raise PolicyError("invalid uploader")
        if (uploader not in PRIVATE_SCOPE_BY_UPLOADER
                or PRIVATE_SCOPE_BY_UPLOADER[uploader] != PRIVATE_SCOPE_BY_AGENT[agent_id]):
            raise PolicyError("uploader does not match agent identity")
        if type(item["private"]) is not bool or type(item["shared"]) is not bool:
            raise PolicyError("access flags must be booleans")
        agents[agent_id] = AgentRule(uploader, item["private"], item["shared"])
    return ImportBrokerPolicy(maximum, MappingProxyType(agents))


def validate_filename(filename: str) -> str:
    if (not filename or filename in {".", ".."} or "/" in filename or "\\" in filename
            or "\x00" in filename or filename.startswith(".") or filename.endswith("~")
            or any(ord(char) < 32 for char in filename)
            or len(filename.encode("utf-8")) > MAX_FILENAME_BYTES):
        raise RequestError("invalid filename")
    if Path(filename).suffix.lower() not in SUFFIXES:
        raise RequestError("unsupported file type")
    return filename


def _header(headers: Any, name: str, *, required: bool = True) -> str | None:
    values = headers.get_all(name, [])
    if len(values) > 1 or (required and len(values) != 1):
        raise RequestError("missing or duplicate metadata header")
    if not values:
        return None
    value = values[0]
    if not value.isascii() or len(value) > MAX_HEADER_BYTES:
        raise RequestError("invalid metadata header")
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeError as exc:
        raise RequestError("invalid metadata encoding") from exc
    if "\x00" in decoded or "\r" in decoded or "\n" in decoded:
        raise RequestError("invalid metadata value")
    return decoded


def validate_headers(headers: Any, policy: ImportBrokerPolicy, kind: str) -> tuple[str, str, str, int]:
    if any(key.lower() in {"destination", "path", "scope", "uploader", "url",
                            "x-destination", "x-path", "x-scope", "x-uploader", "x-url"}
           for key in headers):
        raise RequestError("client routing metadata is forbidden")
    if any(key.lower().startswith("x-knowledge-") and key.lower() not in META_HEADERS for key in headers):
        raise RequestError("unknown knowledge metadata header")
    if headers.get("Transfer-Encoding") is not None:
        raise RequestError("transfer encoding is not supported")
    lengths = headers.get_all("Content-Length", [])
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
        raise RequestError("one Content-Length is required")
    length = int(lengths[0]) if len(lengths[0]) <= 12 else MAX_FILE_BYTES + 1
    if length < 1:
        raise RequestError("empty attachment")
    if length > policy.max_file_size_bytes:
        raise TooLargeError("file exceeds configured size limit")
    if headers.get("Content-Type", "").lower() != "application/octet-stream":
        raise RequestError("content type must be application/octet-stream")
    agent_id = _header(headers, "X-Knowledge-Agent")
    filename = _header(headers, "X-Knowledge-Filename")
    content_type = _header(headers, "X-Knowledge-Content-Type")
    _header(headers, "X-Knowledge-Message-Id", required=False)
    if agent_id is None or not NAME.fullmatch(agent_id):
        raise RequestError("invalid agent ID")
    if policy.resolve(agent_id, kind) is None:
        raise PermissionError("agent is not authorized for import")
    if filename is None or content_type is None or len(content_type) > 200:
        raise RequestError("invalid attachment metadata")
    return agent_id, validate_filename(filename), content_type, length


def _owner(uploader: str) -> tuple[int, int]:
    import pwd  # Linux host only; not imported by Windows unit tests.

    entry = pwd.getpwnam(uploader)
    return entry.pw_uid, entry.pw_gid


def _fsync_directory(fd: int) -> None:
    os.fsync(fd)


def queue_to_inbox(
    body: Any, length: int, destination: Path, uploader: str, filename: str,
    *, now: Any = time.monotonic, owner: Any = _owner,
) -> tuple[str, int, str]:
    """Stream into one uploader Inbox, then publish an unpredictable queue name."""
    reject_symlink_components(destination)
    if not destination.is_dir():
        raise OSError("configured Inbox directory is missing")
    uid, gid = owner(uploader)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(destination, flags)
    temporary: str | None = None
    try:
        directory = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory.st_mode):
            raise OSError("Inbox is not a directory")
        temporary = f".upload-{uuid.uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600, dir_fd=directory_fd)
        sha = hashlib.sha256()
        deadline = now() + TRANSFER_DEADLINE_SECONDS
        try:
            with os.fdopen(fd, "wb") as target:
                remaining = length
                while remaining:
                    if now() > deadline:
                        raise TimeoutError("attachment transfer deadline exceeded")
                    part = body.read(min(READ_BYTES, remaining))
                    if not part:
                        raise RequestError("incomplete attachment body")
                    target.write(part)
                    sha.update(part)
                    remaining -= len(part)
                target.flush()
                os.fchown(target.fileno(), uid, gid)
                os.fchmod(target.fileno(), 0o600)
                os.fsync(target.fileno())
            for _attempt in range(10):
                final = f"kb-{uuid.uuid4().hex}__{filename}"
                try:
                    os.stat(final, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    # This single trusted broker serializes requests; random queue
                    # names and private 0700 Inbox prevent cooperative collisions.
                    os.rename(temporary, final, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    temporary = None
                    _fsync_directory(directory_fd)
                    return final, length, sha.hexdigest()[:12]
            raise FileExistsError("could not allocate a unique Inbox filename")
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
    finally:
        os.close(directory_fd)


class ImportHandler(BaseHTTPRequestHandler):
    server_version = "KnowledgeImportBroker/1.0"

    def log_message(self, _format: str, *_args: Any) -> None:
        return  # Do not log request metadata or attachment contents.

    def _reply(self, status: int, result: dict[str, Any]) -> None:
        raw = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        self.close_connection = True
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
        kind = {"/v1/private-attachment": "private", "/v1/shared-attachment": "shared"}.get(self.path)
        if kind is None:
            self._reply(404, {"error": "not_found"})
            return
        try:
            policy: ImportBrokerPolicy = self.server.policy  # type: ignore[attr-defined]
            agent_id, filename, content_type, length = validate_headers(self.headers, policy, kind)
            uploader = policy.resolve(agent_id, kind)
            assert uploader is not None
            destination = self.server.inbox_root / uploader / kind  # type: ignore[attr-defined]
            final, size, digest = queue_to_inbox(self.rfile, length, destination, uploader, filename)
            LOGGER.info("queued attachment agent=%s kind=%s size=%d digest_prefix=%s", agent_id, kind, size, digest)
            self._reply(200, {"status": "QUEUED", "filename": final, "content_type": content_type,
                              "size_bytes": size, "sha256_prefix": digest, "access_kind": kind})
        except PermissionError:
            self._reply(403, {"error": "authorization_denied"})
        except TooLargeError:
            self._reply(413, {"error": "payload_too_large"})
        except RequestError:
            self._reply(400, {"error": "invalid_request"})
        except TimeoutError:
            self._reply(408, {"error": "request_timeout"})
        except Exception:
            LOGGER.exception("import broker request failed")
            self._reply(500, {"error": "broker_failure"})

    def do_GET(self) -> None:
        self._reply(405, {"error": "method_not_allowed"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_GET


if hasattr(socketserver, "UnixStreamServer"):
    class ImportServer(socketserver.UnixStreamServer):
        allow_reuse_address = False

        def __init__(self, path: Path, policy: ImportBrokerPolicy, inbox_root: Path = INBOX_ROOT) -> None:
            self.policy = policy
            self.inbox_root = inbox_root
            super().__init__(str(path), ImportHandler)
else:
    class ImportServer:
        def __init__(self, _path: Path, _policy: ImportBrokerPolicy, _inbox_root: Path = INBOX_ROOT) -> None:
            raise OSError("Unix domain sockets are unavailable")


def _prepare_socket(path: Path) -> None:
    reject_symlink_components(path.parent)
    if not path.parent.is_dir():
        raise OSError("socket runtime directory is missing")
    try:
        previous = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(previous.st_mode):
        raise OSError("refusing to replace a non-socket path")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise OSError("cannot verify stale import socket") from exc
        else:
            raise OSError("another import broker is already listening")
    current = path.lstat()
    if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
        raise OSError("socket changed during startup")
    path.unlink()


def run_broker(policy: ImportBrokerPolicy, path: Path = SOCKET_PATH) -> None:
    _prepare_socket(path)
    with ImportServer(path, policy) as server:
        bound = path.lstat()
        try:
            os.chmod(path, SOCKET_MODE)
            server.serve_forever(poll_interval=0.5)
        finally:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    if sys.platform != "linux" or os.geteuid() != 0:
        LOGGER.error("import broker requires a privileged Linux host service")
        return 1
    try:
        policy = load_policy()
        signal.signal(signal.SIGTERM, lambda _signum, _frame: (_ for _ in ()).throw(SystemExit(0)))
        run_broker(policy)
    except (PolicyError, OSError) as exc:
        LOGGER.error("cannot start import broker: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
