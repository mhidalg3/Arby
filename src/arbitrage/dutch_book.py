"""Dutch book arbitrage detection.

Given a set of mutually exclusive and exhaustive outcomes (a partition) priced
across one or more platforms, determine whether the implied probabilities sum
to less than 1 and, if so, return the allocation that locks in a profit
regardless of which outcome occurs.

The detector is N-way: it accepts a sequence of `OddsQuote`s of length ≥ 2.
For Argentine soccer the common shapes are:
    - 2-way: BTTS yes/no, over/under, draw-no-bet
    - 3-way: 1X2 (home win / draw / away win)
    - 4+    : Asian handicap quarter-lines and similar

This module is intentionally pure: no I/O, no LLM calls, no async. It is the
deterministic financial core that every other component depends on. The
mechanical "how much do we put on each leg" problem lives in
`src.arbitrage.stake_allocator`; this file is responsible for deciding whether
an arb exists, what its theoretical and realized margins are, and packaging
the result.

Caller responsibilities:
    - Verify that the supplied quotes form a valid partition (handled upstream
      by the semantic layer's partition validator).
    - Verify that all quotes refer to the same underlying match.
    - Verify that no two quotes cover the same outcome (one quote per partition
      cell). This module does not deduplicate.

These preconditions are not checked here because doing so would require
coupling to the storage and semantic layers. Tests in
tests/unit/test_dutch_book.py exercise the math; integration tests verify the
full pipeline.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from src.arbitrage.quotes import OddsQuote
from src.arbitrage.stake_allocator import allocate_maxmin

# Re-exported so callers (and tests) can keep importing OddsQuote from here.
__all__ = ["ArbitrageOpportunity", "OddsQuote", "detect_arbitrage"]


@dataclass(frozen=True)
class ArbitrageOpportunity:
    """A detected Dutch book opportunity with computed optimal allocation.

    `margin_pct` is the theoretical margin intrinsic to the odds
    (`(1 - overround) * 100`). `realized_roi_pct` is the actual return on the
    capital we will deploy after liquidity caps and stake rounding have been
    applied; it can be strictly lower than `margin_pct` when rounding breaks
    the equal-payout property. Both are reported so callers can distinguish a
    soft market from a placement-constrained one.
    """

    legs: tuple[OddsQuote, ...]
    stakes: tuple[float, ...]
    total_stake: float
    guaranteed_profit: float
    margin_pct: float
    realized_roi_pct: float
    capital_utilization: float


def detect_arbitrage(
    quotes: Sequence[OddsQuote],
    budget: float,
    min_margin_pct: float = 1.0,
) -> ArbitrageOpportunity | None:
    """Detect a Dutch book opportunity across N complementary quotes.

    Args:
        quotes: Two or more odds quotes, one per cell of the partition.
        budget: Total capital available to allocate across all legs.
        min_margin_pct: Minimum realized ROI (in percent) below which we
            return None even when an arb mathematically exists. Filters out
            opportunities too small to overcome fees and slippage.

    Returns:
        An ArbitrageOpportunity with the optimal allocation if an arb exists,
        meets the margin threshold, and is placeable under each platform's
        min_stake / stake_increment constraints; otherwise None.

    Raises:
        ValueError: If fewer than two quotes are supplied, any decimal odds
            are not strictly greater than 1.0, budget is non-positive, or any
            `stake_increment` is non-positive.
    """
    if len(quotes) < 2:
        raise ValueError(f"At least two quotes required; got {len(quotes)}")
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError(f"Budget must be a finite positive value; got {budget=}")
    for q in quotes:
        if not math.isfinite(q.decimal_odds) or q.decimal_odds <= 1.0:
            raise ValueError(
                f"Decimal odds must be a finite value > 1.0; got {q.decimal_odds=} "
                f"on {q.platform}/{q.outcome}"
            )
        if q.max_stake is not None and (not math.isfinite(q.max_stake) or q.max_stake <= 0):
            raise ValueError(
                f"max_stake must be a finite positive value when set; got "
                f"{q.max_stake=} on {q.platform}/{q.outcome}"
            )
        if q.min_stake is not None and (not math.isfinite(q.min_stake) or q.min_stake < 0):
            raise ValueError(
                f"min_stake must be a finite non-negative value when set; got "
                f"{q.min_stake=} on {q.platform}/{q.outcome}"
            )
        if q.stake_increment is not None and (
            not math.isfinite(q.stake_increment) or q.stake_increment <= 0
        ):
            raise ValueError(
                f"stake_increment must be a finite positive value when set; got "
                f"{q.stake_increment=} on {q.platform}/{q.outcome}"
            )

    # Dutch book condition: implied probabilities sum to less than 1.
    overround = sum(1.0 / q.decimal_odds for q in quotes)
    if overround >= 1.0:
        return None

    margin_pct = (1.0 - overround) * 100.0
    # Cheap early exit. Realized ROI is checked again after allocation because
    # stake rounding can erode it further.
    if margin_pct < min_margin_pct:
        return None

    stakes = allocate_maxmin(quotes, budget)
    if stakes is None:
        return None

    total_stake = sum(stakes)
    # After rounding the legs no longer pay out equally; the guaranteed profit
    # is bounded by whichever outcome pays the least.
    payouts = [s * q.decimal_odds for q, s in zip(quotes, stakes, strict=True)]
    guaranteed_profit = min(payouts) - total_stake
    realized_roi_pct = guaranteed_profit / total_stake * 100.0
    if realized_roi_pct < min_margin_pct:
        return None

    return ArbitrageOpportunity(
        legs=tuple(quotes),
        stakes=stakes,
        total_stake=total_stake,
        guaranteed_profit=guaranteed_profit,
        margin_pct=margin_pct,
        realized_roi_pct=realized_roi_pct,
        capital_utilization=total_stake / budget,
    )
