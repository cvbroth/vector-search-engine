"""RAG Context contract tests with mocked search; no NAS access."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rag_context import _retrieval_k, build_rag_context, main
from relevance import RelevanceDecision


def fake_result(
    scope: str = "family",
    decision: RelevanceDecision = RelevanceDecision.ACCEPT,
    text: str = "完整证据" * 100,
) -> SimpleNamespace:
    return SimpleNamespace(
        scope=scope,
        rank=1,
        fused_score=0.032787,
        semantic_score=0.759152,
        semantic_distance=0.240848,
        lexical_match=True,
        lexical_score=-0.000012,
        relevance_decision=decision,
        source_path=(
            f"/srv/storage/knowledge/"
            f"{'private/chen' if scope == 'chen' else 'shared/family'}/test.md"
        ),
        filename="test.md",
        page=None,
        chunk_index=0,
        snippet="完整证据" * 35,
        text=text,
    )


def fake_search_module(
    single: list[SimpleNamespace] | None = None,
    multi: list[SimpleNamespace] | None = None,
) -> types.ModuleType:
    module = types.ModuleType("search")
    module.search_scope = Mock(return_value=single if single is not None else [])
    module.search_scopes = Mock(return_value=multi if multi is not None else [])
    return module


class RagContextTests(unittest.TestCase):
    def test_overfetch_bounds(self) -> None:
        self.assertEqual(_retrieval_k(1), 20)
        self.assertEqual(_retrieval_k(3), 20)
        self.assertEqual(_retrieval_k(5), 25)
        self.assertEqual(_retrieval_k(10), 50)
        self.assertEqual(_retrieval_k(50), 50)

    def test_overfetch_recovers_accept_beyond_rejected_top_three(self) -> None:
        raw = [
            fake_result(decision=RelevanceDecision.REJECT) for _ in range(3)
        ] + [fake_result(decision=RelevanceDecision.ACCEPT, text="fourth candidate")]
        module = fake_search_module()

        def gated_search(query: str, scope: str, candidate_k: int, *, relevance_gate: bool):
            self.assertTrue(relevance_gate)
            return [
                item for item in raw[:candidate_k]
                if item.relevance_decision is not RelevanceDecision.REJECT
            ]

        module.search_scope.side_effect = gated_search
        with patch.dict(sys.modules, {"search": module}):
            context = build_rag_context("测试代号？", ["family"], top_k=3)
        module.search_scope.assert_called_once_with(
            "测试代号？", "family", 20, relevance_gate=True
        )
        self.assertIs(context.retrieval_status, RelevanceDecision.ACCEPT)
        self.assertEqual(context.evidence_count, 1)
        self.assertEqual(context.evidence[0].text, "fourth candidate")

    def test_final_evidence_count_never_exceeds_requested_top_k(self) -> None:
        candidates = [fake_result(text=f"chunk {i}") for i in range(20)]
        module = fake_search_module(single=candidates, multi=candidates)
        with patch.dict(sys.modules, {"search": module}):
            single = build_rag_context("问题", ["family"], top_k=3)
            multi = build_rag_context("问题", ["chen", "family"], top_k=3)
        self.assertEqual(single.evidence_count, 3)
        self.assertEqual(multi.evidence_count, 3)
        self.assertLessEqual(single.to_dict()["evidence_count"], 3)
        self.assertLessEqual(multi.to_dict()["evidence_count"], 3)
        self.assertEqual([item.text for item in single.evidence],
                         ["chunk 0", "chunk 1", "chunk 2"])
        self.assertEqual([item.text for item in multi.evidence],
                         ["chunk 0", "chunk 1", "chunk 2"])
        module.search_scope.assert_called_once_with("问题", "family", 20, relevance_gate=True)
        module.search_scopes.assert_called_once_with(
            "问题", ["chen", "family"], 20, relevance_gate=True
        )

    def test_single_scope_accept_and_full_chunk_text(self) -> None:
        full_text = "完整证据" * 100
        module = fake_search_module(single=[fake_result(text=full_text)])
        with patch.dict(sys.modules, {"search": module}):
            context = build_rag_context("测试代号是什么？", ["family"], 5)
        module.search_scope.assert_called_once_with(
            "测试代号是什么？", "family", 25, relevance_gate=True
        )
        module.search_scopes.assert_not_called()
        self.assertIs(context.retrieval_status, RelevanceDecision.ACCEPT)
        self.assertEqual(context.evidence_count, 1)
        payload = context.to_dict()
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["scopes"], ["family"])
        self.assertEqual(payload["evidence"][0]["scope"], "family")
        self.assertEqual(payload["evidence"][0]["text"], full_text)
        self.assertGreater(len(payload["evidence"][0]["text"]), 280)
        self.assertNotIn("snippet", payload["evidence"][0])
        self.assertEqual(payload["evidence"][0]["relevance_decision"], "ACCEPT")

    def test_uncertain_is_retained(self) -> None:
        module = fake_search_module(single=[fake_result(decision=RelevanceDecision.UNCERTAIN)])
        with patch.dict(sys.modules, {"search": module}):
            context = build_rag_context("CPU 是什么？", ["family"])
        self.assertIs(context.retrieval_status, RelevanceDecision.UNCERTAIN)
        self.assertEqual(context.evidence_count, 1)
        self.assertEqual(context.to_dict()["evidence"][0]["relevance_decision"], "UNCERTAIN")

    def test_chen_only_does_not_search_family(self) -> None:
        module = fake_search_module(single=[fake_result(scope="chen")])
        with patch.dict(sys.modules, {"search": module}):
            context = build_rag_context("问题", ["chen"])
        module.search_scope.assert_called_once_with("问题", "chen", 25, relevance_gate=True)
        module.search_scopes.assert_not_called()
        self.assertEqual(context.to_dict()["scopes"], ["chen"])

    def test_reject_has_empty_evidence_and_exit_zero(self) -> None:
        module = fake_search_module(single=[])
        with (
            patch.dict(sys.modules, {"search": module}),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
        ):
            code = main(["未知问题", "--scope", "family"])
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["retrieval_status"], "REJECT")
        self.assertEqual(payload["evidence_count"], 0)
        self.assertEqual(payload["evidence"], [])
        module.search_scope.assert_called_once_with(
            "未知问题", "family", 25, relevance_gate=True
        )

    def test_explicit_multi_scope_uses_existing_fusion_api(self) -> None:
        module = fake_search_module(
            multi=[fake_result(scope="chen", decision=RelevanceDecision.UNCERTAIN),
                   fake_result(scope="family", decision=RelevanceDecision.ACCEPT)]
        )
        with patch.dict(sys.modules, {"search": module}):
            context = build_rag_context("测试代号？", ["chen", "family"], 2)
        module.search_scopes.assert_called_once_with(
            "测试代号？", ["chen", "family"], 20, relevance_gate=True
        )
        module.search_scope.assert_not_called()
        self.assertEqual(context.to_dict()["scopes"], ["chen", "family"])
        self.assertEqual(
            [item["scope"] for item in context.to_dict()["evidence"]],
            ["chen", "family"],
        )
        self.assertIs(context.retrieval_status, RelevanceDecision.ACCEPT)

        with (
            patch.dict(sys.modules, {"search": module}),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
        ):
            code = main(["测试代号？", "--scope", "chen", "--scope", "family"])
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["scopes"], ["chen", "family"])

    def test_stdout_is_pure_json_and_output_file_matches(self) -> None:
        module = fake_search_module(single=[fake_result()])
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "context.json"
            with (
                patch.dict(sys.modules, {"search": module}),
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()) as errors,
            ):
                code = main(["测试代号？", "--scope", "family", "--output", str(destination)])
            self.assertEqual(code, 0)
            self.assertEqual(errors.getvalue(), "")
            stdout_payload = json.loads(output.getvalue())
            file_payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(stdout_payload, file_payload)
            self.assertEqual(stdout_payload["query"], "测试代号？")
            self.assertIsNone(stdout_payload["evidence"][0]["page"])
            self.assertIs(stdout_payload["evidence"][0]["lexical_match"], True)

    def test_errors_never_become_reject_context(self) -> None:
        module = fake_search_module()
        module.search_scope.side_effect = sqlite3.OperationalError("database unavailable")
        with (
            patch.dict(sys.modules, {"search": module}),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
        ):
            code = main(["问题", "--scope", "family"])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("database unavailable", errors.getvalue())

        with self.assertRaisesRegex(ValueError, "unsupported knowledge scope"):
            build_rag_context("问题", ["ziling"])
        with self.assertRaisesRegex(ValueError, "unique"):
            build_rag_context("问题", ["chen", "chen"])
        with (
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as exc:
                main(["问题", "--scope", "ziling"])
        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(output.getvalue(), "")

        invalid_json_context = SimpleNamespace(to_dict=lambda: {"score": float("nan")})
        with (
            patch("rag_context.build_rag_context", return_value=invalid_json_context),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
        ):
            code = main(["问题", "--scope", "family"])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("rag context failed:", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
