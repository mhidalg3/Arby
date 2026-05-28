"""Tests for the Dutch book detector.

The Phase 1 validation gate requires comprehensive coverage of this module
before moving on. These tests cover the 2-way canonical cases, the N-way
generalization (3-way 1X2 is the dominant Argentine soccer shape), and the
two placement-side concerns the detector enforces: liquidity caps and
per-platform stake quantization / minimums.
"""

from __future__ import annotations

import pytest

from src.arbitrage.dutch_book import OddsQuote, detect_arbitrage


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
    """Helper to construct quotes with sensible defaults."""
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


class TestBasicArbitrageDetection:
    def test_classic_two_way_arb(self) -> None:
        """Two platforms quoting 2.10 on opposite outcomes yields ~4.76% margin."""
        a = make_quote(platform="X", outcome="A", decimal_odds=2.10)
        b = make_quote(platform="Y", outcome="B", decimal_odds=2.10)

        opp = detect_arbitrage([a, b], budget=1000.0)

        assert opp is not None
        assert 4.7 < opp.margin_pct < 4.8

    def test_payouts_are_equal_on_both_legs(self) -> None:
        """The hedge property: unrounded stakes yield identical payouts."""
        a = make_quote(decimal_odds=2.10, outcome="A")
        b = make_quote(decimal_odds=2.05, outcome="B")

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None

        payout_if_a_wins = opp.stakes[0] * a.decimal_odds
        payout_if_b_wins = opp.stakes[1] * b.decimal_odds
        assert abs(payout_if_a_wins - payout_if_b_wins) < 0.01

    def test_guaranteed_profit_is_positive(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None
        assert opp.guaranteed_profit > 0

    def test_unrounded_uses_full_budget(self) -> None:
        """Without caps or rounding the allocation consumes the whole budget."""
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None
        assert abs(opp.total_stake - 1000.0) < 1e-6
        assert abs(opp.capital_utilization - 1.0) < 1e-6


class TestThreeWayAndNWay:
    def test_three_way_1x2_arb(self) -> None:
        """Soccer 1X2: home/draw/away across three platforms.

        Implied probabilities: 1/2.6 + 1/3.5 + 1/3.2 ≈ 0.9828 → ~1.72% margin.
        """
        home = make_quote(platform="X", outcome="home", decimal_odds=2.60)
        draw = make_quote(platform="Y", outcome="draw", decimal_odds=3.50)
        away = make_quote(platform="Z", outcome="away", decimal_odds=3.20)

        opp = detect_arbitrage([home, draw, away], budget=1000.0)

        assert opp is not None
        assert len(opp.legs) == 3
        assert len(opp.stakes) == 3
        assert 1.6 < opp.margin_pct < 1.9

        # All three payouts equal in the unrounded case.
        payouts = [s * q.decimal_odds for q, s in zip(opp.legs, opp.stakes, strict=True)]
        assert max(payouts) - min(payouts) < 0.01

    def test_three_way_no_arb_when_overround_exceeds_one(self) -> None:
        """Typical 1X2 with book margin: returns None."""
        home = make_quote(platform="X", decimal_odds=2.20)
        draw = make_quote(platform="Y", decimal_odds=3.20)
        away = make_quote(platform="Z", decimal_odds=2.90)

        assert detect_arbitrage([home, draw, away], budget=1000.0) is None

    def test_four_way_arb(self) -> None:
        """N-way generalization: four equal-payout legs at 4.20 each.

        Overround = 4 / 4.20 ≈ 0.952 → ~4.8% margin.
        """
        quotes = [
            make_quote(platform=f"P{i}", outcome=f"O{i}", decimal_odds=4.20) for i in range(4)
        ]

        opp = detect_arbitrage(quotes, budget=1000.0)

        assert opp is not None
        assert len(opp.legs) == 4
        assert 4.5 < opp.margin_pct < 5.0

    def test_single_quote_raises(self) -> None:
        a = make_quote(decimal_odds=2.10)
        with pytest.raises(ValueError, match="At least two quotes"):
            detect_arbitrage([a], budget=1000.0)

    def test_empty_quotes_raises(self) -> None:
        with pytest.raises(ValueError, match="At least two quotes"):
            detect_arbitrage([], budget=1000.0)


class TestNoArbitrageCases:
    def test_overround_at_unity_returns_none(self) -> None:
        """1/2.0 + 1/2.0 = 1.0 exactly; not a Dutch book."""
        a = make_quote(decimal_odds=2.0)
        b = make_quote(decimal_odds=2.0)

        assert detect_arbitrage([a, b], budget=1000.0) is None

    def test_overround_greater_than_one_returns_none(self) -> None:
        """Typical bookmaker pricing: house edge means overround > 1."""
        a = make_quote(decimal_odds=1.80)
        b = make_quote(decimal_odds=1.80)

        assert detect_arbitrage([a, b], budget=1000.0) is None

    def test_margin_below_threshold_returns_none(self) -> None:
        """Mathematical arb exists but is below configured threshold."""
        # 1/2.01 + 1/2.01 ≈ 0.995, theoretical margin ≈ 0.5%
        a = make_quote(decimal_odds=2.01)
        b = make_quote(decimal_odds=2.01)

        # Default threshold is 1%, so this should be filtered.
        assert detect_arbitrage([a, b], budget=1000.0) is None

        # Lowering the threshold surfaces it.
        opp = detect_arbitrage([a, b], budget=1000.0, min_margin_pct=0.1)
        assert opp is not None
        assert opp.margin_pct < 1.0


class TestInputValidation:
    def test_odds_at_unity_raises(self) -> None:
        a = make_quote(decimal_odds=1.0)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="finite value > 1.0"):
            detect_arbitrage([a, b], budget=1000.0)

    def test_odds_below_unity_raises(self) -> None:
        a = make_quote(decimal_odds=0.95)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="finite value > 1.0"):
            detect_arbitrage([a, b], budget=1000.0)

    def test_zero_budget_raises(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="finite positive"):
            detect_arbitrage([a, b], budget=0.0)

    def test_negative_budget_raises(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="finite positive"):
            detect_arbitrage([a, b], budget=-100.0)

    def test_zero_stake_increment_raises(self) -> None:
        a = make_quote(decimal_odds=2.10, stake_increment=0.0)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="stake_increment"):
            detect_arbitrage([a, b], budget=1000.0)


