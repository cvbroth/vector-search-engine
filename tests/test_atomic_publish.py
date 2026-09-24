"""No-clobber publication tests; Linux-only cases use temporary local files."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import atomic_publish as publication


class AtomicPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_same_directory_is_required(self) -> None:
        with self.assertRaises(ValueError):
            publication.rename_noreplace(self.root / "a.tmp", self.root / "other" / "a.txt")

    @unittest.skipUnless(sys.platform == "linux" or os.name == "nt", "no safe native rename primitive")
    def test_two_concurrent_same_name_renames_cannot_overwrite(self) -> None:
        final = self.root / "result.txt"
        temporary = [self.root / "first.tmp", self.root / "second.tmp"]
        for path, body in zip(temporary, ("first", "second")):
            path.write_text(body, encoding="utf-8")
        start = threading.Barrier(3)
        successes: list[str] = []
        failures: list[Exception] = []

        def publish(path: Path) -> None:
            try:
                start.wait(5)
                publication.rename_noreplace(path, final)
                successes.append(path.name)
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=publish, args=(path,)) for path in temporary]
        for thread in threads:
            thread.start()
        start.wait(5)
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], FileExistsError)
        self.assertEqual(final.read_text(encoding="utf-8"), successes[0].split(".")[0])
        self.assertEqual(final.stat().st_nlink, 1)

    def test_linux_wrapper_maps_eexist_and_unsupported_errno(self) -> None:
        temporary = self.root / "source.tmp"
        final = self.root / "final.txt"

        class FakeCall:
            def __init__(self, code: int) -> None:
                self.code = code
                self.arguments: tuple[object, ...] = ()

            def __call__(self, *arguments: object) -> int:
                self.arguments = arguments
                ctypes.set_errno(self.code)
                return -1

        for code, expected in (
            (errno.EEXIST, FileExistsError),
            (errno.ENOSYS, publication.UnsupportedPublicationError),
            (errno.EINVAL, publication.UnsupportedPublicationError),
            (errno.EOPNOTSUPP, publication.UnsupportedPublicationError),
            (errno.EXDEV, publication.UnsupportedPublicationError),
        ):
            with self.subTest(code=code):
                call = FakeCall(code)
                with patch.object(publication.ctypes, "CDLL", return_value=SimpleNamespace(renameat2=call)):
                    with self.assertRaises(expected):
                        publication._linux_rename_noreplace(temporary, final)
                self.assertEqual(call.arguments[0::2], (publication.AT_FDCWD, publication.AT_FDCWD, publication.RENAME_NOREPLACE))

    def test_missing_libc_symbol_is_explicitly_unsupported(self) -> None:
        with patch.object(publication.ctypes, "CDLL", return_value=object()):
            with self.assertRaises(publication.UnsupportedPublicationError):
                publication._linux_rename_noreplace(self.root / "source.tmp", self.root / "final.txt")

    def test_linux_dispatch_never_falls_back_to_unsafe_publication(self) -> None:
        error = publication.UnsupportedPublicationError(errno.ENOSYS, "unsupported")
        with patch.object(publication, "sys", SimpleNamespace(platform="linux")):
            with patch.object(publication, "_linux_rename_noreplace", side_effect=error):
                with patch.object(publication.os, "rename", side_effect=AssertionError("unsafe fallback")):
                    with patch.object(publication.os, "link", side_effect=AssertionError("hard-link fallback")):
                        with self.assertRaises(publication.UnsupportedPublicationError):
                            publication.rename_noreplace(self.root / "source.tmp", self.root / "final.txt")

    @unittest.skipUnless(sys.platform == "linux", "Linux renameat2 and directory-FD ingestion required")
    def test_concurrent_publisher_and_ingest_never_observe_two_links(self) -> None:
        import ingest

        document_count = 80
        finished = threading.Event()
        errors: list[Exception] = []
        observed: set[Path] = set()
        scope = SimpleNamespace(source_dir=self.root)

        def publish_many() -> None:
            try:
                for index in range(document_count):
                    temporary = self.root / f".import-{index}.tmp"
                    final = self.root / f"document-{index}.txt"
                    with temporary.open("xb") as target:
                        target.write(f"document {index}".encode())
                        target.flush()
                        os.fsync(target.fileno())
                    publication.rename_noreplace(temporary, final)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=publish_many)
        worker.start()
        deadline = time.monotonic() + 15
        try:
            while not finished.is_set() or len(observed) < document_count:
                if time.monotonic() >= deadline:
                    self.fail("publisher/reader stress test timed out")
                for final in self.root.glob("document-*.txt"):
                    if final not in observed:
                        snapshot = ingest.read_snapshot(final, scope)
                        self.assertTrue(snapshot.data.startswith(b"document "))
                        observed.add(final)
                if finished.is_set() and errors:
                    break
                time.sleep(0.001)
        finally:
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(observed), document_count)


if __name__ == "__main__":
    unittest.main()
