"""Evaluate the partition validator against the labeled fixture.

Loads `tests/fixtures/partitions.yaml`, runs every case through the full
pre-filter → LLM pipeline, and reports:

  - accuracy, precision/recall on each label
  - source breakdown (pre-filter hits vs LLM escalations)
  - confusion matrix
  - explicit listing of false VALIDs (the dangerous errors the >99%
    precision-on-VALID gate exists for)
  - explicit listing of false INVALIDs (missed arbs — softer cost)

The first request warms the system-prompt cache; subsequent concurrent
calls read from it. Concurrency is capped at 5 to stay well inside
tier-1 rate limits.

Usage:
    ANTHROPIC_API_KEY=... uv run python scripts/eval_partition_validator.py

This script costs real money on every run. The full fixture currently
escalates ~77 cases to the LLM at Opus 4.7 pricing.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from anthropic import AsyncAnthropic

from src.semantic.llm_validator import LLMValidator
from src.semantic.partition_filter import MatchContext
from src.semantic.partition_validator import PartitionValidator

FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "partitions.yaml"
CONCURRENCY = 5
PRECISION_TARGET = 0.99


@dataclass(frozen=True)
class Prediction:
    id: str
    expected: str
    predicted: str
    source: str
    reason: str
    confidence: str | None  # None for pre-filter decisions
    trap_pattern: str | None
    category: str


async def _evaluate_one(
    case: dict[str, Any],
    validator: PartitionValidator,
    sem: asyncio.Semaphore,
) -> Prediction:
    async with sem:
        ctx = MatchContext(**case["match_context"])
        decision = await validator.validate(case["desc_a"], case["desc_b"], ctx)
    return Prediction(
        id=case["id"],
        expected=case["expected"],
        predicted=decision.verdict.value,
        source=decision.source,
        reason=decision.reason,
        confidence=decision.llm_verdict.confidence if decision.llm_verdict else None,
        trap_pattern=(decision.llm_verdict.trap_pattern if decision.llm_verdict else None),
        category=case["edge_case_category"],
    )


def _print_metrics(preds: list[Prediction]) -> bool:
    total = len(preds)
    by_source = Counter(p.source for p in preds)
    by_verdict = Counter(p.predicted for p in preds)

    tp_valid = sum(1 for p in preds if p.expected == "valid" and p.predicted == "valid")
    fp_valid = sum(1 for p in preds if p.expected == "invalid" and p.predicted == "valid")
    fn_valid = sum(1 for p in preds if p.expected == "valid" and p.predicted == "invalid")
    tp_invalid = sum(1 for p in preds if p.expected == "invalid" and p.predicted == "invalid")
    fp_invalid = fn_valid  # the symmetry
    fn_invalid = fp_valid

    prec_valid = tp_valid / (tp_valid + fp_valid) if (tp_valid + fp_valid) else 0.0
    rec_valid = tp_valid / (tp_valid + fn_valid) if (tp_valid + fn_valid) else 0.0
    prec_invalid = tp_invalid / (tp_invalid + fp_invalid) if (tp_invalid + fp_invalid) else 0.0
    rec_invalid = tp_invalid / (tp_invalid + fn_invalid) if (tp_invalid + fn_invalid) else 0.0
    accuracy = (tp_valid + tp_invalid) / total

    print()
    print("=" * 72)
    print(f"Partition validator evaluation — {total} cases")
    print("=" * 72)
    print(f"Source breakdown:    pre_filter={by_source['pre_filter']}, llm={by_source['llm']}")
    print(f"Verdict breakdown:   valid={by_verdict['valid']}, invalid={by_verdict['invalid']}")
    print(f"Accuracy:            {accuracy:.1%} ({tp_valid + tp_invalid}/{total})")
    print()
    print("VALID label (the safety property):")
    print(f"  Precision: {prec_valid:.1%}  ({tp_valid}/{tp_valid + fp_valid})")
    print(f"  Recall:    {rec_valid:.1%}  ({tp_valid}/{tp_valid + fn_valid})")
    print(f"  Target:    {PRECISION_TARGET:.0%}")
    target_met = prec_valid >= PRECISION_TARGET
    status = "PASS" if target_met else "FAIL"
    print(f"  Status:    [{status}]  ({fp_valid} false VALID{'s' if fp_valid != 1 else ''})")
    print()
    print("INVALID label:")
    print(f"  Precision: {prec_invalid:.1%}  ({tp_invalid}/{tp_invalid + fp_invalid})")
    print(f"  Recall:    {rec_invalid:.1%}  ({tp_invalid}/{tp_invalid + fn_invalid})")
    print()
    print("Confusion matrix:")
    print("            pred=valid  pred=invalid")
    print(f"  true=valid    {tp_valid:>4}        {fn_valid:>4}")
    print(f"  true=invalid  {fp_valid:>4}        {tp_invalid:>4}")

    false_valids = [p for p in preds if p.expected == "invalid" and p.predicted == "valid"]
    if false_valids:
        print()
        print("-" * 72)
        print(f"FALSE VALIDS ({len(false_valids)}) — the dangerous errors")
        print("-" * 72)
        for p in false_valids:
            print(f"  [{p.id}]  source={p.source}  category={p.category}")
            if p.confidence:
                print(f"      confidence={p.confidence}  trap_pattern={p.trap_pattern}")
            print(f"      reason: {p.reason}")
            print()

    false_invalids = [p for p in preds if p.expected == "valid" and p.predicted == "invalid"]
    if false_invalids:
        print("-" * 72)
        print(f"FALSE INVALIDS ({len(false_invalids)}) — missed arbs (softer cost)")
        print("-" * 72)
        for p in false_invalids:
            print(f"  [{p.id}]  source={p.source}  category={p.category}")
            if p.confidence:
                print(f"      confidence={p.confidence}  trap_pattern={p.trap_pattern}")
            print(f"      reason: {p.reason}")
            print()

    return target_met


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "error: ANTHROPIC_API_KEY is not set. Export it and re-run.",
            file=sys.stderr,
        )
        return 2

    cases = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(cases, list)
    print(f"Loaded {len(cases)} cases from {FIXTURE_PATH.name}")

    client = AsyncAnthropic()
    llm_validator = LLMValidator(client=client)
    validator = PartitionValidator(llm_validator=llm_validator)

    # Warm the cache with a single sequential call before fanning out;
    # otherwise the first 5 concurrent calls all pay full prompt-write
    # cost since none can read what the others are still writing.
    print("Warming system-prompt cache with one sequential call...", flush=True)
    warmup_pred = await _evaluate_one(cases[0], validator, asyncio.Semaphore(1))
    print(f"  warmup verdict on '{warmup_pred.id}': {warmup_pred.predicted}")

    sem = asyncio.Semaphore(CONCURRENCY)
    print(f"Running remaining {len(cases) - 1} cases (concurrency={CONCURRENCY})...", flush=True)
    rest = await asyncio.gather(*[_evaluate_one(case, validator, sem) for case in cases[1:]])
    predictions = [warmup_pred, *rest]

    target_met = _print_metrics(predictions)
    return 0 if target_met else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
