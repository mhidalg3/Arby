"""Risk-layer policy — tunable thresholds + per-platform confidence scores.

All financial decisions in arby pass through `src/risk/`. The
policy here encodes the assumptions the operator makes about
which detected arbitrage opportunities are worth executing.

Two categories of policy parameters:

1. **Hard rules** (`min_margin_pct`, `min_distinct_platforms`,
   `max_total_stake`, etc.) — bright-line filters. A detected
   opportunity that fails any hard rule is REJECTED regardless of
   other factors.
2. **Soft scoring** (`platform_reliability`) — per-platform trust
   factors multiplied together to produce an opportunity
   confidence. Below `min_confidence` the opportunity is rejected.

Defaults are tuned from the 30-minute live capture on 2026-05-26:

- **min_margin_pct = 0.5** — matches the threshold the detector
  uses; below this there isn't enough margin to overcome
  bookmaker spread/limiting risk.
- **max_margin_pct = 25.0** — the captured distribution maxed at
  19.18% (Fluminense vs Bolivar) and persisted for 27 minutes,
  so a ceiling of 25% lets persistent high-margin arbs through
  while flagging the truly insane >25% as artifacts.
- **high_margin_warning_pct = 10.0** — between 10-25% the arb is
  flagged in the decision record. Operator should manually verify
  before executing.
- **Per-platform reliability** — set conservatively. **Bplay**
  (SportNCO) and **BetWarrior** (Kambi) get 1.0 (sharp pricing,
  expected to honor winning bets within stake limits). **Betsson**
  (OBG, recreational-skewed) gets 0.8 pending a logged-in recon
  to capture their stake-limit policy; their looseness on arb
  legs raises the (modest) risk of post-hoc limiting or voiding.

These are not platform critiques — they're operational priors. As
real bet-placement data accumulates, the operator should retune.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from src.risk.reliability import (
    DEFAULT_PLATFORM_RELIABILITY,
    DEFAULT_UNKNOWN_PLATFORM_RELIABILITY,
)

# Default per-platform per-leg stake cap (ARS). Real Argentine
# bookmaker limits per-leg are typically 10k–100k ARS depending on
# the market and the account's history with the platform. Without
# a logged-in recon to capture the actual values, 50,000 ARS is the
# operator's informed prior — high enough that real arbs can be
# sized meaningfully, conservative enough that hitting it on a
# winning bet doesn't draw immediate attention.
_DEFAULT_MAX_STAKE_PER_LEG_ARS: Final[float] = 50_000.0


@dataclass(frozen=True)
class RiskPolicy:
    """Risk-evaluator configuration. Frozen — change requires
    constructing a new policy instance.

    Note: stake SIZING (computing the budget for an opportunity) is
    NOT this policy's responsibility — that's
    `src/risk/stake_sizing.py`. This policy only encodes the GATE
    rules: margin band, distinct-platform requirement, per-leg
    feasibility, and the confidence floor. The detector embeds
    sized stakes BEFORE this policy ever sees the opportunity.
    """

    # Margin gates
    min_margin_pct: float = 0.5
    max_margin_pct: float = 25.0
    high_margin_warning_pct: float = 10.0

    # Diversity / partition requirements
    min_distinct_platforms: int = 2

    # Confidence floor (product of per-leg reliability scores)
    min_confidence: float = 0.5

    # Per-leg feasibility — bookmaker-imposed max stake. When the
    # snapshot's `max_stake` is None (current state, until
    # logged-in stake-limit recon), this default applies.
    default_max_stake_per_leg_ars: float = _DEFAULT_MAX_STAKE_PER_LEG_ARS

    # Per-platform reliability (`platform_name → factor in [0, 1]`).
    # Source-of-truth lives in `reliability.py`; default factory
    # copies the shared dict so per-instance mutations don't leak.
    platform_reliability: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PLATFORM_RELIABILITY)
    )

    # Fallback reliability for platforms not in the table.
    default_unknown_platform_reliability: float = DEFAULT_UNKNOWN_PLATFORM_RELIABILITY
