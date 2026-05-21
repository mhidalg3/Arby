"""Tests for the Dutch book detector.

The Phase 1 validation gate requires comprehensive coverage of this module
before moving on. These tests cover the canonical cases plus edge conditions
that will arise in real platform data.
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
) -> OddsQuote:
    """Helper to construct quotes with sensible defaults."""
    return OddsQuote(
        platform=platform,
        market_id=market_id,
        outcome=outcome,
        decimal_odds=decimal_odds,
        max_stake=max_stake,
        timestamp=timestamp,
    )


class TestBasicArbitrageDetection:
    def test_classic_arb_2_10_vs_2_10(self) -> None:
        """Two platforms quoting 2.10 on opposite outcomes yields ~4.76% margin."""
        a = make_quote(platform="X", outcome="A", decimal_odds=2.10)
        b = make_quote(platform="Y", outcome="B", decimal_odds=2.10)

        opp = detect_arbitrage(a, b, budget=1000.0)

        assert opp is not None
        assert 4.7 < opp.margin_pct < 4.8

    def test_payouts_are_equal_on_both_legs(self) -> None:
        """The hedge property: payout must be identical regardless of outcome."""
        a = make_quote(decimal_odds=2.10, outcome="A")
        b = make_quote(decimal_odds=2.05, outcome="B")

        opp = detect_arbitrage(a, b, budget=1000.0)
        assert opp is not None

        payout_if_a_wins = opp.stake_a * a.decimal_odds
        payout_if_b_wins = opp.stake_b * b.decimal_odds
        assert abs(payout_if_a_wins - payout_if_b_wins) < 0.01

    def test_guaranteed_profit_is_positive(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        opp = detect_arbitrage(a, b, budget=1000.0)
        assert opp is not None
        assert opp.guaranteed_profit > 0


class TestNoArbitrageCases:
    def test_overround_at_unity_returns_none(self) -> None:
        """1/2.0 + 1/2.0 = 1.0 exactly; not a Dutch book."""
        a = make_quote(decimal_odds=2.0)
        b = make_quote(decimal_odds=2.0)

        assert detect_arbitrage(a, b, budget=1000.0) is None

    def test_overround_greater_than_one_returns_none(self) -> None:
        """Typical bookmaker pricing: house edge means overround > 1."""
        a = make_quote(decimal_odds=1.80)
        b = make_quote(decimal_odds=1.80)

        assert detect_arbitrage(a, b, budget=1000.0) is None

    def test_margin_below_threshold_returns_none(self) -> None:
        """Mathematical arb exists but is below configured threshold."""
        # 1/2.01 + 1/2.01 ≈ 0.995, margin ≈ 0.5%
        a = make_quote(decimal_odds=2.01)
        b = make_quote(decimal_odds=2.01)

        # Default threshold is 1%, so this should be filtered
        assert detect_arbitrage(a, b, budget=1000.0) is None

        # But explicitly lowering the threshold should surface it
        opp = detect_arbitrage(a, b, budget=1000.0, min_margin_pct=0.1)
        assert opp is not None
        assert opp.margin_pct < 1.0


class TestInputValidation:
    def test_odds_at_unity_raises(self) -> None:
        a = make_quote(decimal_odds=1.0)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="must be > 1.0"):
            detect_arbitrage(a, b, budget=1000.0)

    def test_odds_below_unity_raises(self) -> None:
        a = make_quote(decimal_odds=0.95)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="must be > 1.0"):
            detect_arbitrage(a, b, budget=1000.0)

    def test_zero_budget_raises(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="must be positive"):
            detect_arbitrage(a, b, budget=0.0)

    def test_negative_budget_raises(self) -> None:
        a = make_quote(decimal_odds=2.10)
        b = make_quote(decimal_odds=2.10)

        with pytest.raises(ValueError, match="must be positive"):
            detect_arbitrage(a, b, budget=-100.0)


class TestLiquidityCaps:
    def test_cap_on_leg_a_scales_both_legs(self) -> None:
        """If platform A caps the stake, leg B must scale proportionally."""
        a = make_quote(decimal_odds=2.10, max_stake=100.0)
        b = make_quote(decimal_odds=2.10, max_stake=None)

        opp = detect_arbitrage(a, b, budget=1000.0)
        assert opp is not None
        assert opp.stake_a <= 100.0 + 1e-9

        # Hedge property must be preserved despite the cap
        payout_a = opp.stake_a * a.decimal_odds
        payout_b = opp.stake_b * b.decimal_odds
        assert abs(payout_a - payout_b) < 0.01

    def test_cap_on_leg_b_scales_both_legs(self) -> None:
        a = make_quote(decimal_odds=2.10, max_stake=None)
        b = make_quote(decimal_odds=2.10, max_stake=50.0)

        opp = detect_arbitrage(a, b, budget=1000.0)
        assert opp is not None
        assert opp.stake_b <= 50.0 + 1e-9

        payout_a = opp.stake_a * a.decimal_odds
        payout_b = opp.stake_b * b.decimal_odds
        assert abs(payout_a - payout_b) < 0.01

    def test_caps_above_optimal_stake_have_no_effect(self) -> None:
        """When caps exceed optimal stakes, they should not bind."""
        a = make_quote(decimal_odds=2.10, max_stake=10_000.0)
        b = make_quote(decimal_odds=2.10, max_stake=10_000.0)

        opp_capped = detect_arbitrage(a, b, budget=1000.0)

        a_uncapped = make_quote(decimal_odds=2.10, max_stake=None)
        b_uncapped = make_quote(decimal_odds=2.10, max_stake=None)
        opp_uncapped = detect_arbitrage(a_uncapped, b_uncapped, budget=1000.0)

        assert opp_capped is not None and opp_uncapped is not None
        assert abs(opp_capped.stake_a - opp_uncapped.stake_a) < 1e-9
        assert abs(opp_capped.stake_b - opp_uncapped.stake_b) < 1e-9


class TestAsymmetricOdds:
    def test_strong_favorite_vs_underdog(self) -> None:
        """1.10 vs 12.00 — heavily asymmetric but can still yield an arb."""
        # 1/1.10 + 1/12.00 = 0.909 + 0.083 = 0.992, so ~0.8% margin
        a = make_quote(decimal_odds=1.10)
        b = make_quote(decimal_odds=12.00)

        opp = detect_arbitrage(a, b, budget=1000.0, min_margin_pct=0.1)
        assert opp is not None
        # Most capital goes to leg A (the favorite); leg B is a small hedge
        assert opp.stake_a > opp.stake_b
