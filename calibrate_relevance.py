"""Diagnose Top1 retrieval scores from labeled queries; never filter searches."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config import get_scope
from embeddings import EmbeddingError

if TYPE_CHECKING:
    from search import SearchResult

SearchOne = Callable[[str, str, int], list["SearchResult"]]


@dataclass(frozen=True, slots=True)
class RelevanceCase:
    query: str
    scope: str
    expected_relevant: bool


def load_cases(path: Path) -> list[RelevanceCase]:
    """Validate labels and the same fixed scope whitelist used by search.py."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError("test set must be a JSON array")

    cases: list[RelevanceCase] = []
    for number, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"case {number} must be a JSON object")
        query = item.get("query")
        scope = item.get("scope")
        relevant = item.get("expected_relevant")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"case {number} has no non-empty query")
        if not isinstance(scope, str):
            raise ValueError(f"case {number} has no valid scope")
        try:
            get_scope(scope)
        except ValueError as exc:
            raise ValueError(f"case {number} has unsupported scope: {scope!r}") from exc
        if type(relevant) is not bool:
            raise ValueError(f"case {number} expected_relevant must be true or false")
        cases.append(RelevanceCase(query, scope, relevant))
    return cases


def evaluate_cases(
    cases: Sequence[RelevanceCase], search_one: SearchOne | None = None
) -> list[dict[str, Any]]:
    """Call the existing single-scope hybrid search once per labeled query."""
    if search_one is None:
        from search import search_scope

        search_one = search_scope

    records: list[dict[str, Any]] = []
    for case in cases:
        record: dict[str, Any] = {
            "query": case.query,
            "scope": case.scope,
            "expected_relevant": case.expected_relevant,
            "has_result": False,
            "filename": None,
            "chunk_index": None,
            "semantic_score": None,
            "semantic_distance": None,
            "lexical_match": None,
            "lexical_score": None,
            "fused_score": None,
            "error": None,
        }
        try:
            hits = search_one(case.query, case.scope, 1)
        except (OSError, sqlite3.Error, EmbeddingError, ValueError) as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        else:
            if hits:
                hit = hits[0]
                record.update(
                    has_result=True,
                    filename=hit.filename,
                    chunk_index=hit.chunk_index,
                    semantic_score=hit.semantic_score,
                    semantic_distance=hit.semantic_distance,
                    lexical_match=hit.lexical_match,
                    lexical_score=hit.lexical_score,
                    fused_score=hit.fused_score,
                )
                for field in (
                    "semantic_score", "semantic_distance", "lexical_score", "fused_score"
                ):
                    value = record[field]
                    if value is not None and not math.isfinite(value):
                        record["error"] = f"non-finite {field}"
                        break
        records.append(record)
    return records


def _score_stats(records: Sequence[dict[str, Any]]) -> dict[str, float | int | None]:
    values = [
        row["semantic_score"]
        for row in records
        if row["error"] is None and row["semantic_score"] is not None
    ]
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "median": statistics.median(values) if values else None,
        "max": max(values) if values else None,
    }


