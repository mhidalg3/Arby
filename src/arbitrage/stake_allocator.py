"""Stake allocation for N-leg Dutch books.

Separated from `dutch_book.py` so the detector stays focused on the arb
decision (overround, margin gate, profit/ROI accounting) while the mechanical
problem of "how much do we put on each leg given odds, a budget, and platform
constraints" lives here and can be tested in isolation.

This module implements ONE allocation strategy:

    `allocate_maxmin` — maximize the worst-case (guaranteed) profit. Under no
    binding cap this is the textbook equal-payout allocation. Under a binding
    cap it scales every leg by the tightest cap ratio, preserving equal payout
    at a lower magnitude. Capital not deployed under this strategy is the
    price of preserving the worst-case guarantee.

A "max capital utilization" allocator (asymmetric payouts; pushes uncapped
legs further while keeping every outcome profitable) was considered and
explicitly rejected for the deterministic arb path: it raises capital usage
only by lowering the worst-case profit, which contradicts the point of a
Dutch book. If a future module wants that tradeoff, add it here next to
`allocate_maxmin` rather than mutating this one.

Precondition: the caller is responsible for verifying `sum(1 / o_i) < 1`
before calling. The math here computes valid stakes only when an arb exists;
calling on a non-arb set returns numbers but they will not represent a hedge.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from src.arbitrage.quotes import OddsQuote


def allocate_maxmin(
    quotes: Sequence[OddsQuote],
    budget: float,
) -> tuple[float, ...] | None:
    """Compute MaxMin stakes for an N-leg Dutch book.

    Pipeline:
        1. Equal-payout allocation: s_i = budget / (o_i * overround). With
           uncapped, unrounded math this consumes the full budget and yields
           an identical payout on every outcome.
        2. Liquidity cap scaling: find the tightest binding `max_stake` and
           scale every leg by the same factor. Scaling only one leg breaks
           the equal-payout property and creates directional exposure, so the
           hedge requires uniform scaling.
        3. Round each leg DOWN to its `stake_increment`. Rounding down can
           never violate `max_stake`. It may break equal payout slightly; the
           caller compensates by computing realized profit as
           `min(payouts) - total_stake`.
        4. Reject (return None) if any leg's rounded stake falls below its
           `min_stake`, or if every stake rounded to zero. There is no
           proportional rescue that preserves the hedge in either case.

    Args:
        quotes: Two or more `OddsQuote`s, one per cell of the partition.
        budget: Total capital available to allocate across all legs.

    Returns:
        Per-leg stakes (same order as `quotes`) or None when placement
        constraints make the bet unplaceable.
    """
    overround = sum(1.0 / q.decimal_odds for q in quotes)
    stakes = [budget / (q.decimal_odds * overround) for q in quotes]

    scale = 1.0
    for q, s in zip(quotes, stakes, strict=True):
        if q.max_stake is not None and s > q.max_stake:
            scale = min(scale, q.max_stake / s)
    if scale < 1.0:
        stakes = [s * scale for s in stakes]

    for i, q in enumerate(quotes):
        if q.stake_increment is not None:
            stakes[i] = math.floor(stakes[i] / q.stake_increment) * q.stake_increment

    for q, s in zip(quotes, stakes, strict=True):
        if q.min_stake is not None and s < q.min_stake:
            return None

    if sum(stakes) <= 0:
        return None

    return tuple(stakes)
