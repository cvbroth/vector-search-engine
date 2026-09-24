"""Local-only tests for per-scope ingest and publication locks."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ingest
from scope_lock import scope_file_lock


class ScopeLockTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_same_scope_ingest_calls_are_serial(self) -> None:
        scope = SimpleNamespace(state_dir=self.root / "family")
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        errors: list[Exception] = []
        calls = 0

        def critical_section(_scope: object) -> dict[str, int]:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                if not release_first.wait(5):
                    raise TimeoutError("test ingest wait timed out")
            else:
                second_entered.set()
            return {"failed": 0}

        def run() -> None:
            try:
                ingest.ingest_scope("family")
            except Exception as exc:
                errors.append(exc)

        with patch.object(ingest, "get_scope", return_value=scope):
            with patch.object(ingest, "_ingest_scope_locked", side_effect=critical_section):
                first = threading.Thread(target=run)
                second = threading.Thread(target=run)
                first.start()
                try:
                    self.assertTrue(first_entered.wait(5))
                    second.start()
                    self.assertFalse(second_entered.wait(0.2))
                finally:
                    release_first.set()
                    first.join(5)
                    if second.ident is not None:
                        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(second_entered.is_set())
        self.assertEqual(calls, 2)

    def test_different_scope_locks_are_independent(self) -> None:
        family = self.root / "family" / "ingest.lock"
        chen = self.root / "chen" / "ingest.lock"
        with scope_file_lock(family):
            with scope_file_lock(chen):
                self.assertTrue(family.is_file())
                self.assertTrue(chen.is_file())

    def test_leftover_file_is_not_a_stale_lock(self) -> None:
        path = self.root / "family" / "ingest.lock"
        with scope_file_lock(path):
            pass
        self.assertTrue(path.is_file())
        with scope_file_lock(path):
            pass

    def test_process_exit_releases_lock(self) -> None:
        path = self.root / "family" / "ingest.lock"
        script = (
            "import os,sys; from pathlib import Path; from scope_lock import scope_file_lock; "
            "guard=scope_file_lock(Path(sys.argv[1])); guard.__enter__(); "
            "print('locked', flush=True); os._exit(0)"
        )
        child = subprocess.run(
            [sys.executable, "-B", "-c", script, str(path)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
            check=False,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "locked")
        with scope_file_lock(path):
            pass

    def test_symlink_lock_is_rejected(self) -> None:
        target = self.root / "real.lock"
        target.write_bytes(b"")
        link = self.root / "family" / "ingest.lock"
        link.parent.mkdir()
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this host")
        with self.assertRaises(OSError):
            with scope_file_lock(link):
                pass

    @unittest.skipUnless(os.name == "posix", "Unix permission bits are required")
    def test_lock_mode_is_group_writable_under_restrictive_umask(self) -> None:
        path = self.root / "family" / "ingest.lock"
        previous = os.umask(0o077)
        try:
            with scope_file_lock(path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o660)
        finally:
            os.umask(previous)


if __name__ == "__main__":
    unittest.main()
