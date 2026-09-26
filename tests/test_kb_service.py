"""Exercise the Unix-service HTTP handler in memory; no NAS or TCP listener."""

from __future__ import annotations

import io
import json
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from embeddings import EmbeddingError
from kb_service import (
    KnowledgeQueryHandler,
    _prepare_socket_path,
    parse_socket_mode,
    run_service,
    validate_context_request,
)


class MemorySocket:
    def __init__(self, request: bytes) -> None:
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def settimeout(self, _seconds: int) -> None:
        pass

    def makefile(self, _mode: str, _buffering: int) -> io.BytesIO:
        return self.input

    def sendall(self, content: bytes) -> None:
        self.output.write(content)


def request(method: str, route: str, body: bytes = b"", *, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    fields = {"Host": "localhost", "Content-Length": str(len(body))}
    if method == "POST":
        fields["Content-Type"] = "application/json"
    fields.update(headers or {})
    head = f"{method} {route} HTTP/1.1\r\n" + "".join(f"{name}: {value}\r\n" for name, value in fields.items()) + "\r\n"
    connection = MemorySocket(head.encode("ascii") + body)
    KnowledgeQueryHandler(connection, ("local", 0), object())
    raw = connection.output.getvalue()
    response_head, response_body = raw.split(b"\r\n\r\n", 1)
    status = int(response_head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(response_body)


def context_payload(status: str) -> dict:
    evidence = [] if status == "REJECT" else [{"scope": "family", "text": "some evidence"}]
    return {
        "schema_version": "1.0",
        "query": "问题",
        "scopes": ["family"],
        "retrieval_status": status,
        "evidence_count": len(evidence),
        "evidence": evidence,
    }


VALID_BODY = json.dumps({"query": "问题", "scopes": ["family"], "top_k": 3}, ensure_ascii=False).encode("utf-8")


class KnowledgeQueryServiceTests(unittest.TestCase):
    def test_health(self) -> None:
        status, payload = request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok", "schema_version": "1.0"})

    def test_accept_uncertain_and_reject_are_all_successful_queries(self) -> None:
        for decision in ("ACCEPT", "UNCERTAIN", "REJECT"):
            with self.subTest(decision=decision):
                fake = SimpleNamespace(to_dict=lambda decision=decision: context_payload(decision))
                with patch("kb_service.build_rag_context", return_value=fake) as build:
                    status, payload = request("POST", "/v1/context", VALID_BODY)
                self.assertEqual(status, 200)
                self.assertEqual(payload["retrieval_status"], decision)
                self.assertEqual(payload["evidence_count"], 0 if decision == "REJECT" else 1)
                self.assertEqual(payload["evidence"], [] if decision == "REJECT" else payload["evidence"])
                build.assert_called_once_with("问题", ["family"], 3)

    def test_malformed_json_and_bad_arguments(self) -> None:
        cases = [
            b"{not json",
            b"{}",
            b'{"query":"","scopes":["family"]}',
            b'{"query":"q","scopes":["ziling"]}',
            b'{"query":"q","scopes":["family"],"top_k":0}',
            b'{"query":"q","scopes":["family"],"top_k":true}',
            b'{"query":"q","scopes":["family"],"source_path":"/etc/passwd"}',
            b'{"query":"q","query":"different","scopes":["family"]}',
            b'{"query":"q","scopes":["family"],"top_k":' + b"9" * 5000 + b"}",
        ]
        with patch("kb_service.build_rag_context") as build:
            for body in cases:
                with self.subTest(body=body):
                    status, payload = request("POST", "/v1/context", body)
                    self.assertEqual(status, 400)
                    self.assertIn("error", payload)
            build.assert_not_called()

    def test_query_character_limit_and_top_k_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds"):
            validate_context_request({"query": "字" * 4097, "scopes": ["family"]})
        with self.assertRaisesRegex(ValueError, "top_k"):
            validate_context_request({"query": "q", "scopes": ["family"], "top_k": 51})
        self.assertEqual(validate_context_request({"query": "q", "scopes": ["family"]}), ("q", ["family"], 5))

    def test_oversized_body_rejected_before_reading(self) -> None:
        status, payload = request("POST", "/v1/context", b"", headers={"Content-Length": "65537"})
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "payload_too_large")
        self.assertEqual(request("POST", "/v1/context", b"", headers={"Content-Length": "9" * 5000})[0], 413)

    def test_embedding_and_database_failures_are_not_reject(self) -> None:
        for error, expected in [
            (EmbeddingError("down"), 503),
            (sqlite3.OperationalError("missing DB"), 500),
            (RuntimeError("unexpected"), 500),
        ]:
            with self.subTest(error=error):
                with patch("kb_service.build_rag_context", side_effect=error), patch("kb_service.LOGGER"):
                    status, payload = request("POST", "/v1/context", VALID_BODY)
                self.assertEqual(status, expected)
                self.assertNotIn("retrieval_status", payload)
                self.assertIn("error", payload)

    def test_no_other_routes_or_mutating_methods(self) -> None:
        self.assertEqual(request("POST", "/ingest", VALID_BODY)[0], 404)
        self.assertEqual(request("GET", "/v1/context")[0], 404)
        self.assertEqual(request("DELETE", "/v1/context")[0], 405)

    def test_socket_mode_rejects_world_access(self) -> None:
        self.assertEqual(parse_socket_mode("0660"), 0o660)
        for mode in ("0777", "0666", "0600x"):
            with self.subTest(mode=mode), self.assertRaises(Exception):
                parse_socket_mode(mode)

    def test_socket_path_does_not_replace_files_or_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kb.sock"
            path.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(OSError, "non-socket"):
                _prepare_socket_path(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep")
            path.unlink()
            path.mkdir()
            with self.assertRaisesRegex(OSError, "non-socket"):
                _prepare_socket_path(path)
            self.assertTrue(path.is_dir())
            path.rmdir()
            target = Path(directory) / "target.txt"
            target.write_text("keep", encoding="utf-8")
            try:
                path.symlink_to(target)
            except OSError:
                pass  # Windows may deny symlink creation to non-admin test processes.
            else:
                with self.assertRaisesRegex(OSError, "non-socket"):
                    _prepare_socket_path(path)
                self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "host lacks Unix domain sockets")
    def test_stale_socket_replaced_live_socket_preserved_and_own_socket_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kb.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
                stale.bind(str(path))
            _prepare_socket_path(path)
            self.assertFalse(path.exists())

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as live:
                live.bind(str(path))
                live.listen()
                with self.assertRaisesRegex(OSError, "already listening"):
                    _prepare_socket_path(path)
                self.assertTrue(path.exists())
            path.unlink()

            with patch("kb_service.KnowledgeQueryServer.serve_forever"):
                run_service(path)
            self.assertFalse(path.exists())

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed locally")
    def test_node_client_constructs_fixed_unix_request(self) -> None:
        client_source = (Path(__file__).resolve().parents[1] / "clients" / "openclaw_kb_client.mjs").read_text(encoding="utf-8")
        script = client_source + """
const args = parseArgs(['--query','问题','--scope','chen','--scope','family','--top-k','3']);
const request = buildRequest(args);
const reject = {schema_version:'1.0', query:'问题', scopes:['chen','family'],
                retrieval_status:'REJECT', evidence_count:0, evidence:[]};
validateResponse(reject, args);
let invalidResponseRejected = false;
try { validateResponse({}, args); } catch { invalidResponseRejected = true; }
let urlRejected = false;
try { parseArgs(['--query','q','--scope','family','--socket','http://example.com']); }
catch { urlRejected = true; }
console.log(JSON.stringify({ options: request.options, body: JSON.parse(request.body),
                             invalidResponseRejected, urlRejected }));
"""
        completed = subprocess.run(
            [shutil.which("node") or "node", "--input-type=module", "-"],
            input=script.encode("utf-8"), capture_output=True, check=True,
        )
        payload = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(payload["options"]["socketPath"], "/run/knowledge-base/backend.sock")
        self.assertEqual(payload["options"]["path"], "/v1/context")
        self.assertEqual(payload["options"]["method"], "POST")
        self.assertNotIn("hostname", payload["options"])
        self.assertNotIn("port", payload["options"])
        self.assertEqual(payload["body"], {"query": "问题", "scopes": ["chen", "family"], "top_k": 3})
        self.assertTrue(payload["invalidResponseRejected"])
        self.assertTrue(payload["urlRejected"])


if __name__ == "__main__":
    unittest.main()
