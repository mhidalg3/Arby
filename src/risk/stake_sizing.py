"""Deterministic stake sizing — budget per arbitrage opportunity.

Computes the BUDGET (total capital committed) for a single
arbitrage opportunity given:

  1. **Total capital** — the operator-supplied bankroll across all
     platforms (ARS).
  2. **Per-arb cap** — Kelly-style fraction limiting any single
     opportunity to a small portion of the bankroll.
  3. **Confidence** — product of per-platform reliability factors
     for the legs of this opportunity. Low confidence shrinks the
     budget proportionally.

The budget is then passed to `arbitrage.dutch_book.detect_arbitrage`
which computes the per-leg stakes via the max-min allocator. The
returned `ArbitrageOpportunity` has those stakes baked in; the risk
daemon then gates on per-leg feasibility (bookmaker max_stake), not
on an arbitrary total-stake cap.

Why no margin scaling: for arbs (guaranteed positive ROI), the
"size your bet by edge" intuition from Kelly betting on uncertain
outcomes doesn't apply directly. Higher margin = more PROFIT on the
same risked capital, not justification for more risk. We commit a
fixed fraction (subject to confidence discount); the margin
determines the win, not the bet.

Why confidence multiplier instead of LLM/scoring: the operator's
prior about bookmaker honesty IS the risk signal. Bplay+BetWarrior
are sharper (1.0 each); Betsson is the loose-side outlier (0.8).
Real-world bet-placement outcomes should refine these factors over
time. Until then, a simple multiplicative model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from src.arbitrage.quotes import OddsQuote
from src.risk.reliability import (
    DEFAULT_PLATFORM_RELIABILITY,
    DEFAULT_UNKNOWN_PLATFORM_RELIABILITY,
)

# Default operator bankroll. ~$1,000 USD at ~1,000 ARS/USD; tune via
# env in production. Real deployments should set this higher with
# more aggressive `max_fraction_per_arb` once stake-limit recon
# confirms bookmaker per-leg ceilings.
DEFAULT_TOTAL_CAPITAL_ARS: float = 1_000_000.0


@dataclass(frozen=True)
class StakeSizingPolicy:
    """Configuration for the stake sizer. Frozen — construct a new
    instance to change policy at runtime."""

    # Operator bankroll in ARS. The denominator the per-arb cap is
    # taken against. Must be > 0.
    total_capital_ars: float = DEFAULT_TOTAL_CAPITAL_ARS

    # Maximum fraction of bankroll committable to any single
    # opportunity. 0.05 = 5% — Kelly-style cap that limits exposure
    # per fixture/market. With 1M ARS capital, that's 50k ARS per
    # arb maximum.
    max_fraction_per_arb: float = 0.05

    # Minimum confidence (product of per-platform reliability)
    # below which budget = 0 (skip). Note: the RISK EVALUATOR
    # applies its own `min_confidence` gate downstream; this is
    # just a sizer-side short-circuit so we don't even compute
    # tiny budgets for low-confidence opportunities.
    min_confidence: float = 0.5

    # Minimum total stake worth placing. Arbs below this size are
    # better skipped — placement overhead, fees, and rounding
    # erosion dominate small absolute profits.
    min_total_stake_ars: float = 500.0

    # Per-platform reliability table (shared with `RiskPolicy`).
    platform_reliability: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PLATFORM_RELIABILITY)
    )
    default_unknown_platform_reliability: float = DEFAULT_UNKNOWN_PLATFORM_RELIABILITY


@dataclass
class StakeSizer:
    """Stateless: one instance can serve many `compute_budget` calls.

    Constructed once at daemon startup with a `StakeSizingPolicy`.
    The detector injects `compute_budget` as its `budget_fn` so each
    detected opportunity is sized at the moment of detection.
    """

    policy: StakeSizingPolicy

    def compute_budget(self, quotes: Sequence[OddsQuote]) -> float:
        """Return the budget (ARS) for an opportunity with these
        leg quotes. Returns 0.0 to signal "skip this opportunity".

        Pure function of (quotes, policy) — no I/O, no state.
        """
        if not quotes:
            return 0.0
        confidence = self._confidence(quotes)
        if confidence < self.policy.min_confidence:
            return 0.0
        budget = self.policy.total_capital_ars * self.policy.max_fraction_per_arb * confidence
        if budget < self.policy.min_total_stake_ars:
            return 0.0
        return budget

    def _confidence(self, quotes: Sequence[OddsQuote]) -> float:
        """Product of per-platform reliability factors across the
        legs. Identical model to `RiskEvaluator._compute_confidence`
        — kept duplicated rather than imported because importing
        from `risk.evaluator` would create a tight coupling between
        sizing and evaluation. The reliability table is shared via
        `risk.reliability`, which is the real source of truth."""
        score = 1.0
        for q in quotes:
            factor = self.policy.platform_reliability.get(
                q.platform, self.policy.default_unknown_platform_reliability
            )
            score *= factor
        return score
