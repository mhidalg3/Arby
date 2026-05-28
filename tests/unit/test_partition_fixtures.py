"""Schema and integrity checks for tests/fixtures/partitions.yaml.

The fixture is the ground-truth labeled set the partition validator (Phase 2)
will be evaluated against. These tests don't exercise the validator itself —
they make sure the fixture file is well-formed, complete, and balanced before
anything depends on it.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "partitions.yaml"

REQUIRED_TOP_LEVEL_FIELDS = {
    "id",
    "desc_a",
    "desc_b",
    "match_context",
    "expected",
    "reasoning",
    "edge_case_category",
}
REQUIRED_CONTEXT_FIELDS = {"competition", "stage", "is_knockout"}
ALLOWED_EXPECTED_VALUES = {"valid", "invalid"}


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, list), "fixture root must be a YAML list"
    return data


def test_case_count_is_100(cases: list[dict[str, Any]]) -> None:
    assert len(cases) == 100


def test_every_case_has_required_fields(cases: list[dict[str, Any]]) -> None:
    for case in cases:
        missing = REQUIRED_TOP_LEVEL_FIELDS - case.keys()
        assert not missing, f"case {case.get('id', '?')} missing fields: {missing}"


def test_match_context_has_required_fields(cases: list[dict[str, Any]]) -> None:
    for case in cases:
        ctx = case["match_context"]
        missing = REQUIRED_CONTEXT_FIELDS - ctx.keys()
        assert not missing, f"case {case['id']} match_context missing: {missing}"
        assert isinstance(ctx["is_knockout"], bool), (
            f"case {case['id']} is_knockout must be a bool, got {type(ctx['is_knockout']).__name__}"
        )


def test_ids_are_unique(cases: list[dict[str, Any]]) -> None:
    ids = [c["id"] for c in cases]
    duplicates = [i for i, count in Counter(ids).items() if count > 1]
    assert not duplicates, f"duplicate ids: {duplicates}"


def test_expected_label_is_valid_or_invalid(cases: list[dict[str, Any]]) -> None:
    for case in cases:
        assert case["expected"] in ALLOWED_EXPECTED_VALUES, (
            f"case {case['id']} has unexpected label {case['expected']!r}"
        )


def test_reasoning_is_non_empty(cases: list[dict[str, Any]]) -> None:
    for case in cases:
        reasoning = case["reasoning"]
        assert isinstance(reasoning, str) and reasoning.strip(), (
            f"case {case['id']} has empty or non-string reasoning"
        )


def test_descriptions_are_distinct(cases: list[dict[str, Any]]) -> None:
    """A pair of identical descriptions can't form a meaningful partition test."""
    for case in cases:
        assert case["desc_a"] != case["desc_b"], (
            f"case {case['id']} has identical desc_a and desc_b"
        )


def test_set_is_balanced(cases: list[dict[str, Any]]) -> None:
    """At least 30% of each label so the validator can't trivially win by always
    predicting one class."""
    counts = Counter(c["expected"] for c in cases)
    assert counts["valid"] >= 30, f"only {counts['valid']} valid cases — too few"
    assert counts["invalid"] >= 30, f"only {counts['invalid']} invalid cases — too few"


def test_critical_traps_are_covered(cases: list[dict[str, Any]]) -> None:
    """The validator must catch these classes of error or it is not deployable.
    Verify the labeled set contains at least one example of each."""
    required_categories = {
        "win_loss_trap_non_knockout",
        "win_loss_trap_knockout_90min",
        "integer_push",
        "asian_handicap_overlap",
        "asian_handicap_push",
        "asian_handicap_quarter",
        "double_chance_overlap",
        "goalscorer_overlap",
        "scope_mismatch",
        "to_qualify_scope_mismatch",
    }
    present = {c["edge_case_category"] for c in cases}
    missing = required_categories - present
    assert not missing, f"trap categories not represented in the fixture: {missing}"
