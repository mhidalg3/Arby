"""Tests for the stake allocator.

These exercise the allocator in isolation, decoupled from the arb-detection
gating in `dutch_book.py`. They verify that the math (equal-payout, cap
scaling, rounding) and the placement gates (min_stake, all-zero) behave
correctly across 2-way, 3-way, and N-way shapes.
"""

from __future__ import annotations

import pytest

from src.arbitrage.quotes import OddsQuote
from src.arbitrage.stake_allocator import allocate_maxmin, allocate_residual


def make_quote(
    platform: str = "X",
    market_id: str = "m1",
    outcome: str = "A",
    decimal_odds: float = 2.0,
    max_stake: float | None = None,
    timestamp: float = 0.0,
    min_stake: float | None = None,
    stake_increment: float | None = None,
) -> OddsQuote:
    return OddsQuote(
        platform=platform,
        market_id=market_id,
        outcome=outcome,
        decimal_odds=decimal_odds,
        max_stake=max_stake,
        timestamp=timestamp,
        min_stake=min_stake,
        stake_increment=stake_increment,
    )


class TestEqualPayoutBaseline:
    def test_uncapped_two_way_yields_equal_payouts(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None

        payout_a = stakes[0] * a.decimal_odds
        payout_b = stakes[1] * b.decimal_odds
        assert abs(payout_a - payout_b) < 1e-9
        assert abs(sum(stakes) - 1000.0) < 1e-9

    def test_uncapped_three_way_yields_equal_payouts(self) -> None:
        quotes = [
            make_quote(platform="X", decimal_odds=2.60),
            make_quote(platform="Y", decimal_odds=3.50),
            make_quote(platform="Z", decimal_odds=3.20),
        ]

        stakes = allocate_maxmin(quotes, budget=1000.0)
        assert stakes is not None

        payouts = [s * q.decimal_odds for q, s in zip(quotes, stakes, strict=True)]
        assert max(payouts) - min(payouts) < 1e-9
        assert abs(sum(stakes) - 1000.0) < 1e-9

    def test_asymmetric_odds_allocate_more_to_favorite(self) -> None:
        """Lower odds → larger stake share."""
        fav = make_quote(decimal_odds=1.50)
        dog = make_quote(decimal_odds=4.00)

        stakes = allocate_maxmin([fav, dog], budget=1000.0)
        assert stakes is not None
        assert stakes[0] > stakes[1]


class TestLiquidityCapScaling:
    def test_single_cap_scales_all_legs_proportionally(self) -> None:
        a = make_quote(decimal_odds=2.10, max_stake=100.0)
        b = make_quote(decimal_odds=2.10)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None
        assert stakes[0] == pytest.approx(100.0, abs=1e-9)
        # Equal-payout preserved under scaling.
        assert stakes[0] * a.decimal_odds == pytest.approx(stakes[1] * b.decimal_odds, abs=1e-9)

    def test_tightest_of_multiple_caps_drives_the_scale(self) -> None:
        """Three-way 1X2 with two caps; the lower cap dictates the global scale."""
        a = make_quote(platform="X", decimal_odds=3.05, max_stake=200.0)
        b = make_quote(platform="Y", decimal_odds=3.05)
        c = make_quote(platform="Z", decimal_odds=3.05, max_stake=100.0)

        stakes = allocate_maxmin([a, b, c], budget=1000.0)
        assert stakes is not None
        # Optimal uncapped is ~328 each (1000 / (3.05 * 0.9836)). c's cap of
        # 100 is tighter than a's 200, so scale ≈ 100/328 ≈ 0.305.
        assert stakes[2] == pytest.approx(100.0, abs=1e-9)
        assert stakes[0] < 110.0  # would be ~200 if only c's cap bound

        payouts = [s * q.decimal_odds for q, s in zip([a, b, c], stakes, strict=True)]
        assert max(payouts) - min(payouts) < 1e-9

    def test_caps_above_optimal_do_not_bind(self) -> None:
        a = make_quote(decimal_odds=2.10, max_stake=10_000.0)
        b = make_quote(decimal_odds=2.10, max_stake=10_000.0)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None
        assert abs(sum(stakes) - 1000.0) < 1e-9

    def test_capital_idle_when_cap_binds(self) -> None:
        """The MaxMin contract: under a binding cap, capital is left idle to
        preserve the worst-case-profit guarantee. This is intentional."""
        a = make_quote(decimal_odds=2.10, max_stake=50.0)
        b = make_quote(decimal_odds=2.10)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None
        assert sum(stakes) < 200.0  # far below the 1000 budget


class TestStakeIncrement:
    def test_stakes_are_multiples_of_increment(self) -> None:
        a = make_quote(decimal_odds=2.10, stake_increment=10.0)
        b = make_quote(decimal_odds=2.10, stake_increment=5.0)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None
        assert stakes[0] % 10.0 == pytest.approx(0.0, abs=1e-9)
        assert stakes[1] % 5.0 == pytest.approx(0.0, abs=1e-9)

    def test_rounding_is_strictly_down(self) -> None:
        """Rounding direction is the contract that lets us guarantee
        max_stake is never violated by the rounding step itself."""
        a = make_quote(decimal_odds=2.05, max_stake=512.0, stake_increment=25.0)
        b = make_quote(decimal_odds=2.15)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None
        # Optimal uncapped is ~511.8; max_stake of 512 doesn't bind. Rounding
        # 25.0 takes 511.8 → 500 (floor), not 525.
        assert stakes[0] == pytest.approx(500.0, abs=1e-9)
        assert stakes[0] <= 512.0

    def test_all_zero_after_rounding_returns_none(self) -> None:
        a = make_quote(decimal_odds=2.10, stake_increment=10_000.0)
        b = make_quote(decimal_odds=2.10, stake_increment=10_000.0)

        assert allocate_maxmin([a, b], budget=1000.0) is None


class TestMinStakeGate:
    def test_min_stake_violation_returns_none(self) -> None:
        # Heavily asymmetric: leg b's optimal stake is small (~45) against
        # the favorite. A min of 100 on b makes the bet unplaceable.
        a = make_quote(platform="X", decimal_odds=1.05)
        b = make_quote(platform="Y", decimal_odds=22.0, min_stake=100.0)

        assert allocate_maxmin([a, b], budget=1000.0) is None

    def test_min_stake_satisfied_returns_stakes(self) -> None:
        a = make_quote(decimal_odds=2.10, min_stake=50.0)
        b = make_quote(decimal_odds=2.10, min_stake=50.0)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None
        assert stakes[0] >= 50.0
        assert stakes[1] >= 50.0

    def test_min_stake_checked_after_cap_scaling(self) -> None:
        """A binding cap can drop the OTHER leg below its min via proportional
        scaling. That should reject the arb."""
        a = make_quote(decimal_odds=2.10, max_stake=10.0)
        b = make_quote(decimal_odds=2.10, min_stake=50.0)

        assert allocate_maxmin([a, b], budget=1000.0) is None


class TestCapBoundaries:
    def test_cap_exactly_equal_to_optimal_does_not_scale(self) -> None:
        """When `max_stake` is exactly the optimal stake, the cap condition
        is `s > max_stake` (strict), so no scaling fires."""
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)
        # Optimal for budget=1000 on either leg is 500.
        a_capped = make_quote(decimal_odds=2.10, max_stake=500.0)

        baseline = allocate_maxmin([a, b], budget=1000.0)
        capped = allocate_maxmin([a_capped, b], budget=1000.0)
        assert baseline is not None and capped is not None

        # Stakes are bit-for-bit identical; the cap branch is not taken.
        assert capped == baseline

    def test_all_legs_capped_but_only_tightest_binds(self) -> None:
        a = make_quote(platform="X", decimal_odds=2.10, max_stake=600.0)
        b = make_quote(platform="Y", decimal_odds=2.10, max_stake=400.0)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None
        # Optimal is 500/500. b's 400 cap is the only binding one.
        assert stakes[1] == pytest.approx(400.0, abs=1e-9)
        # Equal-payout preserved.
        assert stakes[0] * a.decimal_odds == pytest.approx(stakes[1] * b.decimal_odds, abs=1e-9)

    def test_all_legs_capped_to_same_relative_share(self) -> None:
        """When every leg's cap binds at the same scale factor, the result
        is just the equal-payout allocation at that uniformly-reduced level."""
        a = make_quote(platform="X", decimal_odds=2.10, max_stake=100.0)
        b = make_quote(platform="Y", decimal_odds=2.10, max_stake=100.0)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None
        assert stakes[0] == pytest.approx(100.0, abs=1e-9)
        assert stakes[1] == pytest.approx(100.0, abs=1e-9)


