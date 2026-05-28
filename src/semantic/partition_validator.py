"""End-to-end partition validator: pre-filter → LLM escalation.

Composes the cheap deterministic pre-filter
(`src.semantic.partition_filter`) with the LLM validator
(`src.semantic.llm_validator`). For every case:

  1. Run the rule-based pre-filter. If it returns VALID or INVALID with
     a named rule, that is the final verdict — no LLM call.
  2. If the pre-filter returns UNKNOWN, escalate to the LLM validator.

The result carries enough provenance (which layer decided, what reason)
to feed the `partition_validations` audit log without further lookup.

This module does not own retry, rate-limiting, or batching policy — that
belongs in the calling layer once we have production traffic shape data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.semantic.llm_validator import LLMValidator, LLMVerdict
from src.semantic.partition_filter import MatchContext, Verdict, classify


@dataclass(frozen=True)
class PartitionDecision:
    """Final decision after pre-filter (and possibly LLM)."""

    verdict: Verdict  # VALID or INVALID — never UNKNOWN
    source: Literal["pre_filter", "llm"]
    reason: str
    llm_verdict: LLMVerdict | None  # full LLM record when escalated; else None


@dataclass(frozen=True)
class PartitionValidator:
    """Pre-filter first, escalate UNKNOWN to LLM. Injected `LLMValidator`
    so production code passes a real `AsyncAnthropic`-backed instance and
    tests pass a mock."""

    llm_validator: LLMValidator

    async def validate(
        self,
        desc_a: str,
        desc_b: str,
        context: MatchContext,
    ) -> PartitionDecision:
        pre_verdict, pre_reason = classify(desc_a, desc_b, context)
        if pre_verdict is not Verdict.UNKNOWN:
            return PartitionDecision(
                verdict=pre_verdict,
                source="pre_filter",
                reason=pre_reason,
                llm_verdict=None,
            )

        llm_result = await self.llm_validator.validate(desc_a, desc_b, context)
        return PartitionDecision(
            verdict=llm_result.verdict,
            source="llm",
            reason=llm_result.reasoning,
            llm_verdict=llm_result,
        )
