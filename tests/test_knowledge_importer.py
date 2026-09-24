"""Temporary-directory tests for the Inbox pipeline; never use NAS paths."""

from __future__ import annotations

import io
import errno
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docx import Document
from pypdf import PdfWriter

import knowledge_importer as ki


def text_pdf(text: str) -> bytes:
    """Build a tiny extractable-text PDF without a PDF-generation dependency."""
    stream = f"BT /F1 12 Tf 72 100 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, item in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + item + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(data)


def blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


class ImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inbox = self.root / "inbox"
        self.chen = self.root / "source-chen"
        self.family = self.root / "source-family"
        self.chen.mkdir()
        self.family.mkdir()
        self.state = self.root / "state" / "imports.db"
        self.lock = self.root / "runtime" / "import.lock"
        self.lock.parent.mkdir()
        self.policy_path = self.root / "policy.json"
        self.routes = {
            "chen": {
                "private": {"enabled": True, "scope": "chen", "destination": str(self.chen)},
                "shared": {"enabled": True, "scope": "family", "destination": str(self.family)},
            },
            "liang": {
                "private": {"enabled": False},
                "shared": {"enabled": True, "scope": "family", "destination": str(self.family)},
            },
        }
        self.write_policy()
        self.calls: list[str] = []

    def write_policy(self, *, routes: dict | None = None, maximum: int = 100_000) -> None:
        self.policy_path.write_text(json.dumps({
            "schema_version": "1.0", "max_file_size_bytes": maximum,
            "routes": self.routes if routes is None else routes,
        }), encoding="utf-8")

    def load_policy(self) -> ki.ImportPolicy:
        return ki.load_policy(
            self.policy_path, inbox_root=self.inbox,
            allowed_destinations={"chen": self.chen, "family": self.family},
        )

    def write(self, name: str, data: bytes | str, *, user: str = "chen", kind: str = "private") -> Path:
        folder = self.inbox / user / kind
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return path

    def importer(self, *, dry_run: bool = False, sleep=None, ingest=None, publication_primitive=None) -> ki.KnowledgeImporter:
        def good_ingest(scope: str) -> dict[str, int]:
            self.calls.append(scope)
            return {"added": 1, "updated": 0, "skipped": 0, "deleted": 0, "failed": 0}

        return ki.KnowledgeImporter(
            self.load_policy(), inbox_root=self.inbox, state_db_path=self.state,
            lock_path=self.lock, settle_seconds=0, dry_run=dry_run,
            publication_lock_paths={
                "chen": self.root / "scope-chen" / "publish.lock",
                "family": self.root / "scope-family" / "publish.lock",
            },
            publication_primitive=ki.rename_noreplace if publication_primitive is None else publication_primitive,
            ingest_function=good_ingest if ingest is None else ingest,
            sleep_function=(lambda _seconds: None) if sleep is None else sleep,
        )

    def per_user_importer(self, uploader: str, *, dry_run: bool = False, ingest=None,
                          publication_primitive=None) -> ki.KnowledgeImporter:
        def good_ingest(scope: str) -> dict[str, int]:
            self.calls.append(scope)
            return {"failed": 0}

        lock = self.root / "runtime" / uploader / "import.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        return ki.KnowledgeImporter(
            self.load_policy(), uploader=uploader, inbox_root=self.inbox,
            state_db_path=self.root / "state" / uploader / "imports.db",
            lock_path=lock, settle_seconds=0, dry_run=dry_run,
            publication_lock_paths={
                "chen": self.root / "scope-chen" / "publish.lock",
                "family": self.root / "scope-family" / "publish.lock",
            },
            publication_primitive=ki.rename_noreplace if publication_primitive is None else publication_primitive,
            ingest_function=good_ingest if ingest is None else ingest,
            sleep_function=lambda _seconds: None,
        )

    def rows(self) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.state)) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute("SELECT * FROM imports ORDER BY created_at,import_id").fetchall()

    def assert_rejected_preserved(self, name: str, status: str, kind: str = "private") -> Path:
        retained = list((self.inbox / "chen" / "rejected" / kind / status.lower()).glob(f"*__{name}"))
        self.assertEqual(len(retained), 1)
        return retained[0]

    def test_four_supported_formats_and_one_batch_ingest(self) -> None:
        names = ["readme.TXT", "notes.MD", "report.DOCX", "manual.PDF"]
        contents = ["Unique TXT content", "# Heading\nUnique markdown", docx_bytes("Unique DOCX content"), text_pdf("Unique PDF content")]
        for name, data in zip(names, contents):
            self.write(name, data)
        results = self.importer().run()
        self.assertEqual([row.status for row in results], ["INDEXED"] * 4)
        self.assertEqual(self.calls, ["chen"])
        for name in names:
            self.assertTrue((self.chen / name).is_file())
            self.assertFalse((self.inbox / "chen" / "private" / name).exists())
        self.assertEqual([row["status"] for row in self.rows()], ["INDEXED"] * 4)

    def test_rejection_codes_and_retained_files(self) -> None:
        self.write("empty.txt", b"")
        self.write("archive.zip", b"archive payload")
        self.write("scanned.pdf", blank_pdf())
        self.write("broken.pdf", b"this is not pdf")
        self.write("broken.docx", b"not a zip")
        self.write("large.txt", "x" * 50_001)
        self.write_policy(maximum=50_000)
        results = self.importer().run()
        by_name = {row.original_filename: row for row in results}
        expected = {
            "empty.txt": "EMPTY_FILE", "archive.zip": "UNSUPPORTED_TYPE",
            "scanned.pdf": "NO_EXTRACTABLE_TEXT", "broken.pdf": "PARSER_ERROR",
            "broken.docx": "PARSER_ERROR", "large.txt": "TOO_LARGE",
        }
        for name, code in expected.items():
            self.assertEqual((by_name[name].status, by_name[name].error_code), ("REJECTED", code))
            self.assert_rejected_preserved(name, "REJECTED")
        self.assertEqual(len(by_name["archive.zip"].sha256), 64)
        self.assertEqual(by_name["empty.txt"].sha256, hashlib.sha256(b"").hexdigest())
        self.assertIsNone(by_name["large.txt"].sha256)
        self.assertEqual(self.calls, [])

    def test_hidden_temp_files_are_ignored(self) -> None:
        self.write("normal.txt", "normal")
        self.write(".upload.tmp", "hidden")
        self.write("upload.part", "partial")
        results = self.importer().run()
        self.assertEqual([row.original_filename for row in results], ["normal.txt"])
        self.assertTrue((self.inbox / "chen" / "private" / ".upload.tmp").exists())
        self.assertTrue((self.inbox / "chen" / "private" / "upload.part").exists())

    def test_symlink_is_rejected_without_following_it(self) -> None:
        candidate_dir = self.inbox / "chen" / "private"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        try:
            (candidate_dir / "link.txt").symlink_to(self.chen / "absent.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this host")
        result = self.importer().run()[0]
        self.assertEqual(result.error_code, "NOT_REGULAR")
        self.assert_rejected_preserved("link.txt", "REJECTED")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is unavailable on this host")
    def test_fifo_is_rejected_without_opening_it(self) -> None:
        candidate_dir = self.inbox / "chen" / "private"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        os.mkfifo(candidate_dir / "pipe.txt")
        result = self.importer().run()[0]
        self.assertEqual(result.error_code, "NOT_REGULAR")
        self.assert_rejected_preserved("pipe.txt", "REJECTED")

    def test_one_settle_wait_and_unstable_file_skipped(self) -> None:
        changing = self.write("changing.txt", "first")
        stable = self.write("stable.txt", "second")
        sleeps: list[float] = []

        def mutate(seconds: float) -> None:
            sleeps.append(seconds)
            changing.write_text("changed after scan", encoding="utf-8")

        results = self.importer(sleep=mutate).run()
        self.assertEqual(sleeps, [0])
        self.assertEqual([row.original_filename for row in results], [stable.name])
        self.assertTrue(changing.exists())
        self.assertEqual(len(self.rows()), 1)

    def test_duplicate_same_scope_and_same_hash_across_scopes(self) -> None:
        (self.family / "original.txt").write_text("same bytes", encoding="utf-8")
        self.write("copy.txt", "same bytes", kind="shared")
        self.write("private.txt", "same bytes", kind="private")
        results = self.importer().run()
        by_name = {row.original_filename: row for row in results}
        self.assertEqual(by_name["copy.txt"].status, "DUPLICATE")
        self.assert_rejected_preserved("copy.txt", "DUPLICATE", "shared")
        self.assertFalse((self.family / "copy.txt").exists())
        self.assertEqual(by_name["private.txt"].status, "INDEXED")
        self.assertTrue((self.chen / "private.txt").exists())
        self.assertEqual(self.calls, ["chen"])

    def test_filename_conflict_never_overwrites(self) -> None:
        existing = self.chen / "router.txt"
        existing.write_text("original version", encoding="utf-8")
        self.write("router.txt", "different version")
        result = self.importer().run()[0]
        self.assertEqual(result.status, "CONFLICT")
        self.assertEqual(existing.read_text(encoding="utf-8"), "original version")
        self.assertEqual(self.assert_rejected_preserved("router.txt", "CONFLICT").read_text(), "different version")

    def test_disabled_private_and_shared_mapping(self) -> None:
        self.write("disabled.txt", "private test", user="liang", kind="private")
        self.write("shared.txt", "shared test", user="liang", kind="shared")
        results = self.importer().run()
        by_name = {row.original_filename: row for row in results}
        self.assertEqual((by_name["disabled.txt"].status, by_name["disabled.txt"].error_code),
                         ("REJECTED", "ROUTE_DISABLED"))
        self.assertEqual(by_name["shared.txt"].status, "INDEXED")
        self.assertEqual(by_name["shared.txt"].scope, "family")
        self.assertTrue((self.family / "shared.txt").exists())
        self.assertEqual(self.calls, ["family"])
        self.assertEqual(len(list((self.inbox / "liang" / "rejected" / "private" / "rejected").glob("*__disabled.txt"))), 1)

    def test_invalid_policy_fail_closed(self) -> None:
        bad = [
            {"schema_version": "2", "max_file_size_bytes": 100, "routes": self.routes},
            {"schema_version": "1.0", "max_file_size_bytes": 0, "routes": self.routes},
            {"schema_version": "1.0", "max_file_size_bytes": 100, "routes": {"chen": {"private": self.routes["chen"]["private"]}}},
            {"schema_version": "1.0", "max_file_size_bytes": 100, "routes": {"chen": {"private": {"enabled": True}, "shared": {"enabled": False}}}},
            {"schema_version": "1.0", "max_file_size_bytes": 100, "routes": {"chen": {"private": {"enabled": True, "scope": "liang", "destination": str(self.chen)}, "shared": {"enabled": False}}}},
            {"schema_version": "1.0", "max_file_size_bytes": 100, "routes": {"chen": {"private": {"enabled": True, "scope": "chen", "destination": "relative/path"}, "shared": {"enabled": False}}}},
            {"schema_version": "1.0", "max_file_size_bytes": 100, "routes": {"chen": {"private": {"enabled": True, "scope": "chen", "destination": str(self.inbox / "chen")}, "shared": {"enabled": False}}}},
        ]
        self.policy_path.unlink()
        with self.assertRaises(ki.ImportPolicyError):
            self.load_policy()
        for value in bad:
            self.policy_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ki.ImportPolicyError):
                self.load_policy()
        for raw in ('{broken', '{"schema_version":"1.0","schema_version":"1.0"}'):
            self.policy_path.write_text(raw, encoding="utf-8")
            with self.assertRaises(ki.ImportPolicyError):
                self.load_policy()

    def test_ingest_failure_keeps_source_and_retry_succeeds(self) -> None:
        self.write("retry.txt", "content for retry")

        def fail(_scope: str) -> dict[str, int]:
            return {"failed": 1}

        first = self.importer(ingest=fail).run()[0]
        self.assertEqual(first.status, "INDEX_ERROR")
        self.assertTrue((self.chen / "retry.txt").exists())
        self.assertEqual(self.rows()[0]["status"], "INDEX_ERROR")
        second = self.importer().run()
        self.assertEqual(second, [])
        self.assertEqual(self.calls, ["chen"])
        self.assertEqual(self.rows()[0]["status"], "INDEXED")

    def test_ingest_exception_marks_index_error_without_deleting_source(self) -> None:
        self.write("exception.txt", "an indexable document")

        def fail(_scope: str) -> dict[str, int]:
            raise RuntimeError("embedding service unavailable")

        with self.assertLogs(ki.LOGGER, level="ERROR") as logs:
            result = self.importer(ingest=fail).run()[0]
        self.assertEqual(result.status, "INDEX_ERROR")
        self.assertTrue((self.chen / "exception.txt").exists())
        self.assertNotIn("an indexable document", "\n".join(logs.output))

    def test_two_scope_batches_and_idempotent_rerun(self) -> None:
        for number in range(3):
            self.write(f"private{number}.txt", f"private content {number}")
            self.write(f"shared{number}.txt", f"shared content {number}", kind="shared")
        first = self.importer().run()
        self.assertEqual(len(first), 6)
        self.assertEqual(self.calls, ["chen", "family"])
        self.assertEqual(self.importer().run(), [])
        self.assertEqual(self.calls, ["chen", "family"])
        self.assertEqual(len(self.rows()), 6)

    def test_dry_run_reports_all_decisions_without_writes(self) -> None:
        (self.chen / "duplicate.txt").write_text("same", encoding="utf-8")
        (self.chen / "conflict.txt").write_text("old", encoding="utf-8")
        for name, content in [
            ("new.txt", "new"), ("duplicate-copy.txt", "same"),
            ("conflict.txt", "different"), ("bad.zip", "unsupported"),
        ]:
            self.write(name, content)
        results = self.importer(dry_run=True).run()
        self.assertEqual({row.status for row in results}, {
            "WOULD_IMPORT", "WOULD_DUPLICATE", "WOULD_CONFLICT", "WOULD_REJECTED",
        })
        self.assertFalse(self.state.exists())
        self.assertFalse(self.lock.exists())
        self.assertFalse((self.chen / "new.txt").exists())
        self.assertFalse((self.inbox / "chen" / "rejected").exists())
        self.assertTrue((self.inbox / "chen" / "private" / "new.txt").exists())
        self.assertEqual(self.calls, [])

    def test_metadata_db_and_audit_exclude_document_body(self) -> None:
        secret = "PRIVATE BODY TOKEN 8675309"
        self.write("secret.txt", secret)
        with self.assertLogs(ki.LOGGER, level="INFO") as captured:
            self.importer().run()
        raw = self.state.read_bytes()
        self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(secret, "\n".join(captured.output))
        row = self.rows()[0]
        for field in (
            "import_id", "uploader", "access_kind", "scope", "original_filename",
            "final_filename", "source_inbox_path", "destination_path", "sha256",
            "size_bytes", "status", "error_code", "error_message", "created_at",
            "updated_at", "indexed_at",
        ):
            self.assertIn(field, row.keys())

    def test_malformed_pdf_diagnostics_do_not_log_header_bytes(self) -> None:
        secret_header = "SECRET_HEADER_BYTES"
        self.write("corrupt.pdf", secret_header.encode() + b" broken PDF")
        diagnostic = io.StringIO()
        with redirect_stderr(diagnostic):
            result = self.importer().run()[0]
        self.assertEqual(result.error_code, "PARSER_ERROR")
        self.assertNotIn(secret_header, diagnostic.getvalue())
        self.assertNotIn(secret_header, self.state.read_bytes().decode("utf-8", errors="ignore"))

    def test_rejected_and_duplicate_are_not_rescanned(self) -> None:
        (self.chen / "known.txt").write_text("identical", encoding="utf-8")
        self.write("copy.txt", "identical")
        self.write("bad.zip", "not supported")
        first = self.importer().run()
        self.assertEqual({row.status for row in first}, {"DUPLICATE", "REJECTED"})
        self.assertEqual(self.importer().run(), [])
        self.assertEqual(len(self.rows()), 2)
        self.assert_rejected_preserved("copy.txt", "DUPLICATE")
        self.assert_rejected_preserved("bad.zip", "REJECTED")

    def test_second_importer_cannot_take_lock(self) -> None:
        with ki.ImporterLock(self.lock):
            with self.assertRaises(ki.ImporterBusy):
                with ki.ImporterLock(self.lock):
                    pass

    def test_ingest_cli_keeps_original_exit_semantics(self) -> None:
        import ingest

        with patch.object(ingest, "configure_logging"), patch.object(ingest, "ingest_scope", return_value={"failed": 0}):
            with patch("sys.argv", ["ingest.py", "--scope", "chen"]):
                self.assertEqual(ingest.main(), 0)
        with patch.object(ingest, "configure_logging"), patch.object(ingest, "ingest_scope", return_value={"failed": 1}):
            with patch("sys.argv", ["ingest.py", "--scope", "family"]):
                self.assertEqual(ingest.main(), 1)

    def test_importer_cli_passes_policy_once_dry_run_and_settle(self) -> None:
        with patch.object(ki, "load_policy", return_value=self.load_policy()) as load:
            with patch.object(ki, "KnowledgeImporter") as importer_type:
                importer_type.return_value.run.return_value = []
                self.assertEqual(ki.main([
                    "--policy", str(self.policy_path), "--once", "--dry-run",
                    "--settle-seconds", "0",
                ]), 0)
                load.assert_called_once_with(self.policy_path)
                self.assertTrue(importer_type.call_args.kwargs["dry_run"])
                self.assertEqual(importer_type.call_args.kwargs["settle_seconds"], 0)

    def test_selected_uploader_scans_only_own_inbox(self) -> None:
        own = self.write("chen.txt", "chen content")
        other = self.write("liang.txt", "liang content", user="liang", kind="shared")
        results = self.per_user_importer("chen").run()
        self.assertEqual([record.original_filename for record in results], [own.name])
        self.assertTrue(other.exists())
        self.assertEqual(self.calls, ["chen"])
        self.assertFalse((self.root / "state" / "liang").exists())

    def test_selected_uploader_never_traverses_other_policy_user(self) -> None:
        own = self.write("only.txt", "own content")
        original = ki.reject_symlink_components

        def reject_other(path: Path) -> None:
            if "liang" in path.parts:
                raise AssertionError("other user's path must not be inspected")
            original(path)

        importer = self.per_user_importer("chen")
        with patch.object(ki, "reject_symlink_components", side_effect=reject_other):
            self.assertEqual([row.original_filename for row in importer.run()], [own.name])

    def test_unknown_uploader_fails_before_scanning_or_state_creation(self) -> None:
        with self.assertRaises(ki.ImportPolicyError):
            self.per_user_importer("azl")
        self.assertFalse((self.root / "state" / "azl").exists())

    def test_invalid_uploader_fails_closed(self) -> None:
        for name in ("../liang", "bad/name", "", "a" * 129):
            with self.subTest(name=name), self.assertRaises(ki.ImportPolicyError):
                ki.KnowledgeImporter(self.load_policy(), uploader=name)

    def test_chen_default_state_and_lock_paths(self) -> None:
        importer = ki.KnowledgeImporter(self.load_policy(), uploader="chen", dry_run=True)
        self.assertEqual(importer.state_db_path, Path("/var/lib/knowledge-import/chen/imports.db"))
        self.assertEqual(importer.lock_path, Path("/run/knowledge-import/chen/import.lock"))

    def test_liang_default_state_and_lock_paths(self) -> None:
        importer = ki.KnowledgeImporter(self.load_policy(), uploader="liang", dry_run=True)
        self.assertEqual(importer.state_db_path, Path("/var/lib/knowledge-import/liang/imports.db"))
        self.assertEqual(importer.lock_path, Path("/run/knowledge-import/liang/import.lock"))

    def test_selected_uploaders_write_independent_state_databases(self) -> None:
        self.write("chen.txt", "chen content")
        self.write("liang.txt", "liang content", user="liang", kind="shared")
        self.per_user_importer("chen").run()
        self.per_user_importer("liang").run()
        for user in ("chen", "liang"):
            path = self.root / "state" / user / "imports.db"
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("SELECT uploader FROM imports").fetchall(), [(user,)])

    def test_pending_index_error_remains_with_own_uploader(self) -> None:
        self.write("retry.txt", "needs retry", user="chen")
        self.per_user_importer("chen", ingest=lambda _scope: {"failed": 1}).run()
        self.per_user_importer("liang").run()
        self.assertEqual(self.calls, [])
        with closing(sqlite3.connect(self.root / "state" / "chen" / "imports.db")) as connection:
            self.assertEqual(connection.execute("SELECT status FROM imports").fetchone(), ("INDEX_ERROR",))
        self.per_user_importer("chen").run()
        self.assertEqual(self.calls, ["chen"])

    def test_pending_filter_rejects_other_user_even_with_injected_shared_db(self) -> None:
        self.write("retry.txt", "retry from chen")
        self.per_user_importer("chen", ingest=lambda _scope: {"failed": 1}).run()
        shared = self.root / "state" / "chen" / "imports.db"
        liang = self.per_user_importer("liang")
        liang.state_db_path = shared  # Deliberate Python API misuse; pending still stays scoped.
        liang.run()
        self.assertEqual(self.calls, [])

    def test_different_uploader_locks_do_not_block_each_other(self) -> None:
        chen = self.per_user_importer("chen")
        liang = self.per_user_importer("liang")
        with ki.ImporterLock(chen.lock_path):
            with ki.ImporterLock(liang.lock_path):
                self.assertNotEqual(chen.lock_path, liang.lock_path)

    def test_same_uploader_lock_remains_exclusive(self) -> None:
        first = self.per_user_importer("chen")
        second = self.per_user_importer("chen")
        with ki.ImporterLock(first.lock_path):
            with self.assertRaises(ki.ImporterBusy):
                second.run()

    def test_selected_uploader_dry_run_does_not_touch_state_or_other_inbox(self) -> None:
        own = self.write("own.txt", "own content")
        other = self.write("other.txt", "other content", user="liang", kind="shared")
        result = self.per_user_importer("chen", dry_run=True).run()
        self.assertEqual([row.status for row in result], ["WOULD_IMPORT"])
        self.assertTrue(own.exists())
        self.assertTrue(other.exists())
        self.assertFalse((self.root / "state" / "chen").exists())

    def test_cli_forwards_uploader_without_arbitrary_path_flags(self) -> None:
        with patch.object(ki, "load_policy", return_value=self.load_policy()):
            with patch.object(ki, "KnowledgeImporter") as importer_type:
                importer_type.return_value.run.return_value = []
                self.assertEqual(ki.main(["--uploader", "chen", "--once"]), 0)
                self.assertEqual(importer_type.call_args.kwargs["uploader"], "chen")

    def test_cli_unknown_uploader_aborts_without_running(self) -> None:
        with patch.object(ki, "load_policy", return_value=self.load_policy()):
            with patch.object(ki.KnowledgeImporter, "run") as run:
                self.assertEqual(ki.main(["--uploader", "nobody"]), 1)
                run.assert_not_called()

    def test_no_uploader_keeps_legacy_single_instance_defaults(self) -> None:
        importer = ki.KnowledgeImporter(self.load_policy(), dry_run=True)
        self.assertEqual(importer.state_db_path, ki.STATE_DB_PATH)
        self.assertEqual(importer.lock_path, ki.LOCK_PATH)

    @unittest.skipUnless(os.name == "posix", "Unix permission bits are required")
    def test_publish_sets_group_read_under_restrictive_umask(self) -> None:
        own_inbox = self.write("private.txt", "private contents")
        shared_inbox = self.write("shared.txt", "shared contents", kind="shared")
        os.chmod(own_inbox.parent, 0o700)
        previous = os.umask(0o077)
        try:
            results = self.importer().run()
        finally:
            os.umask(previous)
        self.assertEqual([item.status for item in results], ["INDEXED", "INDEXED"])
        self.assertEqual(stat.S_IMODE((self.chen / "private.txt").stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((self.family / "shared.txt").stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(own_inbox.parent.stat().st_mode), 0o700)

    def test_duplicate_and_conflict_unchanged_with_publication_lock(self) -> None:
        (self.family / "known.txt").write_text("same", encoding="utf-8")
        self.write("copy.txt", "same", kind="shared")
        self.write("known.txt", "different", kind="shared")
        results = self.importer().run()
        self.assertEqual({item.original_filename: item.status for item in results}, {
            "copy.txt": "DUPLICATE", "known.txt": "CONFLICT",
        })
        self.assertEqual((self.family / "known.txt").read_text(encoding="utf-8"), "same")

    def test_simultaneous_uploaders_cannot_publish_same_sha_twice(self) -> None:
        self.write("chen.txt", "same shared bytes", kind="shared")
        self.write("liang.txt", "same shared bytes", user="liang", kind="shared")
        original_publish = ki.KnowledgeImporter._publish
        first_publishing = threading.Event()
        release_first = threading.Event()
        outcomes: list[ki.ImportRecord] = []
        errors: list[Exception] = []

        def slow_publish(importer, *args, **kwargs):
            if args[0].uploader == "chen":
                first_publishing.set()
                if not release_first.wait(5):
                    raise TimeoutError("test publication wait timed out")
            return original_publish(importer, *args, **kwargs)

        def run_user(user: str) -> None:
            try:
                outcomes.extend(self.per_user_importer(user).run())
            except Exception as exc:
                errors.append(exc)

        with patch.object(ki.KnowledgeImporter, "_publish", slow_publish):
            chen_thread = threading.Thread(target=run_user, args=("chen",))
            liang_thread = threading.Thread(target=run_user, args=("liang",))
            chen_thread.start()
            try:
                self.assertTrue(first_publishing.wait(5))
                liang_thread.start()
                time.sleep(0.1)
            finally:
                release_first.set()
                chen_thread.join(5)
                if liang_thread.ident is not None:
                    liang_thread.join(5)
        self.assertFalse(chen_thread.is_alive() or liang_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(item.status for item in outcomes), ["DUPLICATE", "INDEXED"])
        self.assertEqual(len(list(self.family.glob("*.txt"))), 1)

    def test_publication_lock_released_before_ingest(self) -> None:
        self.write("shared.txt", "shared bytes", kind="shared")
        original_lock = ki.scope_file_lock
        active = 0

        @contextmanager
        def tracked_lock(path: Path):
            nonlocal active
            with original_lock(path):
                active += 1
                try:
                    yield
                finally:
                    active -= 1

        def ingest_after_publication(_scope: str) -> dict[str, int]:
            self.assertEqual(active, 0)
            return {"failed": 0}

        with patch.object(ki, "scope_file_lock", tracked_lock):
            self.assertEqual(self.importer(ingest=ingest_after_publication).run()[0].status, "INDEXED")

    def test_publication_is_single_linked_and_cleans_temporary(self) -> None:
        self.write("new.txt", "published contents")
        result = self.importer().run()[0]
        self.assertEqual(result.status, "INDEXED")
        self.assertEqual((self.chen / "new.txt").stat().st_nlink, 1)
        self.assertEqual(list(self.chen.glob(".import-*.tmp")), [])

    def test_publication_never_uses_hard_link(self) -> None:
        self.write("new.txt", "published contents")
        with patch.object(ki.os, "link", side_effect=AssertionError("hard-link publication forbidden")):
            self.assertEqual(self.importer().run()[0].status, "INDEXED")

    def test_supported_primitive_does_not_use_fallback(self) -> None:
        self.write("new.txt", "published contents")
        with patch.object(ki.KnowledgeImporter, "_fallback_rename_while_locked",
                          side_effect=AssertionError("unexpected fallback")):
            self.assertEqual(self.importer().run()[0].status, "INDEXED")

    def test_private_publish_rejects_call_without_scope_lock(self) -> None:
        self.write("new.txt", "published contents")
        importer = self.importer()
        candidate = importer._scan()[0]
        with self.assertRaisesRegex(RuntimeError, "publish lock"):
            importer._publish(candidate, b"published contents", "unused", self.chen, "test",
                              publication_locked=False)
        self.assertFalse((self.chen / "new.txt").exists())

    def test_mergerfs_einval_uses_locked_fallback_with_single_link(self) -> None:
        self.write("new.txt", "published contents")

        def unsupported(_temporary: Path, final: Path) -> None:
            raise ki.UnsupportedPublicationError(errno.EINVAL, "simulated mergerfs EINVAL", str(final))

        with patch.object(ki.os, "link", side_effect=AssertionError("hard link forbidden")):
            result = self.importer(publication_primitive=unsupported).run()[0]
        final = self.chen / "new.txt"
        self.assertEqual(result.status, "INDEXED")
        self.assertEqual(final.read_text(encoding="utf-8"), "published contents")
        self.assertEqual(final.stat().st_nlink, 1)
        self.assertEqual(list(self.chen.glob(".import-*.tmp")), [])

    def test_fallback_rechecks_existing_final_without_overwriting(self) -> None:
        self.write("race.txt", "incoming content")

        def unsupported(_temporary: Path, final: Path) -> None:
            final.write_text("other writer", encoding="utf-8")
            raise ki.UnsupportedPublicationError(errno.EINVAL, "unsupported", str(final))

        result = self.importer(publication_primitive=unsupported).run()[0]
        self.assertEqual((result.status, result.error_code), ("CONFLICT", "CONFLICT"))
        self.assertEqual((self.chen / "race.txt").read_text(encoding="utf-8"), "other writer")
        self.assertEqual(list(self.chen.glob(".import-*.tmp")), [])

    def test_fallback_rejects_symlink_final(self) -> None:
        target = self.root / "outside.txt"
        target.write_text("outside content", encoding="utf-8")
        probe = self.root / "symlink-probe"
        try:
            probe.symlink_to(target)
            probe.unlink()
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        self.write("race.txt", "incoming content")

        def unsupported(_temporary: Path, final: Path) -> None:
            final.symlink_to(target)
            raise ki.UnsupportedPublicationError(errno.EINVAL, "unsupported", str(final))

        result = self.importer(publication_primitive=unsupported).run()[0]
        self.assertEqual((result.status, result.error_code), ("CONFLICT", "CONFLICT"))
        self.assertTrue((self.chen / "race.txt").is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "outside content")

    def test_real_io_errors_never_use_fallback(self) -> None:
        for code in (errno.EIO, errno.EPERM, errno.EROFS, errno.ENOSPC):
            with self.subTest(code=code):
                name = f"error-{code}.txt"
                self.write(name, "incoming content")

                def failing_rename(_temporary: Path, _final: Path) -> None:
                    raise OSError(code, "simulated I/O failure")

                with patch.object(ki.KnowledgeImporter, "_fallback_rename_while_locked",
                                  side_effect=AssertionError("I/O failure must not fall back")):
                    result = self.importer(publication_primitive=failing_rename).run()[0]
                self.assertEqual((result.status, result.error_code), ("REJECTED", "PUBLISH_ERROR"))
                self.assertFalse((self.chen / name).exists())

    def test_eexist_race_maps_to_conflict_without_overwriting(self) -> None:
        self.write("race.txt", "incoming content")

        def competing_writer(_temporary: Path, final: Path) -> None:
            final.write_text("other writer", encoding="utf-8")
            raise FileExistsError(errno.EEXIST, "already exists", str(final))

        result = self.importer(publication_primitive=competing_writer).run()[0]
        self.assertEqual((result.status, result.error_code), ("CONFLICT", "CONFLICT"))
        self.assertEqual((self.chen / "race.txt").read_text(encoding="utf-8"), "other writer")
        self.assertEqual(list(self.chen.glob(".import-*.tmp")), [])

    def test_eexist_race_maps_to_duplicate_for_same_content(self) -> None:
        self.write("race.txt", "incoming content")

        def competing_writer(temporary: Path, final: Path) -> None:
            final.write_bytes(temporary.read_bytes())
            raise FileExistsError(errno.EEXIST, "already exists", str(final))

        result = self.importer(publication_primitive=competing_writer).run()[0]
        self.assertEqual((result.status, result.error_code), ("DUPLICATE", "DUPLICATE"))
        self.assertEqual(list(self.chen.glob(".import-*.tmp")), [])

    def test_rename_failure_keeps_competing_final_and_cleans_own_temp(self) -> None:
        self.write("race.txt", "incoming content")

        def failing_rename(_temporary: Path, final: Path) -> None:
            final.write_text("other writer", encoding="utf-8")
            raise OSError(errno.EIO, "simulated rename failure")

        result = self.importer(publication_primitive=failing_rename).run()[0]
        self.assertEqual((result.status, result.error_code), ("REJECTED", "PUBLISH_ERROR"))
        self.assertEqual((self.chen / "race.txt").read_text(encoding="utf-8"), "other writer")
        self.assertEqual(list(self.chen.glob(".import-*.tmp")), [])

    def test_unsupported_rename_falls_back_only_inside_scope_lock(self) -> None:
        self.write("new.txt", "incoming content")
        active = False
        original_lock = ki.scope_file_lock

        @contextmanager
        def tracked_lock(path: Path):
            nonlocal active
            with original_lock(path):
                active = True
                try:
                    yield
                finally:
                    active = False

        def unsupported(_temporary: Path, final: Path) -> None:
            self.assertTrue(active)
            raise ki.UnsupportedPublicationError(errno.EOPNOTSUPP, "unsupported", str(final))

        with patch.object(ki, "scope_file_lock", tracked_lock):
            self.assertEqual(self.importer(publication_primitive=unsupported).run()[0].status, "INDEXED")
        self.assertFalse(active)
        self.assertEqual((self.chen / "new.txt").read_text(encoding="utf-8"), "incoming content")

    def test_concurrent_fallback_publishers_serialize_same_scope(self) -> None:
        self.write("chen.txt", "same shared bytes", kind="shared")
        self.write("liang.txt", "same shared bytes", user="liang", kind="shared")
        entered = threading.Event()
        release = threading.Event()
        outcomes: list[ki.ImportRecord] = []
        errors: list[Exception] = []

        def unsupported(_temporary: Path, final: Path) -> None:
            if final.name == "chen.txt":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test publication wait timed out")
            raise ki.UnsupportedPublicationError(errno.EINVAL, "simulated mergerfs EINVAL", str(final))

        def run_user(user: str) -> None:
            try:
                outcomes.extend(self.per_user_importer(user, publication_primitive=unsupported).run())
            except Exception as exc:
                errors.append(exc)

        chen_thread = threading.Thread(target=run_user, args=("chen",))
        liang_thread = threading.Thread(target=run_user, args=("liang",))
        chen_thread.start()
        try:
            self.assertTrue(entered.wait(5))
            liang_thread.start()
            time.sleep(0.1)
        finally:
            release.set()
            chen_thread.join(5)
            if liang_thread.ident is not None:
                liang_thread.join(5)
        self.assertFalse(chen_thread.is_alive() or liang_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(item.status for item in outcomes), ["DUPLICATE", "INDEXED"])
        published = list(self.family.glob("*.txt"))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].stat().st_nlink, 1)

    @unittest.skipUnless(sys.platform == "linux", "read_snapshot uses Linux directory-FD flags")
    def test_ingest_reads_newly_published_single_link_file(self) -> None:
        import ingest

        self.write("new.txt", "immediately readable")
        self.assertEqual(self.importer().run()[0].status, "INDEXED")
        snapshot = ingest.read_snapshot(self.chen / "new.txt", SimpleNamespace(source_dir=self.chen))
        self.assertEqual(snapshot.data, b"immediately readable")

    @unittest.skipUnless(sys.platform == "linux", "read_snapshot uses Linux directory-FD flags")
    def test_ingest_reads_fallback_published_file_immediately(self) -> None:
        import ingest

        self.write("new.txt", "immediately readable")

        def unsupported(_temporary: Path, final: Path) -> None:
            raise ki.UnsupportedPublicationError(errno.EINVAL, "simulated mergerfs EINVAL", str(final))

        self.assertEqual(self.importer(publication_primitive=unsupported).run()[0].status, "INDEXED")
        snapshot = ingest.read_snapshot(self.chen / "new.txt", SimpleNamespace(source_dir=self.chen))
        self.assertEqual(snapshot.data, b"immediately readable")


if __name__ == "__main__":
    unittest.main()
