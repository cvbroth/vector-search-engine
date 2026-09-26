"""Fixed scope layouts and identity maps; no NAS paths are touched."""

from __future__ import annotations

import unittest
from pathlib import Path

from config import (DEFAULT_SCOPE, KNOWLEDGE_SCOPES, PRIVATE_SCOPE_BY_AGENT,
                    PRIVATE_SCOPE_BY_UPLOADER, KnowledgeScope, get_scope)


class ScopeConfigTests(unittest.TestCase):
    def test_four_fixed_scopes_have_separate_paths(self) -> None:
        self.assertEqual(DEFAULT_SCOPE, "chen")
        self.assertEqual(set(KNOWLEDGE_SCOPES), {"chen", "liang", "azl", "family"})
        private = [get_scope(name) for name in ("chen", "liang", "azl")]
        self.assertTrue(all(scope.area == "private" for scope in private))
        self.assertEqual(get_scope("family").area, "shared")
        for scope in (*private, get_scope("family")):
            self.assertEqual(scope.source_dir, Path("/srv/storage/knowledge") / scope.area / scope.name)
            self.assertEqual(scope.state_dir, Path("/var/lib/knowledge-base") / scope.area / scope.name)
            self.assertEqual(scope.database_path, scope.state_dir / "index" / "knowledge.db")
        self.assertEqual(len({scope.source_dir for scope in private}), 3)
        self.assertEqual(len({scope.state_dir for scope in private}), 3)
        self.assertEqual(len({scope.database_path for scope in private}), 3)

    def test_no_arbitrary_or_ziling_scope(self) -> None:
        self.assertEqual(PRIVATE_SCOPE_BY_AGENT["ziling"], "azl")
        self.assertEqual(PRIVATE_SCOPE_BY_UPLOADER["azl"], "azl")
        for name in ("ziling", "unknown", "../chen", "/etc/passwd"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                get_scope(name)
        with self.assertRaises(ValueError):
            KnowledgeScope("family", "private")


if __name__ == "__main__":
    unittest.main()
