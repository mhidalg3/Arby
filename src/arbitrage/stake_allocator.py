"""Stake allocation for N-leg Dutch books.

Separated from `dutch_book.py` so the detector stays focused on the arb
decision (overround, margin gate, profit/ROI accounting) while the mechanical
problem of "how much do we put on each leg given odds, a budget, and platform
constraints" lives here and can be tested in isolation.

This module implements TWO allocation strategies:

    `allocate_maxmin` — maximize the worst-case (guaranteed) profit for a
    NOTHING-PLACED Dutch book. Under no binding cap this is the textbook
    equal-payout allocation. Under a binding cap it scales every leg by the
    tightest cap ratio, preserving equal payout at a lower magnitude. Capital
    not deployed under this strategy is the price of preserving the worst-case
    guarantee.

    `allocate_residual` — size the REMAINING legs around already-filled legs
    (sunk exposure) so every outcome — placed or remaining — still pays out at
    least a common target. Used by the executor's odds-change recapture path
    (a BetWarrior/Kambi reject or mid-sequence drift) to salvage a hedge that
    the nothing-placed re-detector (`allocate_maxmin` via `detect_arbitrage`)
    cannot express, because it always re-runs from a fresh budget.

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
from dataclasses import dataclass

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


@dataclass(frozen=True)
class ResidualAllocation:
    """Residual hedge around already-filled legs: stakes for the remaining legs.

    The placed legs are sunk exposure (fixed stake × odds → fixed payout). The
    allocation sizes the REMAINING legs so every outcome — placed or remaining —
    pays out at least a common target, locking a guaranteed profit around the
    live exposure instead of leaving it naked.
    """

    stakes: tuple[float, ...]  # one per quote, same order
    total_stake: float  # placed_stake_total + sum(stakes)
    guaranteed_profit: float  # min over ALL outcomes' payouts − total_stake


def allocate_residual(
    placed_payouts: Sequence[float],
    placed_stake_total: float,
    quotes: Sequence[OddsQuote],
    budget_remaining: float,
    min_profit_ars: float = 0.0,
) -> ResidualAllocation | None:
    """Size the remaining legs of a Dutch book around already-filled legs.

    Closed-form MaxMin (no LP): the worst-case outcome is already pinned by the
    placed legs (their payouts are fixed), so the optimal common payout target
    for the remaining outcomes is the largest target every remaining leg can
    reach without exceeding its liquidity cap or the remaining budget, and never
    above the lowest placed payout (a higher target only wastes stake — the worst
    case is already capped by a placed leg):

        C  = min( min(placed_payouts),
                  budget_remaining / Σ(1/o_j),
                  min over capped j of max_stake_j · o_j )
        s_j = floor(C / o_j, stake_increment)

    Args:
        placed_payouts: ``stake_filled × odds_filled`` for each already-live leg
            (fixed exposure; one entry per live leg).
        placed_stake_total: sum of the live legs' filled stakes (sunk capital).
        quotes: the NOT-yet-placed legs at their FRESH ``decimal_odds``. These
            MUST cover exactly the partition cells NOT covered by the placed legs,
            in the same order the caller will place them.
        budget_remaining: per-arb budget − ``placed_stake_total``.
        min_profit_ars: floor on ``guaranteed_profit``; default ``0.0`` — once
            money is live, any locked non-negative outcome beats naked exposure.

        A ``ResidualAllocation`` (per-leg stakes, total stake, guaranteed profit), or
        None when no salvageable hedge exists: the remaining budget exhausted, a stake
        rounding to zero, a leg whose on-grid ``min_stake`` exceeds its cap or the
        remaining budget, or the guaranteed profit below ``min_profit_ars`` (which
        includes the no-arb case where a high overround drives profit negative — no
        separate ``Σ(1/o) ≥ 1`` branch). A below-``min_stake`` equal-payout stake is
        bumped up to the leg's on-grid minimum (over-hedging that leg) rather than
        rejected, so a salvageable mid-sequence reject is recovered instead of going
        naked.

    Raises:
        ValueError: mirroring ``detect_arbitrage``'s malformed-input contract —
            empty ``placed_payouts`` or any ≤ 0; empty ``quotes``; any
            ``decimal_odds`` non-finite or ≤ 1.0; any set ``max_stake`` /
            ``min_stake`` / ``stake_increment`` non-finite or out of range; or a
            non-finite ``budget_remaining``.
    """
    if not placed_payouts:
        raise ValueError("placed_payouts must be non-empty")
    if not quotes:
        raise ValueError("quotes must be non-empty")
    for p in placed_payouts:
        if not math.isfinite(p) or p <= 0.0:
            raise ValueError(f"placed payouts must be finite positive; got {p}")
    if not math.isfinite(budget_remaining):
        raise ValueError(f"budget_remaining must be finite; got {budget_remaining=}")
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

    if budget_remaining <= 0.0:
        return None

    placed_floor = min(placed_payouts)
    inv = sum(1.0 / q.decimal_odds for q in quotes)
    # Common payout target: never above the lowest placed payout (a higher target
    # only wastes stake — the worst case is already capped by a placed leg), never
    # above what the remaining budget reaches at equal payout, and never above the
    # tightest per-leg cap (so s_j = C/o_j ≤ max_stake_j).
    target = min(placed_floor, budget_remaining / inv)
    for q in quotes:
        if q.max_stake is not None:
            target = min(target, q.max_stake * q.decimal_odds)

    stakes = [target / q.decimal_odds for q in quotes]
    for i, q in enumerate(quotes):
        if q.stake_increment is not None:
            stakes[i] = math.floor(stakes[i] / q.stake_increment) * q.stake_increment

    for i, q in enumerate(quotes):
        s = stakes[i]
        if s <= 0.0:
            return None
        if q.min_stake is not None and s < q.min_stake:
            # Over-hedge this leg at its minimum PLACEABLE stake rather than reject the
            # whole salvage: the bump adds cost only on this leg's outcome, so the worst
            # case (pinned by the placed legs / the unbumped legs) is unchanged and the
            # allocation stays non-loss-making as long as the budget + guaranteed-profit
            # checks below pass. Rejecting here would turn a salvageable mid-sequence
            # reject into naked exposure (the residual-recapture caller treats None as no
            # salvage). Round the minimum UP to stake_increment so the book accepts it.
            bumped = q.min_stake
            if q.stake_increment is not None:
                bumped = math.ceil(bumped / q.stake_increment) * q.stake_increment
            if q.max_stake is not None and bumped > q.max_stake:
                return None  # on-grid minimum exceeds the cap → genuinely infeasible
            stakes[i] = bumped

    remaining_stake = sum(stakes)
    if remaining_stake > budget_remaining:
        return None
    remaining_floor = min(s * q.decimal_odds for q, s in zip(quotes, stakes, strict=True))
    total_stake = placed_stake_total + remaining_stake
    guaranteed_profit = min(placed_floor, remaining_floor) - total_stake

    if guaranteed_profit < min_profit_ars:
        return None

    return ResidualAllocation(
        stakes=tuple(stakes),
        total_stake=total_stake,
        guaranteed_profit=guaranteed_profit,
    )