class TestLiquidityCaps:
    def test_cap_on_leg_a_scales_both_legs(self) -> None:
        """If platform A caps the stake, every leg must scale proportionally."""
        a = make_quote(decimal_odds=2.10, max_stake=100.0)
        b = make_quote(decimal_odds=2.10, max_stake=None)

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None
        assert opp.stakes[0] <= 100.0 + 1e-9

        # Hedge property preserved despite the cap.
        payout_a = opp.stakes[0] * a.decimal_odds
        payout_b = opp.stakes[1] * b.decimal_odds
        assert abs(payout_a - payout_b) < 0.01

    def test_cap_on_leg_b_scales_both_legs(self) -> None:
        a = make_quote(decimal_odds=2.10, max_stake=None)
        b = make_quote(decimal_odds=2.10, max_stake=50.0)

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None
        assert opp.stakes[1] <= 50.0 + 1e-9

        payout_a = opp.stakes[0] * a.decimal_odds
        payout_b = opp.stakes[1] * b.decimal_odds
        assert abs(payout_a - payout_b) < 0.01

    def test_tightest_of_multiple_caps_wins(self) -> None:
        """When several legs are capped, the tightest one drives the scale."""
        # Optimal stakes for each leg ≈ 333.33 (3-way equal odds).
        # cap 200 on leg 0 → scale ≈ 0.6
        # cap 100 on leg 2 → scale ≈ 0.3  (tighter, should win)
        a = make_quote(platform="X", decimal_odds=3.05, max_stake=200.0)
        b = make_quote(platform="Y", decimal_odds=3.05, max_stake=None)
        c = make_quote(platform="Z", decimal_odds=3.05, max_stake=100.0)

        opp = detect_arbitrage([a, b, c], budget=1000.0)
        assert opp is not None
        assert opp.stakes[2] <= 100.0 + 1e-9
        # If only c's cap bound, stake on a would be ≈ 100 (not 200).
        assert opp.stakes[0] < 110.0

    def test_caps_above_optimal_stake_have_no_effect(self) -> None:
        a = make_quote(decimal_odds=2.10, max_stake=10_000.0)
        b = make_quote(decimal_odds=2.10, max_stake=10_000.0)

        opp_capped = detect_arbitrage([a, b], budget=1000.0)

        a_uncapped = make_quote(decimal_odds=2.10, max_stake=None)
        b_uncapped = make_quote(decimal_odds=2.10, max_stake=None)
        opp_uncapped = detect_arbitrage([a_uncapped, b_uncapped], budget=1000.0)

        assert opp_capped is not None and opp_uncapped is not None
        assert abs(opp_capped.stakes[0] - opp_uncapped.stakes[0]) < 1e-9
        assert abs(opp_capped.stakes[1] - opp_uncapped.stakes[1]) < 1e-9