def summarize(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    positive = [row for row in records if row["expected_relevant"]]
    negative = [row for row in records if not row["expected_relevant"]]

    def lexical_rate(group: Sequence[dict[str, Any]]) -> float | None:
        valid = [row for row in group if row["error"] is None]
        if not valid:
            return None
        # A query without a Top1 result has no lexical hit.
        return sum(row["lexical_match"] is True for row in valid) / len(valid)

    return {
        "positive_count": len(positive),
        "negative_count": len(negative),
        "failed_count": sum(row["error"] is not None for row in records),
        "no_result_count": sum(
            row["error"] is None and not row["has_result"] for row in records
        ),
        "positive_semantic_score": _score_stats(positive),
        "negative_semantic_score": _score_stats(negative),
        "positive_lexical_match_rate": lexical_rate(positive),
        "negative_lexical_match_rate": lexical_rate(negative),
    }


def scan_thresholds(records: Sequence[dict[str, Any]]) -> list[dict[str, float | int]]:
    """Try every observed Top1 score; missing scores predict negative."""
    valid = [row for row in records if row["error"] is None]
    thresholds = sorted(
        {row["semantic_score"] for row in valid if row["semantic_score"] is not None},
        reverse=True,
    )
    scan: list[dict[str, float | int]] = []
    for threshold in thresholds:
        tp = fp = tn = fn = 0
        for row in valid:
            score = row["semantic_score"]
            predicted = score is not None and score >= threshold
            if predicted and row["expected_relevant"]:
                tp += 1
            elif predicted:
                fp += 1
            elif row["expected_relevant"]:
                fn += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        scan.append(
            {
                "threshold": threshold,
                "true_positive": tp,
                "false_positive": fp,
                "true_negative": tn,
                "false_negative": fn,
                "precision": precision,
                "recall": recall,
                "f1": f1,
            }
        )
    return sorted(
        scan,
        key=lambda item: (-item["f1"], -item["precision"], -item["recall"], -item["threshold"]),
    )


def build_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    scan = scan_thresholds(records)
    return {
        "schema_version": 1,
        "diagnostic_only": True,
        "score_definition": "Top1 cosine similarity = 1 - cosine distance",
        "threshold_rule": "semantic_score >= threshold; no result/missing score => negative",
        "cases": records,
        "summary": summarize(records),
        "best_thresholds": scan[:5],
        "threshold_scan": scan,
        "warning": "Small or unrepresentative test sets cannot establish a production threshold.",
    }


def _display_number(value: float | None) -> str:
    return "-" if value is None else f"{value:.6f}"


def print_report(report: dict[str, Any]) -> None:
    for number, row in enumerate(report["cases"], start=1):
        if row["error"] is not None:
            outcome = f"ERROR: {row['error']}"
        elif not row["has_result"]:
            outcome = "NO RESULT"
        else:
            outcome = (
                f"{row['filename']} chunk={row['chunk_index']} "
                f"semantic={_display_number(row['semantic_score'])} "
                f"distance={_display_number(row['semantic_distance'])} "
                f"lexical={row['lexical_match']} "
                f"BM25={_display_number(row['lexical_score'])} "
                f"RRF={_display_number(row['fused_score'])}"
            )
        print(
            f"{number}. [{row['scope']}] expected={row['expected_relevant']} "
            f"{row['query']} -> {outcome}"
        )

    summary = report["summary"]
    print(
        f"\nPositive: {summary['positive_count']}; negative: {summary['negative_count']}; "
        f"no result: {summary['no_result_count']}; errors: {summary['failed_count']}"
    )
    for label in ("positive", "negative"):
        stats = summary[f"{label}_semantic_score"]
        rate = summary[f"{label}_lexical_match_rate"]
        print(
            f"{label} semantic_score (n={stats['count']}): "
            f"min={_display_number(stats['min'])}, "
            f"median={_display_number(stats['median'])}, "
            f"max={_display_number(stats['max'])}; "
            f"lexical_match=True: {_display_number(rate)}"
        )

    print("\nBest candidate thresholds (diagnostic only):")
    if not report["best_thresholds"]:
        print("No semantic scores available.")
    for item in report["best_thresholds"]:
        print(
            f"  >= {item['threshold']:.6f}: "
            f"TP={item['true_positive']} FP={item['false_positive']} "
            f"TN={item['true_negative']} FN={item['false_negative']} "
            f"precision={item['precision']:.4f} recall={item['recall']:.4f} "
            f"F1={item['f1']:.4f}"
        )
    print("Warning: a small test set cannot establish a production threshold.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose labeled Top1 retrieval results")
    parser.add_argument("cases", type=Path, help="JSON array of labeled queries")
    parser.add_argument("--output", type=Path, help="write machine-readable JSON report")
    args = parser.parse_args()
    if args.output is not None and args.output.resolve() == args.cases.resolve():
        parser.error("--output must not overwrite the input test set")

    try:
        cases = load_cases(args.cases)
        report = build_report(evaluate_cases(cases))
        if args.output is not None:
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
            )
    except (ImportError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"calibration failed: {exc}", file=sys.stderr)
        return 1

    print_report(report)
    return 1 if report["summary"]["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
