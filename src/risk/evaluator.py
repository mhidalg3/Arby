"""Deterministic risk evaluator.

Takes an `ArbitrageOpportunity` from the detector and produces a
`RiskDecision`. Pure function — no I/O, no state. The daemon owns
Redis interaction; this module owns the logic.

Rules run in a cascade. The first failure short-circuits; the
returned `RiskDecision.rules_evaluated` records which rules ran
before the cascade exited. This gives auditable visibility into
why an opportunity was rejected without resorting to free-text
reasons.

Rules in order:

1. **margin_min** — `realized_roi_pct >= policy.min_margin_pct`
2. **margin_max** — `realized_roi_pct <= policy.max_margin_pct`
3. **distinct_platforms** — `len({leg.platform}) >= policy.min_distinct_platforms`
4. **stake_per_leg** — every leg stake is within `default_max_stake_per_leg_ars`
   when the leg's `max_stake` is None (which is currently always the
   case until logged-in recon captures bookmaker limits).
5. **confidence** — product of per-leg platform reliability ≥ `policy.min_confidence`

If all rules pass: `Verdict.APPROVED`.

The previous `stake_total` rule (arbitrary 2000 ARS cap) was removed
when stake sizing moved into the detector. Total stake per
opportunity is now bounded by the stake-sizing policy
(`max_fraction_per_arb × total_capital`), not by an evaluator gate.

The `high_margin_warning` flag is set when realized ROI is in the
`[high_margin_warning_pct, max_margin_pct]` band — these arbs PASS
the cascade but the operator should manually verify before placing.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.risk.decision import RiskDecision, Verdict
from src.risk.policy import RiskPolicy


@dataclass
class RiskEvaluator:
    """Stateless evaluator. One instance can serve many evaluations
    concurrently — `evaluate` doesn't mutate any state."""

    policy: RiskPolicy

    def evaluate(
        self, opp: ArbitrageOpportunity, now: float | None = None
    ) -> RiskDecision:
        """Run the rule cascade. Return the decision."""
        evaluated_at = now if now is not None else time.time()
        rules_evaluated: list[str] = []

        # Derive the audit fields once. legs[0].market_id is the
        # canonical market_id assigned by the canonicalizer
        # (`<fixture_id>|<code>[|<line>]`).
        market_id = opp.legs[0].market_id if opp.legs else ""
        fixture_id = market_id.split("|", 1)[0] if "|" in market_id else market_id
        platforms = tuple(sorted({leg.platform for leg in opp.legs}))
        confidence = self._compute_confidence(opp)
        high_margin_warning = (
            opp.realized_roi_pct >= self.policy.high_margin_warning_pct
        )

        def _reject(reason: str) -> RiskDecision:
            return RiskDecision(
                verdict=Verdict.REJECTED,
                reason=reason,
                rules_evaluated=tuple(rules_evaluated),
                confidence=confidence,
                high_margin_warning=high_margin_warning,
                fixture_id=fixture_id,
                market_id=market_id,
                realized_roi_pct=opp.realized_roi_pct,
                platforms=platforms,
                evaluated_at=evaluated_at,
            )

        # Rule 1: minimum margin
        rules_evaluated.append("margin_min")
        if opp.realized_roi_pct < self.policy.min_margin_pct:
            return _reject(
                f"realized_roi_pct={opp.realized_roi_pct:.3f}% "
                f"below min={self.policy.min_margin_pct}%"
            )

        # Rule 2: maximum margin (sanity ceiling)
        rules_evaluated.append("margin_max")
        if opp.realized_roi_pct > self.policy.max_margin_pct:
            return _reject(
                f"realized_roi_pct={opp.realized_roi_pct:.3f}% "
                f"above max={self.policy.max_margin_pct}% "
                f"(likely artifact / book won't honor)"
            )

        # Rule 3: distinct platforms — reject single-platform "arbs"
        rules_evaluated.append("distinct_platforms")
        if len(platforms) < self.policy.min_distinct_platforms:
            return _reject(
                f"only {len(platforms)} distinct platform(s) "
                f"(need ≥ {self.policy.min_distinct_platforms}); "
                f"single-bookmaker arbs are typically transient mispricings "
                f"the book closes before placement"
            )

        # Rule 4: per-leg stake feasibility (was rule 5 before
        # `stake_total` was removed in favor of detector-side sizing).
        rules_evaluated.append("stake_per_leg")
        per_leg_cap = self.policy.default_max_stake_per_leg_ars
        for leg, stake in zip(opp.legs, opp.stakes, strict=True):
            effective_cap = leg.max_stake if leg.max_stake is not None else per_leg_cap
            if not math.isfinite(stake) or stake <= 0:
                return _reject(
                    f"non-positive stake {stake} on {leg.platform}/{leg.outcome}"
                )
            if stake > effective_cap:
                return _reject(
                    f"leg stake {stake:.2f} on {leg.platform}/{leg.outcome} "
                    f"exceeds per-leg cap {effective_cap:.2f}"
                )

        # Rule 5: confidence (product of per-platform reliability)
        rules_evaluated.append("confidence")
        if confidence < self.policy.min_confidence:
            return _reject(
                f"confidence={confidence:.3f} below "
                f"min={self.policy.min_confidence}"
            )

        # All rules passed.
        return RiskDecision(
            verdict=Verdict.APPROVED,
            reason="approved",
            rules_evaluated=tuple(rules_evaluated),
            confidence=confidence,
            high_margin_warning=high_margin_warning,
            fixture_id=fixture_id,
            market_id=market_id,
            realized_roi_pct=opp.realized_roi_pct,
            platforms=platforms,
            evaluated_at=evaluated_at,
        )

    def _compute_confidence(self, opp: ArbitrageOpportunity) -> float:
        """Product of per-leg platform reliability factors.

        Unknown platforms default to
        `policy.default_unknown_platform_reliability`. An opportunity
        with N legs from N high-trust platforms yields confidence
        close to 1; mixing in a low-trust platform multiplies it down.
        """
        if not opp.legs:
            return 0.0
        score = 1.0
        for leg in opp.legs:
            factor = self.policy.platform_reliability.get(
                leg.platform, self.policy.default_unknown_platform_reliability
            )
            score *= factor
        return score