class TestRealizedMargin:
    def test_liquidity_scaling_reduces_utilization_not_roi(self) -> None:
        """Proportional scaling shrinks capital used; ROI percentage holds."""
        a = make_quote(decimal_odds=2.10, max_stake=100.0)
        b = make_quote(decimal_odds=2.10)

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None

        # Used much less than the budget.
        assert opp.capital_utilization < 0.5
        # But ROI on used capital is roughly the theoretical margin.
        assert abs(opp.realized_roi_pct - opp.margin_pct) < 0.5

    def test_rounding_can_erode_realized_roi_below_theoretical(self) -> None:
        """A coarse stake_increment breaks equal-payout; realized ROI drops."""
        # Asymmetric odds so the optimal stakes are NOT exact multiples of the
        # increment. Optimal stakes ≈ (511.8, 488.2); 25-peso increment on
        # leg A rounds 511.8 down to 500, breaking equal-payout. Theoretical
        # margin ≈ 4.71%, realized ROI is strictly lower.
        a = make_quote(decimal_odds=2.05, stake_increment=25.0)
        b = make_quote(decimal_odds=2.15)

        opp = detect_arbitrage([a, b], budget=1000.0, min_margin_pct=0.5)
        assert opp is not None
        assert opp.realized_roi_pct < opp.margin_pct
        # Rounding loss is bounded; we're not destroying value.
        assert opp.realized_roi_pct > 0

    def test_rounding_below_threshold_returns_none(self) -> None:
        """Even when theoretical margin clears, realized ROI is re-checked."""
        # Theoretical margin ≈ 3.36% (well above 1.0% threshold), but 50-peso
        # rounding on both asymmetric legs (~507, ~493) drops both to (500,
        # 450), the smaller payout binds, and realized ROI goes negative.
        a = make_quote(decimal_odds=2.04, stake_increment=50.0)
        b = make_quote(decimal_odds=2.10, stake_increment=50.0)

        assert detect_arbitrage([a, b], budget=1000.0, min_margin_pct=1.0) is None


class TestMinStakeAndIncrement:
    def test_min_stake_violation_returns_none(self) -> None:
        """If any leg's optimal stake falls below its platform minimum, reject."""
        # Heavily asymmetric: 1/1.05 + 1/22.0 ≈ 0.998, marginal arb.
        # On leg b the optimal stake is tiny (~45 of 1000); a min_stake of
        # 100 on that platform makes the bet unplaceable.
        a = make_quote(platform="X", decimal_odds=1.05)
        b = make_quote(platform="Y", decimal_odds=22.0, min_stake=100.0)

        assert detect_arbitrage([a, b], budget=1000.0, min_margin_pct=0.1) is None

    def test_min_stake_met_returns_opportunity(self) -> None:
        a = make_quote(decimal_odds=2.10, min_stake=50.0)
        b = make_quote(decimal_odds=2.10, min_stake=50.0)

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None
        assert opp.stakes[0] >= 50.0
        assert opp.stakes[1] >= 50.0

    def test_stake_increment_rounds_stakes_down(self) -> None:
        """Returned stakes are exact multiples of each leg's increment."""
        a = make_quote(decimal_odds=2.10, stake_increment=10.0)
        b = make_quote(decimal_odds=2.10, stake_increment=5.0)

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None
        assert opp.stakes[0] % 10.0 == pytest.approx(0.0, abs=1e-9)
        assert opp.stakes[1] % 5.0 == pytest.approx(0.0, abs=1e-9)

    def test_rounding_never_violates_max_stake(self) -> None:
        """Rounding DOWN is the contract; the cap must never be exceeded."""
        a = make_quote(decimal_odds=2.10, max_stake=100.0, stake_increment=1.0)
        b = make_quote(decimal_odds=2.10)

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None
        assert opp.stakes[0] <= 100.0

    def test_increment_larger_than_budget_returns_none(self) -> None:
        """When every leg rounds to zero, there is nothing to place."""
        a = make_quote(decimal_odds=2.10, stake_increment=10_000.0)
        b = make_quote(decimal_odds=2.10, stake_increment=10_000.0)

        assert detect_arbitrage([a, b], budget=1000.0) is None


