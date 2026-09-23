"""Local gate tests with mocked retrieval; no NAS or embedding calls."""

from __future__ import annotations

import importlib
import io
import math
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from config import (
    RELEVANCE_ACCEPT_THRESHOLD,
    RELEVANCE_REJECT_THRESHOLD,
    validate_relevance_thresholds,
)
from relevance import RelevanceDecision, classify_relevance

# The workstation need not have the server's parser/vector dependencies.
# Import search with a placeholder database module; every call is mocked below.
database_stub = types.ModuleType("database")
database_stub.connect_database = Mock()  # type: ignore[attr-defined]
with patch.dict(sys.modules, {"database": database_stub}):
    search = importlib.import_module("search")


def make_hit(name: str, score: float, chunk_id: int) -> search._HybridHit:
    return search._HybridHit(
        fused_score=0.01 + chunk_id / 1000,
        row={
            "id": chunk_id,
            "source_path": f"/srv/storage/knowledge/shared/family/{name}",
            "filename": name,
            "page": None,
            "chunk_index": 0,
            "text": name,
        },
        semantic_distance=1.0 - score,
        lexical_score=-0.5 if name == "uncertain.md" else None,
    )


class RelevanceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hits = [
            make_hit("reject.md", 0.54, 1),
            make_hit("uncertain.md", 0.63, 2),
            make_hit("accept.md", 0.64, 3),
        ]
        self.connection = Mock()

    def test_boundary_decisions_and_missing_score(self) -> None:
        expected = [
            (0.54, RelevanceDecision.REJECT),
            (0.55, RelevanceDecision.UNCERTAIN),
            (0.63, RelevanceDecision.UNCERTAIN),
            (0.64, RelevanceDecision.ACCEPT),
            (None, RelevanceDecision.UNCERTAIN),
        ]
        for score, decision in expected:
            with self.subTest(score=score):
                self.assertIs(classify_relevance(score), decision)
        self.assertIs(
            classify_relevance(0.54, reject_threshold=0.50, accept_threshold=0.70),
            RelevanceDecision.UNCERTAIN,
        )

    def test_threshold_configuration_validation(self) -> None:
        self.assertLess(RELEVANCE_REJECT_THRESHOLD, RELEVANCE_ACCEPT_THRESHOLD)
        validate_relevance_thresholds(0.55, 0.64)
        for reject, accept in ((0.64, 0.55), (0.55, 0.55), (math.nan, 0.64), (True, 0.64)):
            with self.subTest(reject=reject, accept=accept):
                with self.assertRaises(ValueError):
                    validate_relevance_thresholds(reject, accept)
        with self.assertRaises(ValueError):
            classify_relevance(math.nan)

    def test_single_scope_api_defaults_to_unfiltered_results(self) -> None:
        with (
            patch.object(search, "connect_database", return_value=self.connection),
            patch.object(search, "_hybrid_hits", return_value=self.hits),
        ):
            original = search.search_scope("question", "family", 3)
            gated = search.search_scope("question", "family", 3, relevance_gate=True)
        self.assertEqual([item.filename for item in original],
                         ["reject.md", "uncertain.md", "accept.md"])
        self.assertEqual([item.filename for item in gated],
                         ["uncertain.md", "accept.md"])
        self.assertEqual([item.rank for item in gated], [1, 2])
        self.assertEqual(gated[0].fused_score, original[1].fused_score)
        self.assertTrue(gated[0].lexical_match)
        self.assertIs(gated[0].relevance_decision, RelevanceDecision.UNCERTAIN)

    def test_multi_scope_api_filters_only_when_requested(self) -> None:
        with (
            patch.object(search, "connect_database", return_value=self.connection),
            patch.object(search, "_hybrid_hits", side_effect=[
                [self.hits[0]], [self.hits[1]],
                [self.hits[0]], [self.hits[1]],
            ]),
        ):
            original = search.search_scopes("question", ["chen", "family"], 2)
            gated = search.search_scopes(
                "question", ["chen", "family"], 2, relevance_gate=True
            )
        self.assertEqual(len(original), 2)
        self.assertEqual([item.filename for item in gated], ["uncertain.md"])
        self.assertEqual(gated[0].rank, 1)

    def test_search_results_keep_full_text_without_expanding_cli_snippet(self) -> None:
        full_text = "甲" * 400
        hit = make_hit("long.md", 0.7, 4)
        hit.row["text"] = full_text
        with (
            patch.object(search, "connect_database", return_value=self.connection),
            patch.object(search, "_hybrid_hits", return_value=[hit]),
        ):
            single = search.search_scope("question", "family", 1)
            multi = search.search_scopes("question", ["family"], 1)
        self.assertEqual(single[0].text, full_text)
        self.assertEqual(multi[0].text, full_text)
        self.assertEqual(len(single[0].snippet), 280)

        with (
            patch.object(search, "_hybrid_hits", return_value=[hit]),
            patch.object(search, "connect_database", return_value=self.connection),
            patch.object(search, "configure_logging"),
            patch.object(sys, "argv", ["search.py", "question", "--scope", "family"]),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(search.main(), 0)
        self.assertNotIn(full_text, output.getvalue())

    def _cli_output(self, *flags: str) -> str:
        with (
            patch.object(sys, "argv", ["search.py", "question", "--scope", "family", *flags]),
            patch.object(search, "configure_logging"),
            patch.object(search, "connect_database", return_value=self.connection),
            patch.object(search, "_hybrid_hits", return_value=self.hits),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(search.main(), 0)
        return output.getvalue()

    def test_cli_default_and_explicit_gate(self) -> None:
        default = self._cli_output()
        self.assertIn("filename: reject.md", default)
        self.assertIn("filename: uncertain.md", default)
        self.assertNotIn("relevance decision:", default)

        gated = self._cli_output("--relevance-gate")
        self.assertNotIn("filename: reject.md", gated)
        self.assertIn("filename: uncertain.md", gated)
        self.assertIn("filename: accept.md", gated)
        self.assertIn("relevance decision: UNCERTAIN", gated)
        self.assertIn("relevance decision: ACCEPT", gated)

    def test_debug_scores_include_decision_without_filtering(self) -> None:
        output = self._cli_output("--debug-scores")
        self.assertIn("filename: reject.md", output)
        self.assertIn("semantic distance (cosine):", output)
        self.assertIn("semantic score (cosine similarity):", output)
        self.assertIn("lexical match (FTS top-N):", output)
        self.assertIn("lexical score (FTS5 BM25):", output)
        self.assertIn("relevance decision: REJECT", output)
        self.assertIn("relevance decision: UNCERTAIN", output)
        self.assertIn("relevance decision: ACCEPT", output)

        gated_debug = self._cli_output("--relevance-gate", "--debug-scores")
        self.assertNotIn("filename: reject.md", gated_debug)
        self.assertIn("semantic score (cosine similarity):", gated_debug)
        self.assertIn("relevance decision: UNCERTAIN", gated_debug)


if __name__ == "__main__":
    unittest.main()
