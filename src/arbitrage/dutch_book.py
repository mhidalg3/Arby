"""Dutch book arbitrage detection.

Given two complementary bets (where P(A∪B)=1 and P(¬A∧¬B)=0), determine
whether the implied probabilities sum to less than 1, and if so compute
the optimal stake allocation that guarantees a profit regardless of outcome.

This module is intentionally pure: no I/O, no LLM calls, no async. It is
the deterministic financial core that every other component depends on.

Caller responsibilities:
    - Verify that the two outcomes form a valid partition (handled upstream
      by the semantic layer's partition validator).
    - Verify that the two quotes refer to the same underlying match.
    - Verify that the two quotes are from different platforms.

This module does not validate those preconditions because doing so would
require coupling to the storage layer. Tests in tests/unit/test_dutch_book.py
exercise the math; integration tests verify the full pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OddsQuote:
    """A single odds quotation from one platform on one outcome."""

    platform: str
    market_id: str  # canonical market identifier, same across platforms
    outcome: str  # canonical outcome identifier within the market
    decimal_odds: float
    max_stake: float | None  # platform-imposed liquidity cap; None = unbounded
    timestamp: float  # unix epoch seconds, for staleness checks upstream


@dataclass(frozen=True)
class ArbitrageOpportunity:
    """A detected Dutch book opportunity with computed optimal allocation."""

    leg_a: OddsQuote
    leg_b: OddsQuote
    stake_a: float
    stake_b: float
    total_stake: float
    guaranteed_profit: float
    margin_pct: float


def detect_arbitrage(
    quote_a: OddsQuote,
    quote_b: OddsQuote,
    budget: float,
    min_margin_pct: float = 1.0,
) -> ArbitrageOpportunity | None:
    """Detect a Dutch book opportunity between two complementary bets.

    Args:
        quote_a: Odds quote for outcome A from platform X.
        quote_b: Odds quote for outcome B (the complement of A) from platform Y.
        budget: Total capital available to allocate across both legs.
        min_margin_pct: Minimum margin (in percent) below which we return None
            even though an arb mathematically exists. This filters out
            opportunities too small to overcome fees and slippage.

    Returns:
        An ArbitrageOpportunity with optimal stake allocation if an arb
        exists and meets the margin threshold; None otherwise.

    Raises:
        ValueError: If decimal odds are not strictly greater than 1.0, or
            if budget is non-positive.
    """
    if quote_a.decimal_odds <= 1.0 or quote_b.decimal_odds <= 1.0:
        raise ValueError(
            f"Decimal odds must be > 1.0; got {quote_a.decimal_odds=}, "
            f"{quote_b.decimal_odds=}"
        )
    if budget <= 0:
        raise ValueError(f"Budget must be positive; got {budget=}")

    # Dutch book condition: implied probabilities sum to less than 1.
    overround = 1.0 / quote_a.decimal_odds + 1.0 / quote_b.decimal_odds

    if overround >= 1.0:
        return None

    margin_pct = (1.0 - overround) * 100.0
    if margin_pct < min_margin_pct:
        return None

    # Optimal allocation: stake each leg so that the payout is identical
    # regardless of which outcome occurs. Derivation:
    #   payout_a = stake_a * odds_a  must equal  payout_b = stake_b * odds_b
    #   stake_a + stake_b = budget
    # Solving: stake_a = budget / (odds_a * overround)
    stake_a = budget / (quote_a.decimal_odds * overround)
    stake_b = budget / (quote_b.decimal_odds * overround)

    # Apply liquidity caps. If a platform won't accept the optimal stake,
    # we must scale BOTH legs down proportionally to maintain the hedge.
    # Scaling only one leg breaks the equal-payout property and creates
    # directional exposure.
    if quote_a.max_stake is not None and stake_a > quote_a.max_stake:
        scale = quote_a.max_stake / stake_a
        stake_a *= scale
        stake_b *= scale

    if quote_b.max_stake is not None and stake_b > quote_b.max_stake:
        scale = quote_b.max_stake / stake_b
        stake_a *= scale
        stake_b *= scale

    total_stake = stake_a + stake_b
    # Payout is equal on both legs by construction, so we can compute profit
    # from either side.
    guaranteed_profit = stake_a * quote_a.decimal_odds - total_stake

    return ArbitrageOpportunity(
        leg_a=quote_a,
        leg_b=quote_b,
        stake_a=stake_a,
        stake_b=stake_b,
        total_stake=total_stake,
        guaranteed_profit=guaranteed_profit,
        margin_pct=margin_pct,
    )
