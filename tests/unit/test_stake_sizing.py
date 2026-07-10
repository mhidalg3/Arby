"""Unit tests for the deterministic stake sizer.

Anchored on real arb shapes captured during the 30-min continuous
run on 2026-05-26 — Fluminense vs Bolivar (Bplay+Betsson, 19% margin),
Avai vs Criciuma (Betsson+BetWarrior BTTS, 1.3% margin), the
BetWarrior-solo emission.
"""

from __future__ import annotations

import pytest

from src.arbitrage.quotes import OddsQuote
from src.risk.stake_sizing import StakeSizer, StakeSizingPolicy


def _q(platform: str, outcome: str = "HOME", odds: float = 2.0) -> OddsQuote:
    return OddsQuote(
        platform=platform,
        market_id="fx-test|1x2",
        outcome=outcome,
        decimal_odds=odds,
        max_stake=None,
        timestamp=1000.0,
    )


# ---- Headline shape tests ----


class TestComputeBudget:
    def test_bplay_plus_betsson_yields_80pct_of_max(self) -> None:
        """The Fluminense vs Bolivar shape: Bplay × Bplay × Betsson.
        Reliability 1.0 × 1.0 × 0.8 = 0.8. Budget = capital × 5% × 0.8."""
        sizer = StakeSizer(policy=StakeSizingPolicy(total_capital_ars=1_000_000.0))
        budget = sizer.compute_budget(
            [
                _q("bplay-pba", "HOME", 1.75),
                _q("bplay-pba", "DRAW", 5.80),
                _q("betsson-pba", "AWAY", 10.50),
            ]
        )
        # 1,000,000 × 0.05 × (1.0 × 1.0 × 0.8) = 40,000 ARS
        assert budget == pytest.approx(40_000.0)

    def test_bplay_plus_betwarrior_yields_full_cap(self) -> None:
        """Both platforms 1.0 reliability → 100% of the max-fraction cap."""
        sizer = StakeSizer(policy=StakeSizingPolicy(total_capital_ars=1_000_000.0))
        budget = sizer.compute_budget(
            [
                _q("bplay-pba", "OVER", 2.0),
                _q("betwarrior-pba", "UNDER", 2.0),
            ]
        )
        # 1,000,000 × 0.05 × (1.0 × 1.0) = 50,000 ARS (the per-arb ceiling)
        assert budget == pytest.approx(50_000.0)

    def test_betsson_plus_betwarrior_2leg_btts(self) -> None:
        """Avai vs Criciuma BTTS shape: 1.0 × 0.8 = 0.8."""
        sizer = StakeSizer(policy=StakeSizingPolicy(total_capital_ars=1_000_000.0))
        budget = sizer.compute_budget(
            [
                _q("betwarrior-pba", "YES", 2.04),
                _q("betsson-pba", "NO", 1.95),
            ]
        )
        # 1,000,000 × 0.05 × 0.8 = 40,000 ARS
        assert budget == pytest.approx(40_000.0)


# ---- Confidence floor ----


class TestConfidenceFloor:
    def test_low_confidence_returns_zero(self) -> None:
        """Two unknown platforms → 0.5 × 0.5 = 0.25 confidence → below
        the 0.5 floor → 0 budget."""
        sizer = StakeSizer(policy=StakeSizingPolicy(total_capital_ars=1_000_000.0))
        budget = sizer.compute_budget(
            [
                _q("unknown-platform-a"),
                _q("unknown-platform-b"),
            ]
        )
        assert budget == 0.0

    def test_mixed_known_and_unknown(self) -> None:
        """1.0 × 0.5 = 0.5 — exactly at the floor → passes."""
        sizer = StakeSizer(policy=StakeSizingPolicy(total_capital_ars=1_000_000.0))
        budget = sizer.compute_budget(
            [
                _q("bplay-pba"),
                _q("totally-new-platform"),
            ]
        )
        # 1,000,000 × 0.05 × 0.5 = 25,000 ARS
        assert budget == pytest.approx(25_000.0)

    def test_single_unknown_yields_zero(self) -> None:
        """A single unknown platform leg (0.5) is below the 0.5 floor,
        not at it (strict less-than)."""
        sizer = StakeSizer(
            policy=StakeSizingPolicy(
                total_capital_ars=1_000_000.0,
                min_confidence=0.5001,
            )
        )
        budget = sizer.compute_budget(
            [
                _q("bplay-pba"),
                _q("unknown-1"),
            ]
        )
        assert budget == 0.0