class TestMinStakeBoundaries:
    def test_min_stake_zero_is_a_noop(self) -> None:
        a = make_quote(decimal_odds=2.10, min_stake=0.0)
        b = make_quote(decimal_odds=2.10, min_stake=0.0)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None
        # Same answer as the no-min case.
        baseline = allocate_maxmin(
            [make_quote(decimal_odds=2.10), make_quote(decimal_odds=2.10)],
            budget=1000.0,
        )
        assert stakes == baseline

    def test_min_stake_exactly_equal_to_rounded_stake_accepts(self) -> None:
        """`s < min_stake` is the rejection condition; equality must pass."""
        # Optimal stake on each leg is 500. min_stake=500 is the boundary.
        a = make_quote(decimal_odds=2.10, min_stake=500.0)
        b = make_quote(decimal_odds=2.10, min_stake=500.0)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None
        assert stakes[0] == pytest.approx(500.0, abs=1e-9)

    def test_min_stake_one_above_rounded_stake_rejects(self) -> None:
        a = make_quote(decimal_odds=2.10, min_stake=500.01)
        b = make_quote(decimal_odds=2.10)

        assert allocate_maxmin([a, b], budget=1000.0) is None


class TestIncrementBoundaries:
    def test_optimal_exact_multiple_of_increment_no_rounding_loss(self) -> None:
        """When the optimal stake is already on the grid, rounding is a no-op
        and equal-payout is preserved exactly."""
        # Optimal stakes are 500/500; an increment of 100 fits exactly.
        a = make_quote(decimal_odds=2.10, stake_increment=100.0)
        b = make_quote(decimal_odds=2.10, stake_increment=100.0)

        stakes = allocate_maxmin([a, b], budget=1000.0)
        assert stakes is not None
        assert stakes[0] == 500.0
        assert stakes[1] == 500.0
        # Payouts still equal — the rounding step did nothing.
        assert stakes[0] * a.decimal_odds == pytest.approx(stakes[1] * b.decimal_odds, abs=1e-9)


