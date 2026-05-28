"""Unit tests for the deterministic risk evaluator.

Anchored on the actual arb shapes captured during the 30-minute live
run on 2026-05-26 — the Fluminense vs Bolivar 1X2 (19.18% margin,
Bplay+Betsson), Avai vs Criciuma BTTS (1.3%, Betsson+BetWarrior),
the BetWarrior-solo emission (single-platform), etc.
"""

from __future__ import annotations

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.arbitrage.quotes import OddsQuote
from src.risk.decision import Verdict
from src.risk.evaluator import RiskEvaluator
from src.risk.policy import RiskPolicy


def _opp(
    legs: list[tuple[str, str, float]],  # [(platform, outcome, decimal_odds)]
    stakes: list[float],
    realized_roi_pct: float,
    market_id: str = "fx-test|1x2",
) -> ArbitrageOpportunity:
    """Build a synthetic ArbitrageOpportunity from a leg sketch."""
    odds_quotes = tuple(
        OddsQuote(
            platform=plat,
            market_id=market_id,
            outcome=outcome,
            decimal_odds=odds,
            max_stake=None,
            timestamp=1000.0,
        )
        for plat, outcome, odds in legs
    )
    total_stake = sum(stakes)
    return ArbitrageOpportunity(
        legs=odds_quotes,
        stakes=tuple(stakes),
        total_stake=total_stake,
        guaranteed_profit=total_stake * realized_roi_pct / 100,
        margin_pct=realized_roi_pct * 0.95,  # margin slightly lower than ROI typically
        realized_roi_pct=realized_roi_pct,
        capital_utilization=total_stake / 1000.0,
    )


# ---- Happy-path approvals ----


class TestApprovals:
    def test_typical_bplay_betsson_arb_approves(self) -> None:
        """Real shape: Fluminense vs Bolivar 1X2, 19.18% margin,
        Bplay best on HOME+DRAW, Betsson best on AWAY. Persisted 27
        minutes in the live run — clearly real and tradeable."""
        opp = _opp(
            legs=[
                ("bplay-pba", "HOME", 1.75),
                ("bplay-pba", "DRAW", 5.80),
                ("betsson-pba", "AWAY", 10.50),
            ],
            stakes=[681.0, 205.5, 113.5],
            realized_roi_pct=19.18,
        )
        e = RiskEvaluator(policy=RiskPolicy())
        d = e.evaluate(opp)
        assert d.verdict == Verdict.APPROVED
        assert d.high_margin_warning is True  # 19.18 > 10
        assert set(d.platforms) == {"bplay-pba", "betsson-pba"}
        # confidence = 1.0 * 1.0 * 0.8 = 0.8
        assert d.confidence == 0.8

    def test_modest_brazilian_btts_arb_approves(self) -> None:
        """Real shape: Avai vs Criciuma BTTS, 1.3% margin, Betsson +
        BetWarrior. Sweet-spot tradeable arb after fees."""
        opp = _opp(
            legs=[
                ("betwarrior-pba", "YES", 2.04),
                ("betsson-pba", "NO", 1.95),
            ],
            stakes=[490.0, 510.0],
            realized_roi_pct=1.30,
            market_id="fx-test|btts",
        )
        e = RiskEvaluator(policy=RiskPolicy())
        d = e.evaluate(opp)
        assert d.verdict == Verdict.APPROVED
        assert d.high_margin_warning is False  # 1.3 < 10
        assert d.confidence == 0.8  # 1.0 * 0.8

    def test_exact_min_margin_approves(self) -> None:
        opp = _opp(
            legs=[("bplay-pba", "OVER", 2.05), ("betwarrior-pba", "UNDER", 2.00)],
            stakes=[488.0, 500.0],
            realized_roi_pct=0.5,  # exactly at threshold
            market_id="fx-test|ou_goals|2.5",
        )
        e = RiskEvaluator(policy=RiskPolicy(min_margin_pct=0.5))
        d = e.evaluate(opp)
        assert d.verdict == Verdict.APPROVED
        assert d.reason == "approved"

    def test_at_max_margin_approves_with_warning(self) -> None:
        opp = _opp(
            legs=[("bplay-pba", "HOME", 2.5), ("betwarrior-pba", "AWAY", 3.0)],
            stakes=[571.4, 428.6],
            realized_roi_pct=25.0,  # exactly at ceiling
            market_id="fx-test|1x2",  # unrealistic but tests the edge
        )
        e = RiskEvaluator(policy=RiskPolicy())
        d = e.evaluate(opp)
        # 25.0 is <= max_margin_pct=25.0, should APPROVE
        assert d.verdict == Verdict.APPROVED
        assert d.high_margin_warning is True


# ---- Rule cascade: rejections ----


