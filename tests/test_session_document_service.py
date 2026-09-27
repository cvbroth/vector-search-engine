"""Exercise the temporary-document HTTP boundary in memory, without a listener."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_document_service import SessionDocumentHandler
from session_documents import SessionDocumentKey, SessionDocumentStore, session_hash


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


def request(route: str, body: bytes, content_type: str = "application/json",
            headers: dict[str, str] | None = None) -> tuple[int, dict]:
    fields = {"Host": "localhost", "Content-Length": str(len(body)), "Content-Type": content_type}
    fields.update(headers or {})
    head = f"POST {route} HTTP/1.1\r\n" + "".join(
        f"{name}: {value}\r\n" for name, value in fields.items()
    ) + "\r\n"
    connection = MemorySocket(head.encode("ascii") + body)
    SessionDocumentHandler(connection, ("local", 0), object())
    response_head, response_body = connection.output.getvalue().split(b"\r\n\r\n", 1)
    status = int(response_head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(response_body)


class SessionDocumentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionDocumentStore(Path(self.temp.name) / "session-documents")
        SessionDocumentHandler.store = self.store
        self.key = SessionDocumentKey("chen", session_hash("agent:chen:sessionA"), "a" * 32)

    def test_upload_uses_fixed_identity_and_does_not_index_on_list(self) -> None:
        status, result = request("/v1/list", json.dumps({
            "agent_id": self.key.agent_id, "session_hash": self.key.session_hash,
        }).encode())
        self.assertEqual((status, result), (200, {"documents": []}))
        self.assertEqual(list(self.store.root.iterdir()), [])

        headers = {
            "X-Session-Agent": self.key.agent_id,
            "X-Session-Hash": self.key.session_hash,
            "X-Attachment-Id": self.key.attachment_id,
            "X-Attachment-Filename": "guide.pdf",
        }
        with patch.object(self.store, "submit", return_value="INDEXING") as submit:
            status, result = request("/v1/index", b"%PDF-1.4", "application/pdf", headers)
        self.assertEqual((status, result), (200, {
            "status": "INDEXING", "attachment_id": self.key.attachment_id,
        }))
        submit.assert_called_once_with(self.key, "guide.pdf", b"%PDF-1.4")

    def test_model_paths_and_extra_identity_fields_are_rejected(self) -> None:
        base = {"agent_id": self.key.agent_id, "session_hash": self.key.session_hash,
                "attachment_id": self.key.attachment_id, "query": "where", "top_k": 3}
        for extra in ({"source_path": "/private/chen"}, {"scope": "family"},
                      {"sessionKey": "forged"}, {"database": "/tmp/other.db"}):
            with self.subTest(extra=extra):
                self.assertEqual(request("/v1/query", json.dumps(base | extra).encode())[0], 400)
        self.assertEqual(request("/v1/query", json.dumps(base | {"top_k": True}).encode())[0], 400)
        self.assertEqual(request("/v1/query", json.dumps(base | {"top_k": 11}).encode())[0], 400)
        self.assertEqual(request("/v1/query", b'{"query":"a","query":"b"}')[0], 400)

    def test_untrusted_header_cannot_select_host_file(self) -> None:
        headers = {
            "X-Session-Agent": self.key.agent_id,
            "X-Session-Hash": self.key.session_hash,
            "X-Attachment-Id": self.key.attachment_id,
            "X-Attachment-Filename": "..%2F..%2Fprivate.pdf",
        }
        with patch.object(self.store, "submit", wraps=self.store.submit) as submit:
            status, _ = request("/v1/index", b"%PDF-1.4", "application/pdf", headers)
        self.assertEqual(status, 400)
        submit.assert_called_once()
        self.assertEqual(list(self.store.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
