"""Temporary-directory tests for the Inbox pipeline; never use NAS paths."""

from __future__ import annotations

import io
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stderr
from pathlib import Path
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

    def importer(self, *, dry_run: bool = False, sleep=None, ingest=None) -> ki.KnowledgeImporter:
        def good_ingest(scope: str) -> dict[str, int]:
            self.calls.append(scope)
            return {"added": 1, "updated": 0, "skipped": 0, "deleted": 0, "failed": 0}

        return ki.KnowledgeImporter(
            self.load_policy(), inbox_root=self.inbox, state_db_path=self.state,
            lock_path=self.lock, settle_seconds=0, dry_run=dry_run,
            ingest_function=good_ingest if ingest is None else ingest,
            sleep_function=(lambda _seconds: None) if sleep is None else sleep,
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


if __name__ == "__main__":
    unittest.main()