class TestAsymmetricOdds:
    def test_strong_favorite_vs_underdog(self) -> None:
        """1.10 vs 12.00 — heavily asymmetric but can still yield an arb."""
        # 1/1.10 + 1/12.00 = 0.909 + 0.083 = 0.992, so ~0.8% margin.
        a = make_quote(decimal_odds=1.10)
        b = make_quote(decimal_odds=12.00)

        opp = detect_arbitrage([a, b], budget=1000.0, min_margin_pct=0.1)
        assert opp is not None
        # Most capital goes to the favorite; the underdog is a small hedge.
        assert opp.stakes[0] > opp.stakes[1]


class TestNumericalBoundaries:
    def test_overround_just_under_one_yields_arb(self) -> None:
        """Tiny arbs exist near the boundary; threshold must allow them through."""
        # 1/2.0 + 1/2.005 ≈ 0.5 + 0.49875 = 0.99875 → margin ≈ 0.125%.
        a = make_quote(decimal_odds=2.0)
        b = make_quote(decimal_odds=2.005)

        # Default threshold (1%) rejects.
        assert detect_arbitrage([a, b], budget=1000.0) is None

        # Sub-threshold call surfaces it.
        opp = detect_arbitrage([a, b], budget=1000.0, min_margin_pct=0.0)
        assert opp is not None
        assert 0.1 < opp.margin_pct < 0.2

    def test_overround_just_over_one_returns_none(self) -> None:
        """Symmetric small overround above 1.0; the math correctly rejects."""
        # 1/1.999 + 1/1.999 ≈ 1.0005 → no arb.
        a = make_quote(decimal_odds=1.999)
        b = make_quote(decimal_odds=1.999)

        assert detect_arbitrage([a, b], budget=1000.0, min_margin_pct=0.0) is None


class TestExtremeBudgets:
    def test_very_small_budget_scales_linearly(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        small = detect_arbitrage([a, b], budget=1.0)
        large = detect_arbitrage([a, b], budget=1_000.0)
        assert small is not None and large is not None

        # Stakes scale linearly with budget; ROI percentage is invariant.
        assert small.stakes[0] * 1000.0 == pytest.approx(large.stakes[0], rel=1e-9)
        assert small.realized_roi_pct == pytest.approx(large.realized_roi_pct, rel=1e-9)

    def test_very_large_budget(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        opp = detect_arbitrage([a, b], budget=1e9)
        assert opp is not None
        assert opp.total_stake == pytest.approx(1e9, rel=1e-9)
        assert opp.guaranteed_profit > 0

    def test_nan_budget_raises(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="finite"):
            detect_arbitrage([a, b], budget=float("nan"))

    def test_infinite_budget_raises(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="finite"):
            detect_arbitrage([a, b], budget=float("inf"))


class TestExtremeOdds:
    def test_huge_odds_yield_huge_margin(self) -> None:
        """Symmetric long-shot odds: overround tiny, margin near 100%.

        Mostly a sanity check that the math doesn't break at this extreme; in
        reality you would never see a real arb of this shape.
        """
        a = make_quote(decimal_odds=100.0)
        b = make_quote(decimal_odds=100.0)

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None
        assert opp.margin_pct > 97.0
        # Equal split because odds are equal.
        assert opp.stakes[0] == pytest.approx(opp.stakes[1], rel=1e-9)

    def test_nan_odds_raises(self) -> None:
        a = make_quote(decimal_odds=float("nan"))
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="finite"):
            detect_arbitrage([a, b], budget=1000.0)

    def test_infinite_odds_raises(self) -> None:
        """Without this guard 1/inf collapses to 0 and the math produces a
        nonsense opportunity with NaN profit. Reject explicitly."""
        a = make_quote(decimal_odds=float("inf"))
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="finite"):
            detect_arbitrage([a, b], budget=1000.0)