class TestNWayLarger:
    def test_five_way_arb_allocates_per_leg(self) -> None:
        """Sanity check that the loop generalizes past 4 legs."""
        quotes = [
            make_quote(platform=f"P{i}", outcome=f"O{i}", decimal_odds=5.20) for i in range(5)
        ]

        stakes = allocate_maxmin(quotes, budget=1000.0)
        assert stakes is not None
        assert len(stakes) == 5

        # Equal odds → equal stakes.
        for s in stakes[1:]:
            assert s == pytest.approx(stakes[0], rel=1e-9)
        # Budget fully consumed in the uncapped, unrounded case.
        assert sum(stakes) == pytest.approx(1000.0, rel=1e-9)


# ---- residual allocation (hedge around already-filled legs) ----


class TestResidualAllocator:
    """allocate_residual sizes remaining legs so the worst case (pinned by the
    placed legs) still locks a profit. All numbers below are hand-computed."""

    def test_one_placed_leg_equal_payout_and_profit(self) -> None:
        # placed payout 1000 (stake 700 @ ~1.43), one remaining leg @ 4.0, budget 1000.
        # target = min(1000, 1000/0.25) = 1000 → s = 250; profit = 1000 − (700+250) = 50.
        alloc = allocate_residual([1000.0], 700.0, [make_quote(decimal_odds=4.0)], 1000.0)
        assert alloc is not None
        assert alloc.stakes == (250.0,)
        assert alloc.total_stake == pytest.approx(950.0)
        assert alloc.guaranteed_profit == pytest.approx(50.0)

    def test_two_remaining_legs_equal_payout(self) -> None:
        # placed payout 1000 (stake 200 @ 5.0); two remaining legs @ 2.0 and 4.0.
        # inv = 0.75; target = min(1000, 2000/0.75) = 1000 → s = (500, 250).
        # both remaining outcomes + the placed leg each pay 1000; profit = 50.
        alloc = allocate_residual(
            [1000.0],
            200.0,
            [
                make_quote(platform="A", outcome="x", decimal_odds=2.0),
                make_quote(platform="B", outcome="y", decimal_odds=4.0),
            ],
            2000.0,
        )
        assert alloc is not None
        assert alloc.stakes == pytest.approx((500.0, 250.0))
        assert alloc.total_stake == pytest.approx(950.0)
        assert alloc.guaranteed_profit == pytest.approx(50.0)

    def test_cap_bound_target_clamps_stake_to_cap(self) -> None:
        # placed payout 2000; remaining @ 4.0 capped at max_stake 300 → target bound
        # by the cap term (300·4 = 1200 < placed 2000, < budget 4000) → s = 300 == cap.
        alloc = allocate_residual(
            [2000.0], 700.0, [make_quote(decimal_odds=4.0, max_stake=300.0)], 1000.0
        )
        assert alloc is not None
        assert alloc.stakes == (300.0,)  # exactly the cap
        assert alloc.guaranteed_profit == pytest.approx(200.0)

    def test_budget_bound_target_consumes_full_budget(self) -> None:
        # remaining budget 400 binds below placed payout (400/0.25 = 1600 < 2000) →
        # s = 400, i.e. the entire remaining budget is deployed.
        alloc = allocate_residual([2000.0], 700.0, [make_quote(decimal_odds=4.0)], 400.0)
        assert alloc is not None
        assert alloc.stakes == (400.0,)
        assert alloc.guaranteed_profit == pytest.approx(500.0)

    def test_increment_round_down_erodes_but_keeps_profit(self) -> None:
        # optimal s = 250, rounded DOWN to a multiple of 7 → 245; profit erodes 50→35.
        alloc = allocate_residual(
            [1000.0], 700.0, [make_quote(decimal_odds=4.0, stake_increment=7.0)], 1000.0
        )
        assert alloc is not None
        assert alloc.stakes == (245.0,)
        assert alloc.guaranteed_profit == pytest.approx(35.0)

    def test_favorable_higher_odds_smaller_stake_larger_profit(self) -> None:
        # remaining @ 5.0 (vs 4.0 baseline): s drops 250→200, profit rises 50→100.
        alloc = allocate_residual([1000.0], 700.0, [make_quote(decimal_odds=5.0)], 1000.0)
        assert alloc is not None
        assert alloc.stakes == (200.0,)
        assert alloc.guaranteed_profit == pytest.approx(100.0)

    def test_min_stake_bump_over_hedges_when_break_even(self) -> None:
        # optimal s = 250 < min_stake 300, but staking 300 still locks a non-negative
        # outcome: payout 1200, worst case min(1000, 1200) = 1000, total 700+300 = 1000 →
        # profit 0 ≥ floor. Over-hedging this leg beats going naked with the fill live.
        alloc = allocate_residual(
            [1000.0], 700.0, [make_quote(decimal_odds=4.0, min_stake=300.0)], 1000.0
        )
        assert alloc is not None
        assert alloc.stakes == (300.0,)
        assert alloc.guaranteed_profit == pytest.approx(0.0)

    def test_min_stake_bump_on_grid_with_increment(self) -> None:
        # optimal s = 250 < min_stake 251; the on-grid minimum is ceil(251/50)*50 = 300,
        # NOT 251 — a raw `s = min_stake` would bet off-grid and the book would reject it.
        alloc = allocate_residual(
            [1000.0],
            700.0,
            [make_quote(decimal_odds=4.0, min_stake=251.0, stake_increment=50.0)],
            1000.0,
        )
        assert alloc is not None
        assert alloc.stakes == (300.0,)
        assert alloc.guaranteed_profit == pytest.approx(0.0)

    def test_min_stake_bump_unprofitable_returns_none(self) -> None:
        # bumping to 400 drives profit negative (1000 − 1100 = −100) → no salvageable
        # hedge; reject (the worst case is still the placed leg's 1000).
        alloc = allocate_residual(
            [1000.0], 700.0, [make_quote(decimal_odds=4.0, min_stake=400.0)], 1000.0
        )
        assert alloc is None

    def test_min_stake_bump_exceeds_budget_returns_none(self) -> None:
        # the on-grid minimum (600) outruns the 500 remaining budget → the budget check
        # rejects even though the leg is hedge-able in principle.
        alloc = allocate_residual(
            [1000.0], 700.0, [make_quote(decimal_odds=4.0, min_stake=600.0)], 500.0
        )
        assert alloc is None

    def test_budget_exhausted_returns_none(self) -> None:
        # remaining budget ≤ 0 → nothing left to hedge with.
        assert allocate_residual([1000.0], 700.0, [make_quote(decimal_odds=4.0)], 0.0) is None
        assert allocate_residual([1000.0], 700.0, [make_quote(decimal_odds=4.0)], -5.0) is None

    def test_no_arb_overround_ge_one_returns_none(self) -> None:
        # two remaining legs @ 1.5 each (inv ≈ 1.333) → profit drives deeply negative.
        alloc = allocate_residual(
            [1000.0],
            700.0,
            [
                make_quote(platform="A", outcome="x", decimal_odds=1.5),
                make_quote(platform="B", outcome="y", decimal_odds=1.5),
            ],
            1000.0,
        )
        assert alloc is None

    def test_profit_below_floor_returns_none(self) -> None:
        # baseline profit 50; a 60 floor rejects, a 50 floor keeps (boundary inclusive).
        q = [make_quote(decimal_odds=4.0)]
        assert allocate_residual([1000.0], 700.0, q, 1000.0, min_profit_ars=60.0) is None
        kept = allocate_residual([1000.0], 700.0, q, 1000.0, min_profit_ars=50.0)
        assert kept is not None and kept.guaranteed_profit == pytest.approx(50.0)

    def test_value_error_on_odds_le_one(self) -> None:
        with pytest.raises(ValueError, match="> 1.0"):
            allocate_residual([1000.0], 700.0, [make_quote(decimal_odds=1.0)], 1000.0)

    def test_value_error_on_empty_placed_payouts(self) -> None:
        with pytest.raises(ValueError, match="placed_payouts"):
            allocate_residual([], 0.0, [make_quote(decimal_odds=4.0)], 1000.0)

    def test_value_error_on_empty_quotes(self) -> None:
        with pytest.raises(ValueError, match="quotes"):
            allocate_residual([1000.0], 700.0, [], 1000.0)

    def test_value_error_on_non_positive_placed_payout(self) -> None:
        with pytest.raises(ValueError, match="placed payouts"):
            allocate_residual([-5.0], 700.0, [make_quote(decimal_odds=4.0)], 1000.0)
