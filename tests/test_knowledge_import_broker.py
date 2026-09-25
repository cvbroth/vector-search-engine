"""Policy, metadata, and local-only Inbox streaming tests."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import knowledge_import_broker as broker


class ImportBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy = broker.ImportBrokerPolicy(1024, MappingProxyType({
            "main": broker.AgentRule("chen", True, True),
            "chen": broker.AgentRule("chen", True, True),
            "liang": broker.AgentRule("liang", False, True),
            "ziling": broker.AgentRule("azl", False, True),
        }))

    def headers(self, agent: str = "main", filename: str = "note.txt", length: int = 4) -> Message:
        value = Message()
        for key, item in {
            "Content-Type": "application/octet-stream", "Content-Length": str(length),
            "X-Knowledge-Agent": agent, "X-Knowledge-Filename": filename,
            "X-Knowledge-Content-Type": "text/plain",
        }.items():
            value[key] = item
        return value

    def test_explicit_agent_mapping_and_denials(self) -> None:
        self.assertEqual(self.policy.resolve("main", "private"), "chen")
        self.assertEqual(self.policy.resolve("chen", "shared"), "chen")
        self.assertEqual(self.policy.resolve("ziling", "shared"), "azl")
        self.assertIsNone(self.policy.resolve("liang", "private"))
        self.assertIsNone(self.policy.resolve("ziling", "private"))
        self.assertIsNone(self.policy.resolve("unknown", "shared"))
        for agent, kind in (("liang", "private"), ("ziling", "private"), ("unknown", "shared")):
            with self.subTest(agent=agent, kind=kind), self.assertRaises(PermissionError):
                broker.validate_headers(self.headers(agent=agent), self.policy, kind)

    def test_policy_schema_and_fixed_mapping(self) -> None:
        policy_file = self.root / "policy.json"
        policy_file.write_text(json.dumps({
            "schema_version": "1.0", "max_file_size_bytes": 1024,
            "agents": {"ziling": {"uploader": "azl", "private": False, "shared": True}},
        }), encoding="utf-8")
        policy_file.chmod(0o600)
        loaded = broker.load_policy(policy_file)
        self.assertEqual(loaded.resolve("ziling", "shared"), "azl")
        policy_file.write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding="utf-8")
        with self.assertRaises(broker.PolicyError):
            broker.load_policy(policy_file)

    def test_filename_traversal_and_unsupported_type_rejected(self) -> None:
        for filename in ("../note.txt", "a/b.txt", "a\\b.txt", ".", "..", "a\x00.txt",
                         ".hidden.txt", "bad.exe", "x" * 181 + ".txt"):
            with self.subTest(filename=filename), self.assertRaises(broker.RequestError):
                broker.validate_filename(filename)
        headers = self.headers(filename="nested%2Fnote.txt")
        with self.assertRaises(broker.RequestError):
            broker.validate_headers(headers, self.policy, "shared")

    def test_client_cannot_supply_destination_path_or_scope(self) -> None:
        for name in ("X-Knowledge-Destination", "X-Knowledge-Path", "X-Knowledge-Scope",
                     "Destination", "Path", "Scope"):
            headers = self.headers()
            headers[name] = "/srv/disks/disk1"
            with self.subTest(name=name), self.assertRaises(broker.RequestError):
                broker.validate_headers(headers, self.policy, "shared")

    def test_length_and_duplicate_headers_rejected(self) -> None:
        for length in (0, 1025):
            with self.subTest(length=length), self.assertRaises(broker.RequestError):
                broker.validate_headers(self.headers(length=length), self.policy, "shared")
        headers = self.headers()
        headers["Content-Length"] = "4"
        with self.assertRaises(broker.RequestError):
            broker.validate_headers(headers, self.policy, "shared")

    def handler(self, agent: str = "main", kind: str = "shared") -> tuple[broker.ImportHandler, list[tuple[int, dict]]]:
        handler = object.__new__(broker.ImportHandler)
        handler.path = f"/v1/{kind}-attachment"
        handler.headers = self.headers(agent=agent)
        handler.rfile = io.BytesIO(b"body")
        handler.connection = SimpleNamespace(settimeout=lambda _timeout: None)
        handler.server = SimpleNamespace(policy=self.policy, inbox_root=self.root)
        responses: list[tuple[int, dict]] = []
        handler._reply = lambda status, value: responses.append((status, value))
        return handler, responses

    def test_http_authorization_and_broker_failure_are_distinct(self) -> None:
        for agent, kind in (("unknown", "shared"), ("liang", "private"), ("ziling", "private")):
            with self.subTest(agent=agent, kind=kind):
                handler, responses = self.handler(agent, kind)
                handler.do_POST()
                self.assertEqual(responses[0][0], 403)
        handler, responses = self.handler()
        with patch.object(broker, "queue_to_inbox", side_effect=OSError("simulated disk error")):
            handler.do_POST()
        self.assertEqual(responses[0][0], 500)

    def test_http_success_means_queued_not_indexed(self) -> None:
        handler, responses = self.handler(agent="ziling")
        with patch.object(broker, "queue_to_inbox", return_value=("kb-test__note.txt", 4, "abcdef123456")) as queued:
            handler.do_POST()
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses[0][1]["status"], "QUEUED")
        self.assertEqual(queued.call_args.args[3], "azl")
        self.assertEqual(queued.call_args.args[2], self.root / "azl" / "shared")
        headers = self.headers()
        headers["Transfer-Encoding"] = "chunked"
        with self.assertRaises(broker.RequestError):
            broker.validate_headers(headers, self.policy, "shared")

    @unittest.skipUnless(sys.platform == "linux", "directory-FD publication and fchown require Linux")
    def test_raw_body_streams_to_private_inbox(self) -> None:
        destination = self.root / "chen" / "private"
        destination.mkdir(parents=True)

        class CheckedStream(io.BytesIO):
            def read(self, count: int = -1) -> bytes:
                self.assert_count(count)
                return super().read(min(count, 2))

            def assert_count(self, count: int) -> None:
                if count < 1 or count > broker.READ_BYTES:
                    raise AssertionError("unbounded read")

        final, size, digest = broker.queue_to_inbox(
            CheckedStream(b"content"), 7, destination, "chen", "note.txt",
            owner=lambda _uploader: (os.getuid(), os.getgid()),
        )
        published = destination / final
        self.assertEqual(published.read_bytes(), b"content")
        self.assertEqual((size, len(digest)), (7, 12))
        self.assertEqual(stat.S_IMODE(published.stat().st_mode), 0o600)
        self.assertEqual(published.stat().st_nlink, 1)
        self.assertEqual(list(destination.glob(".upload-*.tmp")), [])

    @unittest.skipUnless(sys.platform == "linux", "directory-FD publication and fchown require Linux")
    def test_existing_inbox_entry_is_never_overwritten(self) -> None:
        destination = self.root / "chen" / "private"
        destination.mkdir(parents=True)
        collision = "b" * 32
        existing = destination / f"kb-{collision}__note.txt"
        existing.write_bytes(b"original")
        uuids = iter(("a" * 32, collision, "c" * 32))
        with patch.object(broker.uuid, "uuid4", side_effect=lambda: SimpleNamespace(hex=next(uuids))):
            final, _, _ = broker.queue_to_inbox(
                io.BytesIO(b"new!"), 4, destination, "chen", "note.txt",
                owner=lambda _uploader: (os.getuid(), os.getgid()),
            )
        self.assertEqual(existing.read_bytes(), b"original")
        self.assertEqual(final, f"kb-{'c' * 32}__note.txt")
        self.assertEqual((destination / final).read_bytes(), b"new!")

    @unittest.skipUnless(sys.platform == "linux", "directory-FD publication and fchown require Linux")
    def test_incomplete_body_cleans_temporary(self) -> None:
        destination = self.root / "chen" / "private"
        destination.mkdir(parents=True)
        with self.assertRaises(broker.RequestError):
            broker.queue_to_inbox(io.BytesIO(b"x"), 4, destination, "chen", "note.txt",
                                  owner=lambda _uploader: (os.getuid(), os.getgid()))
        self.assertEqual(list(destination.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