class TestQuoteFieldValidation:
    def test_negative_max_stake_raises(self) -> None:
        a = make_quote(decimal_odds=2.10, max_stake=-50.0)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="max_stake"):
            detect_arbitrage([a, b], budget=1000.0)

    def test_zero_max_stake_raises(self) -> None:
        """max_stake=0 means 'no bet possible'; reject as an upstream data bug."""
        a = make_quote(decimal_odds=2.10, max_stake=0.0)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="max_stake"):
            detect_arbitrage([a, b], budget=1000.0)

    def test_negative_min_stake_raises(self) -> None:
        a = make_quote(decimal_odds=2.10, min_stake=-10.0)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="min_stake"):
            detect_arbitrage([a, b], budget=1000.0)

    def test_zero_min_stake_is_allowed(self) -> None:
        """min_stake=0 is a sensible 'no minimum'; should not raise."""
        a = make_quote(decimal_odds=2.10, min_stake=0.0)
        b = make_quote(decimal_odds=2.10, min_stake=0.0)

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None

    def test_nan_max_stake_raises(self) -> None:
        a = make_quote(decimal_odds=2.10, max_stake=float("nan"))
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="max_stake"):
            detect_arbitrage([a, b], budget=1000.0)

    def test_nan_stake_increment_raises(self) -> None:
        a = make_quote(decimal_odds=2.10, stake_increment=float("nan"))
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="stake_increment"):
            detect_arbitrage([a, b], budget=1000.0)


class TestMarginThresholdBoundaries:
    def test_margin_exactly_at_threshold_accepts(self) -> None:
        """The threshold check is strict-less-than; equality must pass."""
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)
        # The actual computed margin for symmetric 2.10/2.10.
        expected_margin = (1.0 - (1.0 / 2.10 + 1.0 / 2.10)) * 100.0

        opp = detect_arbitrage([a, b], budget=1000.0, min_margin_pct=expected_margin)
        assert opp is not None

    def test_zero_threshold_accepts_any_positive_margin(self) -> None:
        a = make_quote(decimal_odds=2.001)
        b = make_quote(decimal_odds=2.001)

        opp = detect_arbitrage([a, b], budget=1000.0, min_margin_pct=0.0)
        assert opp is not None
        assert 0 < opp.margin_pct < 0.1


class TestArbitrageOpportunityProperties:
    def test_is_hashable_and_equal_by_value(self) -> None:
        """Frozen dataclass should be usable in sets and dict keys."""
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        opp1 = detect_arbitrage([a, b], budget=1000.0)
        opp2 = detect_arbitrage([a, b], budget=1000.0)
        assert opp1 is not None and opp2 is not None

        assert opp1 == opp2
        assert hash(opp1) == hash(opp2)
        assert {opp1, opp2} == {opp1}

    def test_legs_preserve_input_order(self) -> None:
        # A real 3-way arb (1/2.60 + 1/3.50 + 1/3.20 ≈ 0.983).
        a = make_quote(platform="X", outcome="A", decimal_odds=2.60)
        b = make_quote(platform="Y", outcome="B", decimal_odds=3.50)
        c = make_quote(platform="Z", outcome="C", decimal_odds=3.20)

        opp = detect_arbitrage([c, a, b], budget=1000.0, min_margin_pct=0.1)
        assert opp is not None
        assert [leg.outcome for leg in opp.legs] == ["C", "A", "B"]


class TestInputHandling:
    def test_accepts_tuple_input(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        opp = detect_arbitrage((a, b), budget=1000.0)
        assert opp is not None

    def test_mutating_input_list_does_not_affect_result(self) -> None:
        """The detector should snapshot its inputs into the returned tuple."""
        a = make_quote(platform="X", decimal_odds=2.10)
        b = make_quote(platform="Y", decimal_odds=2.10)
        quotes = [a, b]

        opp = detect_arbitrage(quotes, budget=1000.0)
        assert opp is not None

        quotes.append(make_quote(decimal_odds=99.0))
        quotes[0] = make_quote(platform="Z", decimal_odds=3.0)

        assert len(opp.legs) == 2
        assert opp.legs[0].platform == "X"

    def test_same_platform_on_multiple_legs_is_allowed(self) -> None:
        """The detector does not enforce platform diversity — that's a policy
        decision for the risk layer. Math works regardless."""
        a = make_quote(platform="codere", outcome="home", decimal_odds=2.10)
        b = make_quote(platform="codere", outcome="away", decimal_odds=2.10)

        opp = detect_arbitrage([a, b], budget=1000.0)
        assert opp is not None
