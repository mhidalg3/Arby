"""Fixture-driven evaluation of the rule-based pre-filter.

This is the safety net the architecture cares about. For every case the
pre-filter classifies as VALID or INVALID, the prediction MUST match the
ground-truth label — a false VALID puts unhedged money on the table; a
false INVALID quietly drops a real arb. The single mislabel-zero assertion
catches either failure mode the moment a new rule introduces it.

Coverage and per-category breakdowns are printed for visibility but not
asserted, since they will rise monotonically as we add rules. Re-run with
`uv run pytest tests/unit/test_partition_filter_eval.py -s` to see them.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.semantic.partition_filter import MatchContext, classify

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "partitions.yaml"


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, list)
    return data


@pytest.fixture(scope="module")
def predictions(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run every case through `classify` once, return enriched records."""
    out = []
    for case in cases:
        ctx = MatchContext(**case["match_context"])
        verdict, reason = classify(case["desc_a"], case["desc_b"], ctx)
        out.append(
            {
                "id": case["id"],
                "expected": case["expected"],
                "predicted": verdict.value,
                "reason": reason,
                "category": case["edge_case_category"],
            }
        )
    return out


def test_pre_filter_never_lies(predictions: list[dict[str, Any]]) -> None:
    """Safety property: every non-UNKNOWN verdict must match ground truth.

    This is the assertion the architecture requires. As rules are added,
    coverage rises; this test ensures correctness does not regress with it.
    """
    mismatches = [
        f"{p['id']}: predicted {p['predicted']!r}, expected {p['expected']!r} (rule: {p['reason']})"
        for p in predictions
        if p["predicted"] != "unknown" and p["predicted"] != p["expected"]
    ]
    assert not mismatches, "\n".join(mismatches)


def test_report_coverage_and_breakdown(predictions: list[dict[str, Any]]) -> None:
    """Informational metrics. Always passes; the print output is the value."""
    total = len(predictions)
    by_verdict = Counter(p["predicted"] for p in predictions)
    covered = total - by_verdict["unknown"]
    coverage_pct = covered / total * 100

    # Per-category breakdown of where the pre-filter currently lands.
    by_category: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for p in predictions:
        by_category[p["category"]][p["predicted"]] += 1

    print()
    print(f"--- partition pre-filter coverage on labeled set ({total} cases) ---")
    print(f"  covered:   {covered:>3} ({coverage_pct:5.1f}%)")
    print(f"  escalated: {by_verdict['unknown']:>3} (LLM workload)")
    print(
        f"  verdicts:  valid={by_verdict['valid']}, "
        f"invalid={by_verdict['invalid']}, unknown={by_verdict['unknown']}"
    )
    print()
    print("  per-category breakdown (only categories with coverage):")
    for category in sorted(by_category):
        counts = by_category[category]
        if counts["unknown"] == sum(counts.values()):
            continue
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"    {category:<40} {rendered}")
