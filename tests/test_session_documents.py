"""Real SQLite/FTS/vec logic with deterministic local embeddings, no NAS access."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import KNOWLEDGE_SCOPES
from parsers import ParseError, ParsedBlock
from session_documents import (
    SessionDocumentError, SessionDocumentKey, SessionDocumentLimits,
    SessionDocumentStore, session_hash,
)


def _vectors(texts: list[str]) -> list[list[float]]:
    result = []
    for text in texts:
        # The tail marker is deliberately absent from the first 499 pages.
        if "TAIL_MARKER" in text or "异化" in text:
            result.append([0.0, 1.0] + [0.0] * 766)
        else:
            result.append([1.0, 0.0] + [0.0] * 766)
    return result


class SessionDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = [1_700_000_000.0]
        self.store = SessionDocumentStore(Path(self.temp.name) / "sessions", now=lambda: self.clock[0])
        self.chen_a = SessionDocumentKey("chen", session_hash("agent:chen:sessionA"), "a" * 32)

    def _index(self, key: SessionDocumentKey, blocks: list[ParsedBlock], name: str = "book.pdf") -> None:
        self.assertEqual(self.store.submit(key, name, b"%PDF-1.4 test", launch=False), "INDEXING")
        with patch("session_documents.parse_document", return_value=blocks), \
             patch("session_documents.embed_texts", side_effect=_vectors):
            self.store.index_once(key)

    def _query(self, key: SessionDocumentKey, query: str = "异化", top_k: int = 3) -> dict:
        with patch("search.embed_texts", side_effect=_vectors):
            return self.store.query(key, query, top_k)

    def test_lazy_index_then_reuse_for_followup(self) -> None:
        blocks = [ParsedBlock("前几页的概览", 1), ParsedBlock("异化概念的定义", 300)]
        self.assertEqual(self.store.submit(self.chen_a, "book.pdf", b"%PDF test", launch=False), "INDEXING")
        self.assertEqual(self.store.query(self.chen_a, "异化", 3)["status"], "INDEXING")
        with patch("session_documents.parse_document", return_value=blocks) as parser, \
             patch("session_documents.embed_texts", side_effect=_vectors):
            self.store.index_once(self.chen_a)
        self.assertEqual(parser.call_count, 1)
        first = self._query(self.chen_a)
        second = self._query(self.chen_a, "异化和什么有关")
        self.assertEqual(first["status"], "READY")
        self.assertEqual(first["evidence"][0]["page"], 300)
        self.assertEqual(second["status"], "READY")
        self.assertEqual(parser.call_count, 1)

    def test_five_hundred_logical_pages_retrieve_tail(self) -> None:
        blocks = [ParsedBlock(
            f"第{number}页 TAIL_MARKER 异化定义" if number == 480 else f"第{number}页普通资料",
            number,
        ) for number in range(1, 501)]
        self._index(self.chen_a, blocks)
        hit = self._query(self.chen_a, "异化", 1)
        self.assertEqual(hit["status"], "READY")
        self.assertEqual(hit["evidence"][0]["page"], 480)

    def test_attachment_session_and_agent_boundaries(self) -> None:
        second = SessionDocumentKey("chen", self.chen_a.session_hash, "b" * 32)
        another_session = SessionDocumentKey("chen", session_hash("agent:chen:sessionB"), self.chen_a.attachment_id)
        liang = SessionDocumentKey("liang", self.chen_a.session_hash, self.chen_a.attachment_id)
        ziling = SessionDocumentKey("ziling", self.chen_a.session_hash, self.chen_a.attachment_id)
        self._index(self.chen_a, [ParsedBlock("异化属于A", 10)], "A.pdf")
        self._index(second, [ParsedBlock("另一份资料属于B", 20)], "B.pdf")
        self.assertEqual(self._query(self.chen_a)["filename"], "A.pdf")
        self.assertEqual(self._query(second, "另一份资料")["filename"], "B.pdf")
        for outsider in (another_session, liang, ziling):
            self.assertEqual(self._query(outsider)["status"], "NOT_FOUND")
        self.assertEqual(self.store.list_documents("liang", self.chen_a.session_hash), [])
        self.assertEqual(self.store.list_documents("ziling", self.chen_a.session_hash), [])

    def test_ttl_and_idempotent_cleanup(self) -> None:
        self._index(self.chen_a, [ParsedBlock("异化", 5)])
        self.clock[0] += 72 * 3600
        self.assertEqual(self._query(self.chen_a)["status"], "NOT_FOUND")
        self.assertEqual(self.store.cleanup(), 1)
        self.assertEqual(self.store.cleanup(), 0)

    def test_failed_and_corrupt_indexes_fail_closed(self) -> None:
        self.store.submit(self.chen_a, "scan.pdf", b"%PDF test", launch=False)
        with patch("session_documents.parse_document", side_effect=ParseError("no text")):
            self.store.index_once(self.chen_a)
        self.assertEqual(self._query(self.chen_a)["status"], "NO_SEARCHABLE_TEXT")
        self.assertFalse((self.store._directory(self.chen_a) / "source.pdf").exists())
        state = self.store._directory(self.chen_a) / "state.json"
        state.write_text("{broken", encoding="utf-8")
        self.assertEqual(self._query(self.chen_a)["status"], "FAILED")
        self.assertEqual(self.store.list_documents("chen", self.chen_a.session_hash), [])
        self.assertEqual(self.store.cleanup(), 1)

    def test_oversize_limits_and_no_persistent_scope(self) -> None:
        limits = SessionDocumentLimits(max_attachment_bytes=10, max_session_source_bytes=20)
        small_store = SessionDocumentStore(Path(self.temp.name) / "small", limits=limits)
        with self.assertRaises(SessionDocumentError):
            small_store.submit(self.chen_a, "huge.pdf", b"x" * 11, launch=False)
        with self.assertRaises(SessionDocumentError):
            self.store.submit(self.chen_a, "../private.pdf", b"%PDF", launch=False)
        with self.assertRaises(SessionDocumentError):
            SessionDocumentKey("liang", "../chen", self.chen_a.attachment_id)
        with self.assertRaises(SessionDocumentError):
            self._query(self.chen_a, top_k=11)
        self.assertNotIn("session-documents", KNOWLEDGE_SCOPES)
        self.assertTrue(all("session-documents" not in str(scope.database_path)
                            for scope in KNOWLEDGE_SCOPES.values()))

    def test_interrupted_job_fails_closed_and_cleanup_recovers(self) -> None:
        self.store.submit(self.chen_a, "book.pdf", b"%PDF test", launch=False)
        self.assertEqual(self.store.cleanup(), 0)
        self.assertEqual(self._query(self.chen_a)["status"], "FAILED")
        self.assertFalse((self.store._directory(self.chen_a) / "source.pdf").exists())

    def test_index_timeout_terminates_worker_and_discards_snapshot(self) -> None:
        self.store.submit(self.chen_a, "book.pdf", b"%PDF test", launch=False)

        class HungProcess:
            terminated = False

            def join(self, _seconds: int) -> None:
                pass

            def is_alive(self) -> bool:
                return not self.terminated

            def terminate(self) -> None:
                self.terminated = True

        worker = HungProcess()
        self.store._jobs[self.chen_a] = worker  # Simulate an active background job.
        self.store._watch(self.chen_a, worker)
        self.assertTrue(worker.terminated)
        self.assertEqual(self._query(self.chen_a)["status"], "FAILED")
        self.assertFalse((self.store._directory(self.chen_a) / "source.pdf").exists())

    @unittest.skipUnless(os.name == "posix", "Unix permission bits are required")
    def test_identity_directories_are_private_even_with_permissive_umask(self) -> None:
        old_umask = os.umask(0o022)
        try:
            self.store.submit(self.chen_a, "book.pdf", b"%PDF test", launch=False)
        finally:
            os.umask(old_umask)
        directory = self.store._directory(self.chen_a)
        for component in (directory, directory.parent, directory.parent.parent):
            self.assertEqual(component.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
