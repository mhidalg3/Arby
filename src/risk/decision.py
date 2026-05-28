"""Risk-decision types — the output shape of the evaluator.

`RiskDecision` is the audit record produced for EVERY detected
opportunity, approved or rejected. The downstream execution agent
filters on `verdict == Verdict.APPROVED`. Operators read the full
stream (`arb:risk_decisions`) to debug rejection patterns.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    """Risk-layer decision outcome."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class RiskDecision:
    """Audit record for one (opportunity, evaluation) pair.

    Carried fields:
        verdict: APPROVED or REJECTED.
        reason: The first rule that decided the outcome — for
            rejections, the rule that failed; for approvals,
            "approved". Stable string keys for log-grepping.
        rules_evaluated: All rule names that ran (in order),
            so an audit consumer can see how far the cascade
            got before exiting.
        confidence: Product of per-platform reliability factors.
            `[0, 1]`. Below `RiskPolicy.min_confidence` triggers
            rejection on the confidence rule.
        high_margin_warning: True when realized_roi_pct exceeds
            `RiskPolicy.high_margin_warning_pct` but is still
            below the rejection ceiling. APPROVED opportunities
            with this flag set should be manually verified before
            placement.
        fixture_id: Carried through from the opportunity for ease
            of joining against `arb:opportunities`.
        market_id: Carried through similarly.
        realized_roi_pct: Echoed for at-a-glance audit reading.
        platforms: The set of distinct platforms in the opportunity.
        evaluated_at: Unix epoch of the evaluation.
    """

    verdict: Verdict
    reason: str
    rules_evaluated: tuple[str, ...]
    confidence: float
    high_margin_warning: bool
    fixture_id: str
    market_id: str
    realized_roi_pct: float
    platforms: tuple[str, ...]
    evaluated_at: float
