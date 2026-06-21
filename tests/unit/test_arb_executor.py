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
    res = await execute_opportunity(
        ex, _opp(stakes=(100.0, 100.0)), opp_id="opp-1", budget=200.0, min_margin_pct=1.0
    )
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert len(bp.legs) == 1 and len(ap.legs) == 1  # each platform got its leg
    assert ap.legs[0].live_max_stake_ars == 5000.0  # dynamic cap carried through


async def test_execute_opportunity_places_three_leg_arb() -> None:
    """A 1X2 (three-outcome) arb now executes — all three legs route + complete."""
    # Real 1X2 arb (overround < 1) — the re-pricing closure re-runs
    # `detect_arbitrage` at the fresh odds, so the legs must form a genuine arb.
    legs = (
        _quote("betsson", "home", 2.5),
        _quote("betano", "away", 3.5),
        _quote("bplay", "draw", 5.0),
    )
    opp = ArbitrageOpportunity(
        legs=legs,
        stakes=(90.0, 64.0, 45.0),
        total_stake=199.0,
        guaranteed_profit=25.0,
        margin_pct=11.4,
        realized_roi_pct=12.9,
        capital_utilization=0.995,
    )
    bp, ap, dp = _Placer(), _Placer(), _Placer()
    ex = Executor(
        guardrails=_guard(),
        notifier=_Notifier(),
        recovery=_Recovery(),
        placers={"betsson": bp, "betano": ap, "bplay": dp},
    )
    res = await execute_opportunity(ex, opp, opp_id="opp-3", budget=200.0, min_margin_pct=1.0)
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert len(res.legs) == 3  # all three filled
    assert len(bp.legs) == len(ap.legs) == len(dp.legs) == 1  # each platform got its leg


async def test_execute_opportunity_completes_at_5000_budget_under_tight_per_match_cap() -> None:
    """Phase B: budget raised to 5000 and per-match cap set equal to it. A 3-leg
    arb allocates the full budget and clears the strict-`>` per-match guard —
    guards against the cap silently binding at the new aggression level."""
    legs = (
        _quote("betsson", "home", 2.5),
        _quote("betano", "away", 3.5),
        _quote("bplay", "draw", 5.0),
    )
    opp = ArbitrageOpportunity(
        legs=legs,
        stakes=(90.0, 64.0, 45.0),  # pre-reprice; _revalidate re-sizes to budget
        total_stake=199.0,
        guaranteed_profit=25.0,
        margin_pct=11.4,
        realized_roi_pct=12.9,
        capital_utilization=0.995,
    )
    bp, ap, dp = _Placer(), _Placer(), _Placer()
    guard = Guardrails(
        max_position_per_match_ars=5000.0,  # == budget: strict `>` must let the full arb through
        max_total_exposure_ars=15000.0,
        max_daily_loss_ars=5000.0,
        odds_tolerance_pct=100.0,
    )
    ex = Executor(
        guardrails=guard,
        notifier=_Notifier(),
        recovery=_Recovery(),
        placers={"betsson": bp, "betano": ap, "bplay": dp},
    )
    res = await execute_opportunity(ex, opp, opp_id="opp-B", budget=5000.0, min_margin_pct=1.0)
    assert res.outcome is ExecutionOutcome.COMPLETED
    total = sum(r.stake_filled for r in res.legs)
    assert total == pytest.approx(5000.0, rel=1e-6)


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


def _two_leg_opp_betano_uncapped() -> ArbitrageOpportunity:
    """2-leg arb whose Betano leg carries no max_stake — its cap is the dynamic
    fallback (300) until a live cap_refresh overrides it. Same odds both sides →
    each leg's Dutch-book stake at budget 1000 is exactly 500 (> 300, so the
    fallback binds when no live cap is read)."""
    legs = (
        _quote("betsson", "home", 2.1),
        _quote("betano", "away", 2.1, max_stake=None),
    )
    return ArbitrageOpportunity(
        legs=legs,
        stakes=(100.0, 100.0),
        total_stake=200.0,
        guaranteed_profit=5.0,
        margin_pct=5.0,
        realized_roi_pct=5.0,
        capital_utilization=1.0,
    )


async def _execute_with_betano_cap(cap_for_betano: float | None) -> tuple[object, _Placer]:
    bp, ap = _Placer(), _Placer()

    async def cap_refresh(leg: Leg) -> float | None:
        return cap_for_betano if leg.platform == "betano" else None

    ex = Executor(
        guardrails=_guard(),
        notifier=_Notifier(),
        recovery=_Recovery(),
        placers={"betsson": bp, "betano": ap},
        cap_refresh=cap_refresh,
    )
    res = await execute_opportunity(
        ex,
        _two_leg_opp_betano_uncapped(),
        opp_id="opp-cap",
        budget=1000.0,
        min_margin_pct=1.0,
        dynamic_stake_cap_ars=300.0,
    )
    return res, ap


async def test_live_cap_lets_betano_size_past_static_fallback() -> None:
    """Phase C: a live cap (70M, Betano's real per-bet ceiling) lets the Betano
    leg size to its Dutch-book stake (500) — PAST the 300 static fallback. This
    only completes if the re-sized leg carries the live cap into check_leg;
    without that linkage the guardrail would abort 500 > 300. Regression catcher
    for the live_max_stake_ars=q.max_stake fix in _revalidate."""
    res, ap = await _execute_with_betano_cap(70_000_000.0)
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert ap.legs[0].stake_ars == pytest.approx(500.0)


async def test_no_live_cap_falls_back_to_static_cap() -> None:
    """Without a live cap the Betano leg is bound by the 300 static fallback
    (Dutch-book stake 500 > 300 → allocate_maxmin scales to 300), never placed
    naked. Contrast with the live-cap test above (500 vs 300) — the delta is
    exactly the aggression Phase C unlocks on Betano."""
    res, ap = await _execute_with_betano_cap(None)
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert ap.legs[0].stake_ars == pytest.approx(300.0)
