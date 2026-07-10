"""Tests for the detector→executor bridge (OddsQuote → Leg mapping + execution)."""

from __future__ import annotations

import pytest
import structlog.testing

from src.arbitrage.dutch_book import ArbitrageOpportunity, detect_arbitrage
from src.arbitrage.quotes import OddsQuote
from src.execution.arb_executor import (
    execute_opportunity,
    leg_from_quote,
    legs_from_opportunity,
    order_opportunity_for_execution,
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
    assert (
        leg_from_quote(q_capped, 50.0, match_id="M", dynamic_stake_cap_ars=500.0).live_max_stake_ars
        == 1234.0
    )


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


# ---- residual recapture end-to-end through the bridge ----


def _two_way_opp() -> ArbitrageOpportunity:
    """2-leg arb: betsson HOME 2.5 + betano AWAY 2.5 (overround 0.8). At budget
    1000, allocate_maxmin stakes each leg 500."""
    opp = detect_arbitrage(
        [_quote("betsson", "home", 2.5), _quote("betano", "away", 2.5)], 1000.0, 1.0
    )
    assert opp is not None
    return opp


class _RejectOncePlacer:
    """Rejects the first place() with odds_rejected, accepts thereafter."""

    def __init__(self) -> None:
        self.legs: list[Leg] = []
        self._rejected = False

    async def place(self, leg: Leg) -> PlacementResult:
        self.legs.append(leg)
        if not self._rejected:
            self._rejected = True
            return PlacementResult(accepted=False, odds_rejected=True, detail="invalid odds")
        return PlacementResult(accepted=True, stake_filled=leg.stake_ars, odds_filled=leg.odds)


class _OverfillPlacer:
    """Accepts but reports a FIXED stake_filled (to exhaust the residual budget)."""

    def __init__(self, fill: float) -> None:
        self.fill = fill

    async def place(self, leg: Leg) -> PlacementResult:
        return PlacementResult(accepted=True, stake_filled=self.fill, odds_filled=leg.odds)


def _reverify_drift_on(*, drift_call: int, drift_odds: float):
    """reverify that returns leg.odds everywhere EXCEPT the ``drift_call``-th call,
    which returns ``drift_odds``. In these tests ``drift_call`` is set to leg B's
    recapture refetch call (the passing assertions pin that ordinal), so the
    residual closure sees the drifted fresh odds."""

    state = {"c": 0}

    async def reverify(leg: Leg) -> float:
        state["c"] += 1
        return drift_odds if state["c"] == drift_call else leg.odds

    return reverify


async def test_residual_recapture_completes_with_resized_stake() -> None:
    """Leg B (betano) odds_rejected once → the bridge's _residual closure runs
    allocate_residual around leg A's fill → leg B's second POST carries the
    residual-solver stake/odds → COMPLETED (no naked). Hand-computed: A fills
    500 @ 2.5 → payout 1250; B re-fetched at 3.0 → stake 1250/3 ≈ 416.67."""
    opp = _two_way_opp()
    bp = _Placer()  # betsson (leg A) accepts
    rp = _RejectOncePlacer()  # betano (leg B) rejects once, then accepts
    ex = Executor(
        guardrails=_guard(),
        notifier=_Notifier(),
        recovery=_Recovery(),
        placers={"betsson": bp, "betano": rp},
        reverify=_reverify_drift_on(drift_call=5, drift_odds=3.0),
    )
    res = await execute_opportunity(ex, opp, opp_id="t", budget=1000.0, min_margin_pct=1.0)
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert bp.legs[0].stake_ars == pytest.approx(500.0)  # leg A at the revalidated stake
    assert rp.legs[1].odds == 3.0  # leg B re-POSTed at the fresh odds
    assert rp.legs[1].stake_ars == pytest.approx(1250.0 / 3.0, rel=1e-4)  # residual stake


async def test_residual_unprofitable_fresh_odds_is_naked() -> None:
    """Leg B's recapture fresh odds collapse to 1.01 → no hedge locks ≥ 0 →
    residual returns None → today's NAKED EXPOSURE (leg A live), B never re-POSTed."""
    opp = _two_way_opp()
    bp = _Placer()
    rp = _RejectOncePlacer()
    ex = Executor(
        guardrails=_guard(),
        notifier=_Notifier(),
        recovery=_Recovery(),
        placers={"betsson": bp, "betano": rp},
        reverify=_reverify_drift_on(drift_call=5, drift_odds=1.01),
    )
    res = await execute_opportunity(ex, opp, opp_id="t", budget=1000.0, min_margin_pct=1.0)
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE
    assert len(rp.legs) == 1  # B rejected once; recapture refused, never re-POSTed


async def test_residual_budget_exhausted_is_naked() -> None:
    """Leg A's reported fill consumes the whole budget → budget_remaining (= budget
    − placed_total) ≤ 0 → allocate_residual returns None → NAKED EXPOSURE. This
    branch can't arise from a normal allocation (each leg stakes < budget), so the
    placer over-reports A's fill to reach it — it's a defensive-guard test."""
    opp = _two_way_opp()
    bp = _OverfillPlacer(fill=1000.0)  # betsson (leg A) over-reports stake_filled
    rp = _RejectOncePlacer()  # betano (leg B) rejects once
    ex = Executor(
        guardrails=_guard(),
        notifier=_Notifier(),
        recovery=_Recovery(),
        placers={"betsson": bp, "betano": rp},
    )
    res = await execute_opportunity(ex, opp, opp_id="t", budget=1000.0, min_margin_pct=1.0)
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE
    assert len(rp.legs) == 1  # B rejected; no salvageable budget left → never re-POSTed


async def test_revalidate_logs_fresh_odds_and_roi_on_abort() -> None:
    """Edge gone at reverify → the arb_executor.revalidated line carries the
    fresh odds and margin, so the abort is self-explanatory without scrollback."""

    async def reverify(leg: Leg) -> float:
        return 1.6  # overround 1.25 → no arb

    ex = Executor(
        guardrails=_guard(),
        notifier=_Notifier(),
        recovery=_Recovery(),
        placers={"betsson": _Placer(), "betano": _Placer()},
        reverify=reverify,
    )
    with structlog.testing.capture_logs() as logs:
        res = await execute_opportunity(
            ex, _two_way_opp(), opp_id="opp-rv1", budget=1000.0, min_margin_pct=1.0
        )
    assert res.outcome is ExecutionOutcome.ABORTED
    entry = next(e for e in logs if e["event"] == "arb_executor.revalidated")
    assert entry["opp_id"] == "opp-rv1"
    assert entry["original_odds"] == [2.5, 2.5]
    assert entry["current_odds"] == [1.6, 1.6]
    assert entry["fresh_margin_pct"] == pytest.approx(-25.0)
    assert entry["repriced_roi_pct"] is None


async def test_revalidate_logs_repriced_roi_when_edge_survives() -> None:
    """Edge survives at drifted-but-profitable odds → the line carries the
    post-allocation ROI of the re-priced arb."""

    async def reverify(leg: Leg) -> float:
        return 2.4  # overround 0.8333 → margin 16.667%, realized ROI 20%

    ex = Executor(
        guardrails=_guard(),
        notifier=_Notifier(),
        recovery=_Recovery(),
        placers={"betsson": _Placer(), "betano": _Placer()},
        reverify=reverify,
    )
    with structlog.testing.capture_logs() as logs:
        res = await execute_opportunity(
            ex, _two_way_opp(), opp_id="opp-rv2", budget=1000.0, min_margin_pct=1.0
        )
    assert res.outcome is ExecutionOutcome.COMPLETED
    entry = next(e for e in logs if e["event"] == "arb_executor.revalidated")
    assert entry["current_odds"] == [2.4, 2.4]
    assert entry["fresh_margin_pct"] == pytest.approx((1.0 - 2.0 / 2.4) * 100.0, abs=1e-3)
    assert entry["repriced_roi_pct"] == pytest.approx(20.0, abs=1e-3)


# ---- leg placement ordering (order_opportunity_for_execution) ----


def _opp_real(legs: tuple[OddsQuote, ...], stakes: tuple[float, ...]) -> ArbitrageOpportunity:
    """Build an opportunity with REAL platform ids (the live -pba suffixes) so the
    fragile-auth / single-leg / stake ordering keys actually match."""
    return ArbitrageOpportunity(
        legs=legs,
        stakes=stakes,
        total_stake=sum(stakes),
        guaranteed_profit=10.0,
        margin_pct=3.0,
        realized_roi_pct=3.0,
        capital_utilization=1.0,
    )


def _q(platform: str, outcome: str, odds: float) -> OddsQuote:
    return OddsQuote(
        platform=platform,
        market_id="fx-1|1x2",
        outcome=outcome,
        decimal_odds=odds,
        max_stake=5000.0,
        timestamp=0.0,
        platform_outcome_id=f"{platform}-{outcome}",
        platform_event_id=f"{platform}-evt",
    )


def test_ordering_betsson_pair_plus_bw_is_bw_first_then_betsson_by_stake() -> None:
    """fx-366d0143f674 shape: BW HOME (large stake) + Betsson AWAY 744 + Betsson DRAW
    1633. Rule 1 (fragile-auth first) dominates rule 3's cross-platform stake ordering;
    within the Betsson pair, smaller stake first (rule 3)."""
    bw = _q("betwarrior-pba", "home", 1.94)
    bs_away = _q("betsson-pba", "away", 6.8)
    bs_draw = _q("betsson-pba", "draw", 3.1)
    opp = _opp_real((bw, bs_away, bs_draw), (2000.0, 744.0, 1633.0))
    ordered = order_opportunity_for_execution(opp)
    assert [q.platform for q in ordered.legs] == [
        "betwarrior-pba",
        "betsson-pba",
        "betsson-pba",
    ]
    assert ordered.legs[0].outcome == "home"  # BW leg preserved
    assert ordered.stakes == (2000.0, 744.0, 1633.0)  # AWAY (744) before DRAW (1633)


def test_ordering_bw_pair_plus_betsson_is_smaller_bw_first() -> None:
    """fx-ee45e93fd258 (the completed arb) shape: two BW legs + one Betsson. The
    completed-live order was BW 563.91, BW 676.69, Betsson — exactly what the key
    produces (both fragile-auth first, then smaller stake, then Betsson)."""
    bw_small = _q("betwarrior-pba", "home", 1.9)
    bw_large = _q("betwarrior-pba", "away", 2.0)
    bs = _q("betsson-pba", "draw", 4.0)
    opp = _opp_real((bw_small, bw_large, bs), (563.91, 676.69, 300.0))
    ordered = order_opportunity_for_execution(opp)
    assert [q.platform for q in ordered.legs] == [
        "betwarrior-pba",
        "betwarrior-pba",
        "betsson-pba",
    ]
    assert ordered.stakes == (563.91, 676.69, 300.0)  # smaller BW stake first


def test_ordering_no_bw_uses_single_leg_platform_then_stake() -> None:
    """No fragile-auth platform: rule 2 (single-leg platform before a same-platform
    pair) dominates. Betano (single leg) first, then the Betsson pair by stake
    ascending — pins rule 2 without rule 1."""
    ba = _q("betano-pba", "home", 2.0)
    bs_a = _q("betsson-pba", "away", 3.5)
    bs_b = _q("betsson-pba", "draw", 3.5)
    opp = _opp_real((ba, bs_a, bs_b), (900.0, 1633.0, 744.0))
    ordered = order_opportunity_for_execution(opp)
    assert [q.platform for q in ordered.legs] == ["betano-pba", "betsson-pba", "betsson-pba"]
    assert ordered.stakes == (900.0, 744.0, 1633.0)  # Betsson pair ascending


def test_ordering_preserves_scalar_fields_and_permutes_stakes_with_legs() -> None:
    """The permutation touches ONLY legs+stakes; all scalar fields are order-invariant
    and stay identical (replace keeps them)."""
    bw = _q("betwarrior-pba", "home", 2.0)
    bs = _q("betsson-pba", "away", 2.0)
    opp = _opp_real((bs, bw), (100.0, 90.0))
    ordered = order_opportunity_for_execution(opp)
    assert [q.platform for q in ordered.legs] == ["betwarrior-pba", "betsson-pba"]
    assert ordered.stakes == (90.0, 100.0)  # permuted WITH the legs
    # Scalars unchanged.
    assert ordered.total_stake == opp.total_stake
    assert ordered.guaranteed_profit == opp.guaranteed_profit
    assert ordered.margin_pct == opp.margin_pct
    assert ordered.realized_roi_pct == opp.realized_roi_pct
    assert ordered.capital_utilization == opp.capital_utilization


def test_ordering_equal_keys_keep_detector_order() -> None:
    """Two legs on different single-leg platforms with equal stakes: the stable
    tie-break (detector index) keeps the original order."""
    a = _q("betsson-pba", "home", 2.0)
    b = _q("betano-pba", "away", 2.0)
    opp = _opp_real((a, b), (100.0, 100.0))
    ordered = order_opportunity_for_execution(opp)
    assert [q.platform for q in ordered.legs] == ["betsson-pba", "betano-pba"]


def test_ordering_staleness_rank_puts_laggard_first() -> None:
    """With staleness_rank, the laggard (perishable stale quote) is placed first
    among non-fragile-auth legs. Betsson-pba (rank 0.9) before Betano (rank 0.1),
    equal stakes to isolate the staleness term."""
    a = _q("betano", "home", 2.0)
    b = _q("betsson-pba", "away", 2.0)
    opp = _opp_real((a, b), (100.0, 100.0))
    rank = {"1x2": {"betano": 0.1, "betsson-pba": 0.9}}
    ordered = order_opportunity_for_execution(opp, staleness_rank=rank)
    assert ordered.legs[0].platform == "betsson-pba"


def test_ordering_staleness_rank_dominated_by_fragile_auth() -> None:
    """BW (fragile-auth) still comes first even when it's the leader (low staleness).
    Naked-incident evidence beats latency statistics."""
    a = _q("betwarrior-pba", "home", 2.0)
    b = _q("betsson-pba", "away", 2.0)
    opp = _opp_real((a, b), (100.0, 100.0))
    rank = {"1x2": {"betwarrior-pba": 0.0, "betsson-pba": 0.9}}
    ordered = order_opportunity_for_execution(opp, staleness_rank=rank)
    assert ordered.legs[0].platform == "betwarrior-pba"


def test_ordering_staleness_rank_missing_type_falls_back() -> None:
    """A market type absent from the staleness_rank mapping → today's order
    (staleness term constantly 0.0 for every leg)."""
    a = _q("betano", "home", 2.0)
    b = _q("betsson-pba", "away", 2.0)
    opp = _opp_real((a, b), (100.0, 100.0))
    # rank has "btts" but the opp's market_id suffix is "1x2"
    rank = {"btts": {"betano": 0.1, "betsson-pba": 0.9}}
    ordered = order_opportunity_for_execution(opp, staleness_rank=rank)
    # Without staleness: single-leg platforms, equal stakes → detector order
    assert [q.platform for q in ordered.legs] == ["betano", "betsson-pba"]


def test_ordering_staleness_rank_none_is_today_behavior() -> None:
    """staleness_rank=None (default) is byte-identical to today's ordering."""
    a = _q("betano", "home", 2.0)
    b = _q("betsson-pba", "away", 2.0)
    opp = _opp_real((a, b), (100.0, 100.0))
    ordered_default = order_opportunity_for_execution(opp)
    ordered_none = order_opportunity_for_execution(opp, staleness_rank=None)
    assert [q.platform for q in ordered_default.legs] == [q.platform for q in ordered_none.legs]