class TestRejections:
    def test_below_min_margin_rejects(self) -> None:
        opp = _opp(
            legs=[("bplay-pba", "OVER", 2.0), ("betwarrior-pba", "UNDER", 1.99)],
            stakes=[497.5, 502.5],
            realized_roi_pct=0.2,
        )
        d = RiskEvaluator(policy=RiskPolicy()).evaluate(opp)
        assert d.verdict == Verdict.REJECTED
        assert "below min" in d.reason
        assert d.rules_evaluated == ("margin_min",)  # cascade exited at first rule

    def test_above_max_margin_rejects(self) -> None:
        """The detector emits everything ≥ min_margin; the risk layer
        rejects suspicious >25% as likely artifact."""
        opp = _opp(
            legs=[("bplay-pba", "HOME", 5.0), ("betsson-pba", "AWAY", 5.0)],
            stakes=[500.0, 500.0],
            realized_roi_pct=60.0,  # absurd
        )
        d = RiskEvaluator(policy=RiskPolicy()).evaluate(opp)
        assert d.verdict == Verdict.REJECTED
        assert "above max" in d.reason
        assert d.rules_evaluated == ("margin_min", "margin_max")

    def test_single_platform_rejects(self) -> None:
        """The BetWarrior-solo emission shape from the 30-min run.
        Single-bookmaker arbs are typically transient mispricings."""
        opp = _opp(
            legs=[
                ("betwarrior-pba", "YES", 2.10),
                ("betwarrior-pba", "NO", 2.10),
            ],
            stakes=[500.0, 500.0],
            realized_roi_pct=5.0,
            market_id="fx-test|btts",
        )
        d = RiskEvaluator(policy=RiskPolicy()).evaluate(opp)
        assert d.verdict == Verdict.REJECTED
        assert "distinct platform" in d.reason
        # Got to the distinct_platforms rule
        assert d.rules_evaluated[-1] == "distinct_platforms"
        assert d.platforms == ("betwarrior-pba",)

    def test_per_leg_stake_above_cap_rejects(self) -> None:
        """The default per-leg cap was raised to 50k ARS when stake
        sizing moved to the detector. A leg above that gets rejected."""
        opp = _opp(
            legs=[("bplay-pba", "OVER", 2.0), ("betwarrior-pba", "UNDER", 2.0)],
            stakes=[60_000.0, 50_000.0],  # leg 0 over 50k per-leg cap
            realized_roi_pct=2.0,
            market_id="fx-test|ou_goals|2.5",
        )
        d = RiskEvaluator(policy=RiskPolicy()).evaluate(opp)
        assert d.verdict == Verdict.REJECTED
        assert "per-leg cap" in d.reason

    def test_negative_stake_rejects(self) -> None:
        opp = _opp(
            legs=[("bplay-pba", "OVER", 2.0), ("betwarrior-pba", "UNDER", 2.0)],
            stakes=[500.0, -1.0],
            realized_roi_pct=1.0,
            market_id="fx-test|ou_goals|2.5",
        )
        d = RiskEvaluator(policy=RiskPolicy()).evaluate(opp)
        assert d.verdict == Verdict.REJECTED
        assert "non-positive stake" in d.reason

    def test_low_confidence_rejects(self) -> None:
        """An opportunity whose legs all come from unknown / low-trust
        platforms has confidence below `min_confidence` and gets
        rejected."""
        opp = _opp(
            legs=[("unknown-pba", "HOME", 2.5), ("another-unknown", "AWAY", 2.5)],
            stakes=[500.0, 500.0],
            realized_roi_pct=5.0,
        )
        # unknown platforms get 0.5 each → 0.25 confidence
        d = RiskEvaluator(policy=RiskPolicy()).evaluate(opp)
        assert d.verdict == Verdict.REJECTED
        assert "confidence" in d.reason
        assert d.confidence == 0.25


# ---- Policy tuning ----


class TestPolicyOverride:
    def test_custom_margin_threshold(self) -> None:
        """Restrictive policy: only approve ≥5% margin arbs."""
        policy = RiskPolicy(min_margin_pct=5.0)
        opp = _opp(
            legs=[("bplay-pba", "HOME", 2.0), ("betwarrior-pba", "AWAY", 2.0)],
            stakes=[500.0, 500.0],
            realized_roi_pct=2.0,
        )
        d = RiskEvaluator(policy=policy).evaluate(opp)
        assert d.verdict == Verdict.REJECTED
        assert "below min" in d.reason

    def test_custom_platform_reliability(self) -> None:
        policy = RiskPolicy(
            platform_reliability={"bplay-pba": 1.0, "betsson-pba": 0.3},
            min_confidence=0.5,
        )
        opp = _opp(
            legs=[("bplay-pba", "HOME", 2.0), ("betsson-pba", "AWAY", 2.0)],
            stakes=[500.0, 500.0],
            realized_roi_pct=5.0,
        )
        d = RiskEvaluator(policy=policy).evaluate(opp)
        # 1.0 * 0.3 = 0.3 < 0.5 → reject
        assert d.verdict == Verdict.REJECTED
        assert d.confidence == 0.3

    def test_unknown_platform_uses_default(self) -> None:
        policy = RiskPolicy(
            platform_reliability={"bplay-pba": 1.0},
            default_unknown_platform_reliability=0.4,
            min_confidence=0.5,
        )
        opp = _opp(
            legs=[
                ("bplay-pba", "HOME", 2.0),
                ("totally-new-platform", "AWAY", 2.0),
            ],
            stakes=[500.0, 500.0],
            realized_roi_pct=5.0,
        )
        d = RiskEvaluator(policy=policy).evaluate(opp)
        # 1.0 * 0.4 = 0.4 < 0.5 → reject
        assert d.confidence == 0.4
        assert d.verdict == Verdict.REJECTED


# ---- Decision shape ----


class TestDecisionShape:
    def test_decision_carries_audit_fields(self) -> None:
        opp = _opp(
            legs=[("bplay-pba", "HOME", 2.0), ("betsson-pba", "AWAY", 2.05)],
            stakes=[506.2, 493.8],
            realized_roi_pct=1.25,
            market_id="fx-abc|btts",
        )
        d = RiskEvaluator(policy=RiskPolicy()).evaluate(opp, now=1500.0)
        assert d.fixture_id == "fx-abc"
        assert d.market_id == "fx-abc|btts"
        assert d.realized_roi_pct == 1.25
        assert d.evaluated_at == 1500.0
        assert d.platforms == ("betsson-pba", "bplay-pba")  # sorted
