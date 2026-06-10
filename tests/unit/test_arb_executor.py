"""Tests for the detector→executor bridge (OddsQuote → Leg mapping + execution)."""

from __future__ import annotations

import pytest

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.arbitrage.quotes import OddsQuote
from src.execution.arb_executor import (
    execute_opportunity,
    leg_from_quote,
    legs_from_opportunity,
)
from src.execution.executor import ExecutionOutcome, Executor, Leg, PlacementResult
from src.execution.guardrails import Guardrails
from src.execution.recovery import RecoveryOutcome


def _quote(platform: str, outcome: str, odds: float, **over: object) -> OddsQuote:
    kw: dict[str, object] = {
        "platform": platform,
        "market_id": "MKT-1",
        "outcome": outcome,
        "decimal_odds": odds,
        "max_stake": 5000.0,
        "timestamp": 0.0,
        "platform_outcome_id": f"{platform}-sel",
        "platform_event_id": f"{platform}-evt",
    }
    kw.update(over)
    return OddsQuote(**kw)  # type: ignore[arg-type]


def _opp(*, stakes: tuple[float, ...]) -> ArbitrageOpportunity:
    legs = (_quote("betsson", "home", 2.1), _quote("betano", "away", 2.1))
    return ArbitrageOpportunity(
        legs=legs,
        stakes=stakes,
        total_stake=sum(stakes),
        guaranteed_profit=5.0,
        margin_pct=2.0,
        realized_roi_pct=2.0,
        capital_utilization=1.0,
    )


def test_leg_from_quote_maps_all_fields() -> None:
    q = _quote("betano", "away", 1.95, max_stake=1234.0)
    leg = leg_from_quote(q, 100.0, match_id="MKT-1")
    assert leg.platform == "betano"
    assert leg.match_id == "MKT-1"  # canonical, for exposure tracking
    assert leg.platform_event_ref == "betano-evt"  # Betano eventId
    assert leg.platform_outcome_id == "betano-sel"
    assert leg.odds == 1.95 and leg.stake_ars == 100.0
    assert leg.live_max_stake_ars == 1234.0  # liquidity cap → resolves the dynamic guard


def test_legs_share_canonical_match_id() -> None:
    legs = legs_from_opportunity(_opp(stakes=(100.0, 100.0)))
    assert len(legs) == 2
    assert legs[0].match_id == legs[1].match_id == "MKT-1"  # both legs, one match
    assert {leg.platform for leg in legs} == {"betsson", "betano"}


def test_legs_stakes_length_mismatch_raises() -> None:
    opp = _opp(stakes=(100.0,))  # 1 stake, 2 legs
    with pytest.raises(ValueError, match="length mismatch"):
        legs_from_opportunity(opp)


# ---- execution through the bridge ----


class _Placer:
    def __init__(self) -> None:
        self.legs: list[Leg] = []

    async def place(self, leg: Leg) -> PlacementResult:
        self.legs.append(leg)
        return PlacementResult(accepted=True, stake_filled=leg.stake_ars, odds_filled=leg.odds)


class _Notifier:
    async def send(self, text: str) -> bool:
        return True


class _Recovery:
    async def recover(self, reason: str) -> RecoveryOutcome:
        return RecoveryOutcome.UNRESOLVED


def _guard() -> Guardrails:
    return Guardrails(
        max_position_per_match_ars=10_000.0,
        max_total_exposure_ars=100_000.0,
        max_daily_loss_ars=5_000.0,
        odds_tolerance_pct=100.0,
    )


async def test_execute_opportunity_routes_both_legs_and_completes() -> None:
    bp, ap = _Placer(), _Placer()
    ex = Executor(
        guardrails=_guard(),
        notifier=_Notifier(),
        recovery=_Recovery(),
        placers={"betsson": bp, "betano": ap},
    )
    res = await execute_opportunity(ex, _opp(stakes=(100.0, 100.0)), opp_id="opp-1")
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert len(bp.legs) == 1 and len(ap.legs) == 1  # each platform got its leg
    assert ap.legs[0].live_max_stake_ars == 5000.0  # dynamic cap carried through


async def test_execute_opportunity_places_three_leg_arb() -> None:
    """A 1X2 (three-outcome) arb now executes — all three legs route + complete."""
    three = _opp(stakes=(50.0, 50.0))
    legs = (*three.legs, _quote("bplay", "draw", 3.5))
    opp = ArbitrageOpportunity(
        legs=legs,
        stakes=(50.0, 50.0, 50.0),
        total_stake=150.0,
        guaranteed_profit=1.0,
        margin_pct=1.0,
        realized_roi_pct=1.0,
        capital_utilization=1.0,
    )
    bp, ap, dp = _Placer(), _Placer(), _Placer()
    ex = Executor(
        guardrails=_guard(),
        notifier=_Notifier(),
        recovery=_Recovery(),
        placers={"betsson": bp, "betano": ap, "bplay": dp},
    )
    res = await execute_opportunity(ex, opp, opp_id="opp-3")
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert len(res.legs) == 3  # all three filled
    assert len(bp.legs) == len(ap.legs) == len(dp.legs) == 1  # each platform got its leg


def test_dynamic_cap_applied_to_betano_when_quote_has_no_max_stake() -> None:
    # Betano (dynamic) quote with no max_stake → the conservative cap is applied;
    # Betsson (non-dynamic) is untouched.
    q_betano = _quote("betano", "away", 1.95, max_stake=None)
    leg = leg_from_quote(q_betano, 50.0, match_id="MKT-1", dynamic_stake_cap_ars=500.0)
    assert leg.live_max_stake_ars == 500.0
    q_betsson = _quote("betsson", "home", 2.1, max_stake=None)
    leg2 = leg_from_quote(q_betsson, 50.0, match_id="MKT-1", dynamic_stake_cap_ars=500.0)
    assert leg2.live_max_stake_ars is None  # non-dynamic: no fallback cap
    # a quote that already carries a cap keeps it
    q_capped = _quote("betano", "away", 1.95, max_stake=1234.0)
    assert leg_from_quote(q_capped, 50.0, match_id="M", dynamic_stake_cap_ars=500.0).live_max_stake_ars == 1234.0