# ---- Min-stake floor ----


class TestMinStakeFloor:
    def test_tiny_capital_skips(self) -> None:
        """With 1,000 ARS capital and 5% per-arb cap, max budget is
        50 ARS — well below the 500-ARS min-stake floor."""
        sizer = StakeSizer(
            policy=StakeSizingPolicy(
                total_capital_ars=1_000.0,
                min_total_stake_ars=500.0,
            )
        )
        budget = sizer.compute_budget(
            [
                _q("bplay-pba"),
                _q("betwarrior-pba"),
            ]
        )
        assert budget == 0.0


# ---- Policy tuning ----


class TestPolicyTuning:
    def test_higher_capital_scales_budget_linearly(self) -> None:
        """Doubling capital doubles the budget (at the same
        confidence + fraction)."""
        base_policy = StakeSizingPolicy(total_capital_ars=1_000_000.0)
        scaled_policy = StakeSizingPolicy(total_capital_ars=2_000_000.0)
        legs = [_q("bplay-pba"), _q("betwarrior-pba")]
        base = StakeSizer(base_policy).compute_budget(legs)
        scaled = StakeSizer(scaled_policy).compute_budget(legs)
        assert scaled == pytest.approx(base * 2)

    def test_smaller_fraction_yields_smaller_budget(self) -> None:
        """2% per-arb instead of 5% → 40% of the budget."""
        policy_5 = StakeSizingPolicy(total_capital_ars=1_000_000.0, max_fraction_per_arb=0.05)
        policy_2 = StakeSizingPolicy(total_capital_ars=1_000_000.0, max_fraction_per_arb=0.02)
        legs = [_q("bplay-pba"), _q("betwarrior-pba")]
        assert StakeSizer(policy_5).compute_budget(legs) == pytest.approx(50_000.0)
        assert StakeSizer(policy_2).compute_budget(legs) == pytest.approx(20_000.0)

    def test_custom_reliability_override(self) -> None:
        """Operator can override per-platform reliability in policy."""
        policy = StakeSizingPolicy(
            total_capital_ars=1_000_000.0,
            platform_reliability={"bplay-pba": 0.5, "betwarrior-pba": 1.0},
        )
        budget = StakeSizer(policy).compute_budget(
            [
                _q("bplay-pba"),
                _q("betwarrior-pba"),
            ]
        )
        # 1,000,000 × 0.05 × (0.5 × 1.0) = 25,000 ARS
        assert budget == pytest.approx(25_000.0)


# ---- Edge cases ----


class TestEdgeCases:
    def test_empty_legs_returns_zero(self) -> None:
        sizer = StakeSizer(policy=StakeSizingPolicy(total_capital_ars=1_000_000.0))
        assert sizer.compute_budget([]) == 0.0

    def test_single_platform_three_legs(self) -> None:
        """If somehow all 3 legs come from one high-reliability
        platform, the sizer still returns full budget. The DETECTOR
        + risk daemon are responsible for rejecting single-platform
        opportunities — sizing doesn't second-guess that."""
        sizer = StakeSizer(policy=StakeSizingPolicy(total_capital_ars=1_000_000.0))
        budget = sizer.compute_budget(
            [
                _q("bplay-pba", "HOME"),
                _q("bplay-pba", "DRAW"),
                _q("bplay-pba", "AWAY"),
            ]
        )
        assert budget == pytest.approx(50_000.0)

    def test_high_leg_count_compounds_low_confidence(self) -> None:
        """Six legs at 0.8 each → 0.8^6 ≈ 0.262 confidence → below
        the 0.5 floor → 0 budget."""
        sizer = StakeSizer(policy=StakeSizingPolicy(total_capital_ars=1_000_000.0))
        budget = sizer.compute_budget([_q("betsson-pba") for _ in range(6)])
        assert budget == 0.0
