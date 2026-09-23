"""Pure local tests; no NAS, database, or embedding service is contacted."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from calibrate_relevance import (
    RelevanceCase,
    build_report,
    evaluate_cases,
    load_cases,
    main,
)


class CalibrationTests(unittest.TestCase):
    def test_load_cases_validates_scope_and_boolean_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(
                json.dumps(
                    [{"query": "服务器", "scope": "family", "expected_relevant": True}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_cases(path)[0].scope, "family")

            path.write_text(
                '[{"query":"test","scope":"liang","expected_relevant":true}]',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsupported scope"):
                load_cases(path)

            path.write_text(
                '[{"query":"test","scope":"chen","expected_relevant":1}]',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "true or false"):
                load_cases(path)

    def test_top1_summary_and_thresholds_include_no_result(self) -> None:
        cases = [
            RelevanceCase("relevant", "family", True),
            RelevanceCase("unrelated", "family", False),
            RelevanceCase("empty", "chen", False),
        ]

        def fake_search(query: str, scope: str, top_k: int) -> list[SimpleNamespace]:
            self.assertEqual(top_k, 1)
            if query == "empty":
                return []
            score = 0.8 if query == "relevant" else 0.4
            return [
                SimpleNamespace(
                    filename="example.md",
                    chunk_index=2,
                    semantic_score=score,
                    semantic_distance=1 - score,
                    lexical_match=query == "relevant",
                    lexical_score=-1.2 if query == "relevant" else None,
                    fused_score=0.02,
                )
            ]

        records = evaluate_cases(cases, fake_search)
        report = build_report(records)
        summary = report["summary"]
        self.assertEqual(summary["positive_count"], 1)
        self.assertEqual(summary["negative_count"], 2)
        self.assertEqual(summary["no_result_count"], 1)
        self.assertEqual(summary["positive_semantic_score"]["median"], 0.8)
        self.assertEqual(summary["negative_semantic_score"]["median"], 0.4)
        self.assertEqual(summary["positive_lexical_match_rate"], 1.0)
        self.assertEqual(summary["negative_lexical_match_rate"], 0.0)
        self.assertIsNone(records[2]["semantic_score"])

        best = report["best_thresholds"][0]
        self.assertEqual(best["threshold"], 0.8)
        self.assertEqual(
            (best["true_positive"], best["false_positive"],
             best["true_negative"], best["false_negative"]),
            (1, 0, 2, 0),
        )
        self.assertEqual(best["f1"], 1.0)
        self.assertEqual(len(report["threshold_scan"]), 2)
        self.assertTrue(report["diagnostic_only"])
        self.assertEqual(len(json.loads(json.dumps(report))["cases"]), 3)

    def test_query_error_is_not_treated_as_no_result(self) -> None:
        def failing_search(query: str, scope: str, top_k: int) -> list[SimpleNamespace]:
            raise OSError("database unavailable")

        records = evaluate_cases(
            [RelevanceCase("question", "chen", True)], failing_search
        )
        report = build_report(records)
        self.assertFalse(records[0]["has_result"])
        self.assertIn("database unavailable", records[0]["error"])
        self.assertEqual(report["summary"]["failed_count"], 1)
        self.assertEqual(report["summary"]["no_result_count"], 0)
        self.assertEqual(report["threshold_scan"], [])

    def test_output_option_writes_machine_readable_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.json"
            output_path = Path(directory) / "results.json"
            cases_path.write_text(
                '[{"query":"missing","scope":"family","expected_relevant":false}]',
                encoding="utf-8",
            )
            with (
                patch.object(
                    sys,
                    "argv",
                    ["calibrate_relevance.py", str(cases_path), "--output", str(output_path)],
                ),
                patch("calibrate_relevance.evaluate_cases") as evaluate,
                redirect_stdout(io.StringIO()),
            ):
                evaluate.return_value = evaluate_cases(
                    [RelevanceCase("missing", "family", False)],
                    lambda query, scope, top_k: [],
                )
                self.assertEqual(main(), 0)
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["cases"][0]["query"], "missing")
            self.assertFalse(saved["cases"][0]["has_result"])
            self.assertEqual(saved["summary"]["no_result_count"], 1)


if __name__ == "__main__":
    unittest.main()
