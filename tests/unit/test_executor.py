"""Tests for the N-leg execution state machine (dry-run)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from src.arbitrage.dutch_book import detect_arbitrage
from src.arbitrage.quotes import OddsQuote
from src.execution.arb_executor import execute_opportunity
from src.execution.executor import (
    DryRunPlacer,
    ExecutionOutcome,
    Executor,
    Leg,
    PlacementResult,
)
from src.execution.guardrails import Guardrails
from src.execution.recovery import RecoveryOutcome


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


class _FakeRecovery:
    def __init__(self, outcome: RecoveryOutcome = RecoveryOutcome.UNRESOLVED) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    async def recover(self, reason: str) -> RecoveryOutcome:
        self.calls.append(reason)
        return self.outcome


class _CountingPlacer:
    """DryRunPlacer that counts calls and can reject a chosen leg index."""

    def __init__(self, reject_index: int | None = None) -> None:
        self.reject_index = reject_index
        self.calls = 0

    async def place(self, leg: Leg) -> PlacementResult:
        i = self.calls
        self.calls += 1
        if self.reject_index is not None and i == self.reject_index:
            return PlacementResult(accepted=False, detail="rejected by book")
        return PlacementResult(
            accepted=True, stake_filled=leg.stake_ars, odds_filled=leg.odds, ref=f"ref{i}"
        )


class _RaisingPlacer:
    async def place(self, leg: Leg) -> PlacementResult:
        raise RuntimeError("session expired")


class _PendingUnknownPlacer:
    """DryRunPlacer that returns a pending_unknown result at a chosen leg index
    (bet submitted, acceptance unconfirmed) and fills the rest normally."""

    def __init__(self, pending_index: int) -> None:
        self.pending_index = pending_index
        self.calls = 0

    async def place(self, leg: Leg) -> PlacementResult:
        i = self.calls
        self.calls += 1
        if i == self.pending_index:
            return PlacementResult(
                accepted=False,
                pending_unknown=True,
                detail="betwarrior: LIVE_DELAY_PENDING unresolved (couponRef 777)",
            )
        return PlacementResult(
            accepted=True, stake_filled=leg.stake_ars, odds_filled=leg.odds, ref=f"ref{i}"
        )


def _guard(**over: float) -> Guardrails:
    base = {
        "max_position_per_match_ars": 10_000.0,
        "max_total_exposure_ars": 100_000.0,
        "max_daily_loss_ars": 5_000.0,
        "odds_tolerance_pct": 1.0,
    }
    base.update(over)
    return Guardrails(**base)  # type: ignore[arg-type]


def _legs() -> tuple[Leg, Leg]:
    # Cross-platform arb on one match: both legs share match_id.
    return (
        Leg("betsson", "m1", "1X2", "home", 100.0, 2.0),
        Leg("betwarrior", "m1", "1X2", "away", 100.0, 2.1),
    )


def _three_legs() -> tuple[Leg, Leg, Leg]:
    # A 1X2 (three-outcome) arb on one match — three legs sharing match_id. Betano is
    # a dynamic-cap platform, so its leg carries a live_max_stake_ars (as the real flow
    # supplies) or the guardrail fail-closes.
    return (
        Leg("betsson", "m1", "1X2", "home", 60.0, 3.0),
        Leg("betano", "m1", "1X2", "draw", 60.0, 3.1, live_max_stake_ars=5000.0),
        Leg("betwarrior", "m1", "1X2", "away", 60.0, 3.2),
    )


def _executor(
    g: Guardrails,
    n: _FakeNotifier,
    rec: _FakeRecovery,
    placer: object,
    reverify: Callable[[Leg], Awaitable[float]] | None = None,
    cap_refresh: Callable[[Leg], Awaitable[float | None]] | None = None,
    auth_precheck: Callable[[Leg], Awaitable[bool]] | None = None,
    reauth: Callable[[Leg], Awaitable[bool]] | None = None,
) -> Executor:
    kw: dict[str, object] = {
        "guardrails": g,
        "notifier": n,
        "recovery": rec,
        "placer": placer,
    }
    if reverify is not None:
        kw["reverify"] = reverify
    if cap_refresh is not None:
        kw["cap_refresh"] = cap_refresh
    if auth_precheck is not None:
        kw["auth_precheck"] = auth_precheck
    if reauth is not None:
        kw["reauth"] = reauth
    return Executor(**kw)  # type: ignore[arg-type]


async def test_happy_path_completes_and_records_exposure() -> None:
    g, n = _guard(), _FakeNotifier()
    res = await _executor(g, n, _FakeRecovery(), DryRunPlacer()).execute_two_leg("opp1", *_legs())
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert g.total_exposure_ars == 200.0
    assert any("COMPLETE" in t for t in n.sent)


async def test_three_leg_arb_completes_and_places_all_three() -> None:
    g, n = _guard(), _FakeNotifier()
    res = await _executor(g, n, _FakeRecovery(), DryRunPlacer()).execute_n_leg(
        "opp", list(_three_legs())
    )
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert len(res.legs) == 3
    assert sum(1 for t in n.sent if "BET PLACED" in t) == 3  # one alert per leg
    assert g.total_exposure_ars == 180.0  # 3 × 60


async def test_three_leg_third_leg_rejected_is_naked_with_two_live() -> None:
    """The new failure mode: a 3-leg arb where the LAST leg fails leaves TWO legs
    live + unhedged — reported as naked exposure with the live-leg count."""
    g, n = _guard(), _FakeNotifier()
    placer = _CountingPlacer(reject_index=2)  # leg C rejected
    res = await _executor(g, n, _FakeRecovery(), placer).execute_n_leg("opp", list(_three_legs()))
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE
    assert len(res.legs) == 2  # two legs live
    assert any("NAKED" in t and "2 leg" in t for t in n.sent)


async def test_pending_unknown_on_first_leg_halts_not_abort() -> None:
    """A LIVE_DELAY_PENDING that doesn't resolve is NOT a clean reject: the bet may
    be placed. Leg A pending_unknown → PENDING_UNKNOWN (kill switch tripped + alert),
    never ABORTED (which would falsely claim 'nothing placed' and hide a live position)."""
    g, n = _guard(), _FakeNotifier()
    placer = _PendingUnknownPlacer(pending_index=0)
    res = await _executor(g, n, _FakeRecovery(), placer).execute_two_leg("opp", *_legs())
    assert res.outcome is ExecutionOutcome.PENDING_UNKNOWN
    assert g.kill_switch_tripped  # auto-placement halted
    assert any("PENDING UNKNOWN" in t for t in n.sent)
    assert placer.calls == 1  # never placed leg B


async def test_pending_unknown_after_live_leg_is_naked() -> None:
    """Leg A placed, leg B pending_unknown: a confirmed live leg + an unconfirmed one
    is naked exposure (the hedge is incomplete either way)."""
    g, n = _guard(), _FakeNotifier()
    placer = _PendingUnknownPlacer(pending_index=1)
    res = await _executor(g, n, _FakeRecovery(), placer).execute_two_leg("opp", *_legs())
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE
    assert len(res.legs) == 1  # leg A confirmed live
    assert any("NAKED" in t for t in n.sent)


def _incident_legs() -> tuple[Leg, Leg, Leg]:
    # Mirror the 2026-06-20 naked-exposure incident: two betsson legs then betwarrior
    # (Kambi) last — the leg whose server-dead bearer 401'd. All share one match.
    return (
        Leg("betsson-pba", "m1", "1X2", "home", 60.0, 3.0),
        Leg("betsson-pba", "m1", "1X2", "draw", 60.0, 3.1),
        Leg("betwarrior-pba", "m1", "1X2", "away", 60.0, 3.2),
    )


async def test_pre_place_auth_gate_aborts_before_placing_anything() -> None:
    """The 2026-06-20 naked-exposure incident, fixed: a betwarrior (Kambi) leg whose
    session is server-dead (auth_precheck ⇒ False) aborts BEFORE leg A is placed —
    zero position. Without the D2 gate the betsson legs A/B would place and the dead
    leg C would leave them unhedged (NAKED)."""
    g, n = _guard(), _FakeNotifier()
    placer = _CountingPlacer()

    # Auth dead ONLY on the betwarrior (Kambi) leg — exactly the wiring's behaviour.
    async def auth_dead_on_betwarrior(leg: Leg) -> bool:
        return leg.platform.split("-", 1)[0].lower() != "betwarrior"

    ex = _executor(g, n, _FakeRecovery(), placer, auth_precheck=auth_dead_on_betwarrior)
    res = await ex.execute_n_leg("opp", list(_incident_legs()))
    assert res.outcome is ExecutionOutcome.ABORTED
    assert "session auth not live" in res.reason
    assert placer.calls == 0  # NOTHING placed — no naked exposure


async def test_pre_place_auth_gate_live_session_places_normally() -> None:
    """A live session (auth_precheck ⇒ True on every leg) is invisible to execution:
    the arb completes exactly as without the gate. Guards against the gate firing by
    accident on a healthy session."""
    g, n = _guard(), _FakeNotifier()
    placer = _CountingPlacer()

    async def auth_always_live(leg: Leg) -> bool:
        return True

    ex = _executor(g, n, _FakeRecovery(), placer, auth_precheck=auth_always_live)
    res = await ex.execute_n_leg("opp", list(_incident_legs()))
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert placer.calls == 3  # all legs placed


async def test_placed_bet_alert_describes_leg_platform_event_and_bet() -> None:
    g, n = _guard(), _FakeNotifier()
    leg_a, leg_b = _legs()
    await _executor(g, n, _FakeRecovery(), DryRunPlacer()).execute_two_leg("opp1", leg_a, leg_b)
    placed = [t for t in n.sent if "BET PLACED" in t]
    assert len(placed) == 2  # one per leg
    a = next(t for t in placed if "Leg A" in t)
    assert leg_a.platform in a  # platform
    assert leg_a.market in a  # event/market
    assert leg_a.outcome in a  # what bet
    assert "BET PLACED — Leg B" in "\n".join(placed)  # leg attribution


async def test_precheck_failure_places_nothing() -> None:
    g = _guard(max_position_per_match_ars=50.0)  # 100 > 50 → deny Leg A
    placer = _CountingPlacer()
    res = await _executor(g, _FakeNotifier(), _FakeRecovery(), placer).execute_two_leg(
        "o", *_legs()
    )
    assert res.outcome is ExecutionOutcome.ABORTED
    assert placer.calls == 0
    assert g.total_exposure_ars == 0.0


async def test_odds_drift_in_precheck_aborts_before_placing() -> None:
    g = _guard()
    placer = _CountingPlacer()

    async def drift(leg: Leg) -> float:
        return leg.odds * 0.9  # 10% drop, beyond 1% tolerance

    res = await _executor(g, _FakeNotifier(), _FakeRecovery(), placer, drift).execute_two_leg(
        "o", *_legs()
    )
    assert res.outcome is ExecutionOutcome.ABORTED
    assert placer.calls == 0


async def test_leg_a_rejected_aborts_nothing_at_risk() -> None:
    g = _guard()
    res = await _executor(
        g, _FakeNotifier(), _FakeRecovery(), _CountingPlacer(reject_index=0)
    ).execute_two_leg("o", *_legs())
    assert res.outcome is ExecutionOutcome.ABORTED
    assert g.total_exposure_ars == 0.0


async def test_leg_b_rejected_is_naked_exposure() -> None:
    g, n = _guard(), _FakeNotifier()
    res = await _executor(g, n, _FakeRecovery(), _CountingPlacer(reject_index=1)).execute_two_leg(
        "o", *_legs()
    )
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE
    assert g.total_exposure_ars == 100.0  # Leg A is live
    assert any("NAKED" in t for t in n.sent)


async def test_leg_b_drift_after_leg_a_is_naked_exposure() -> None:
    g = _guard()
    state = {"n": 0}

    async def reverify(leg: Leg) -> float:
        state["n"] += 1
        # calls 1,2 = pre-check A,B; 3 = placement A (ok); 4 = placement B drifts (A already live)
        return leg.odds * 0.9 if state["n"] == 4 else leg.odds

    res = await _executor(
        g, _FakeNotifier(), _FakeRecovery(), DryRunPlacer(), reverify
    ).execute_two_leg("o", *_legs())
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE
    assert g.total_exposure_ars == 100.0


async def test_places_at_reverified_current_odds() -> None:
    """The leg is placed at the LIVE re-verified odds (within tolerance), not the
    stale detection odds — required for books that demand exact-current odds."""
    g, n = _guard(odds_tolerance_pct=5.0), _FakeNotifier()  # allow a small move

    async def reverify(leg: Leg) -> float:
        return leg.odds * 0.98  # 2% drift, within the 5% tolerance

    placer = _CountingPlacer()
    res = await _executor(g, n, _FakeRecovery(), placer, reverify).execute_two_leg("o", *_legs())
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert res.legs[0].odds_filled == _legs()[0].odds * 0.98  # placed at current, not detection


async def test_unverifiable_odds_aborts_before_placing() -> None:
    """A leg whose odds can't be confirmed (reverify → 0.0) fails acceptance → abort,
    nothing placed (fail-closed)."""
    g, n = _guard(), _FakeNotifier()

    async def reverify(leg: Leg) -> float:
        return 0.0  # unverifiable (no refresher / fetch error / market gone)

    placer = _CountingPlacer()
    res = await _executor(g, n, _FakeRecovery(), placer, reverify).execute_two_leg("o", *_legs())
    assert res.outcome is ExecutionOutcome.ABORTED
    assert placer.calls == 0  # never placed


async def test_unexpected_error_freezes_and_trips_kill_switch() -> None:
    g, n = _guard(), _FakeNotifier()
    rec = _FakeRecovery(RecoveryOutcome.UNRESOLVED)
    res = await _executor(g, n, rec, _RaisingPlacer()).execute_two_leg("o", *_legs())
    assert res.outcome is ExecutionOutcome.FROZEN
    assert rec.calls and g.kill_switch_tripped
    assert any("FROZEN" in t for t in n.sent)


async def test_recovery_resolved_does_not_freeze() -> None:
    g = _guard()
    rec = _FakeRecovery(RecoveryOutcome.RESOLVED)
    res = await _executor(g, _FakeNotifier(), rec, _RaisingPlacer()).execute_two_leg("o", *_legs())
    assert res.outcome is ExecutionOutcome.ABORTED
    assert not g.kill_switch_tripped


async def test_kill_switch_blocks_execution() -> None:
    g = _guard()
    g.trip_kill_switch("manual")
    placer = _CountingPlacer()
    res = await _executor(g, _FakeNotifier(), _FakeRecovery(), placer).execute_two_leg(
        "o", *_legs()
    )
    assert res.outcome is ExecutionOutcome.ABORTED
    assert placer.calls == 0


async def test_routes_each_leg_to_its_platform_placer() -> None:
    # Cross-platform arb: each leg must go to its own platform's placer.
    g, n = _guard(), _FakeNotifier()
    pa, pb = _CountingPlacer(), _CountingPlacer()
    leg_a, leg_b = _legs()  # betsson, betwarrior
    res = await Executor(
        guardrails=g,
        notifier=n,
        recovery=_FakeRecovery(),
        placers={"betsson": pa, "betwarrior": pb},
    ).execute_two_leg("opp", leg_a, leg_b)
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert pa.calls == 1 and pb.calls == 1  # each placer handled exactly its own leg


async def test_aborts_when_a_platform_has_no_placer() -> None:
    # Missing placer for one leg → abort before placing anything (no naked leg).
    g, n = _guard(), _FakeNotifier()
    pa = _CountingPlacer()
    leg_a, leg_b = _legs()  # betsson, betwarrior
    res = await Executor(
        guardrails=g,
        notifier=n,
        recovery=_FakeRecovery(),
        placers={"betsson": pa},  # no betwarrior placer
    ).execute_two_leg("opp", leg_a, leg_b)
    assert res.outcome is ExecutionOutcome.ABORTED and "no placer" in res.reason
    assert pa.calls == 0  # nothing placed


# ---- re-pricing at fresh reverify odds (capture drifted arbs) ----


def _arb1_quotes(*, home_max_stake: float = 5000.0) -> tuple[OddsQuote, OddsQuote, OddsQuote]:
    """Arb-1 shape: betwarrior AWAY 2.75 + DRAW 2.75, betsson HOME 4.6.
    A real ~5.5% 1X2 arb (overround 0.9446); AWAY/DRAW carry a large max_stake so
    the re-sized allocation clears the guardrail. ``home_max_stake`` lets a test
    bind the HOME leg's cap (default uncapped)."""
    return (
        OddsQuote(
            platform="betwarrior",
            market_id="m",
            outcome="away",
            decimal_odds=2.75,
            max_stake=5000.0,
            timestamp=0.0,
        ),
        OddsQuote(
            platform="betwarrior",
            market_id="m",
            outcome="draw",
            decimal_odds=2.75,
            max_stake=5000.0,
            timestamp=0.0,
        ),
        OddsQuote(
            platform="betsson",
            market_id="m",
            outcome="home",
            decimal_odds=4.6,
            max_stake=home_max_stake,
            timestamp=0.0,
        ),
    )


async def test_reprice_captures_drifted_but_still_profitable_arb() -> None:
    """DRAW drifts 2.75→2.55 at reverify. The arb is still profitable at the
    fresh odds (overround 0.9732, margin 2.68%) → re-price + re-size + PLACE,
    instead of aborting on the 1% per-leg tolerance. The DRAW fill lands at the
    fresh 2.55 odds with re-sized stakes, and every outcome still pays ≥ total."""
    g, n = _guard(), _FakeNotifier()
    opp = detect_arbitrage(list(_arb1_quotes()), 200.0, 1.0)
    assert opp is not None  # sanity: the snapshot is a real arb

    async def reverify(leg: Leg) -> float:
        if leg.outcome == "draw":
            return 2.55  # drifted 7.3% — past the 1% tolerance but still an arb
        return leg.odds

    placer = _CountingPlacer()
    ex = _executor(g, n, _FakeRecovery(), placer, reverify)
    res = await execute_opportunity(ex, opp, opp_id="t", budget=200.0, min_margin_pct=1.0)
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert placer.calls == 3
    fills = list(res.legs)
    total = sum(f.stake_filled for f in fills)
    # every outcome pays out at least the total stake — still hedged post-reprice
    assert all(f.stake_filled * f.odds_filled >= total for f in fills)
    # the DRAW fill (index 1) is at the fresh odds with a re-sized stake (not 77)
    assert fills[1].odds_filled == 2.55
    assert fills[1].stake_filled != 77.0


async def test_reprice_aborts_when_arb_evaporates_at_live_odds() -> None:
    """DRAW collapses to 1.50 → overround ≥ 1 (edge gone) → abort cleanly with
    the re-pricing message, nothing placed. The fresh odds are valid (>1.0), so
    this is the re-pricer rejecting a vanished edge, not the unverifiable path."""
    g, n = _guard(), _FakeNotifier()
    opp = detect_arbitrage(list(_arb1_quotes()), 200.0, 1.0)
    assert opp is not None

    async def reverify(leg: Leg) -> float:
        if leg.outcome == "draw":
            return 1.50  # overround ≥ 1 → no arb
        return leg.odds

    placer = _CountingPlacer()
    ex = _executor(g, n, _FakeRecovery(), placer, reverify)
    res = await execute_opportunity(ex, opp, opp_id="t", budget=200.0, min_margin_pct=1.0)
    assert res.outcome is ExecutionOutcome.ABORTED
    assert res.reason == "arb no longer profitable at live odds"
    assert placer.calls == 0


async def test_reprice_unverifiable_leg_aborts_with_clear_message() -> None:
    """A leg whose odds can't be confirmed (reverify → 0.0 sentinel) aborts with
    an 'unverifiable' message — not the old 'drifted X→0.0' misreport — and
    places nothing (fail-closed)."""
    g, n = _guard(), _FakeNotifier()
    opp = detect_arbitrage(list(_arb1_quotes()), 200.0, 1.0)
    assert opp is not None

    async def reverify(leg: Leg) -> float:
        if leg.outcome == "away":
            return 0.0  # unverifiable (refetch failed / market gone)
        return leg.odds

    placer = _CountingPlacer()
    ex = _executor(g, n, _FakeRecovery(), placer, reverify)
    res = await execute_opportunity(ex, opp, opp_id="t", budget=200.0, min_margin_pct=1.0)
    assert res.outcome is ExecutionOutcome.ABORTED
    assert "unverifiable" in (res.reason or "")
    assert placer.calls == 0


async def test_reprice_then_phase2_drift_is_naked_exposure() -> None:
    """Re-pricing succeeds in phase 1, but the market drifts AGAIN during phase-2
    sequential placement (after leg A is live) → NAKED EXPOSURE with one leg live,
    not a clean abort. Re-pricing RESETS the drift baseline, so phase 2 only catches
    FURTHER drift during the place window — the residual risk of capturing fast
    markets (bounded by the existing naked-exposure guard + operator alert)."""
    g, n = _guard(), _FakeNotifier()
    opp = detect_arbitrage(list(_arb1_quotes()), 200.0, 1.0)
    assert opp is not None
    state = {"n": 0}

    async def reverify(leg: Leg) -> float:
        state["n"] += 1
        # calls 1-3 = phase-1 reverify (no drift → arb re-prices + re-sizes);
        # call 4 = phase-2 leg A (away) → ok, placed; call 5 = phase-2 leg B (draw)
        # → drifts 10% past tolerance while leg A is already LIVE.
        if state["n"] == 5:
            return leg.odds * 0.9
        return leg.odds

    placer = _CountingPlacer()
    ex = _executor(g, n, _FakeRecovery(), placer, reverify)
    res = await execute_opportunity(ex, opp, opp_id="t", budget=200.0, min_margin_pct=1.0)
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE
    assert len(res.legs) == 1  # leg A live, hedge incomplete
    assert g.total_exposure_ars == res.legs[0].stake_filled  # the live leg recorded
    assert any("NAKED" in t for t in n.sent)


async def test_reprice_respects_binding_live_cap() -> None:
    """A binding live cap flows through re-pricing: the HOME leg (max_stake 30,
    below its ~46 uncapped allocation) is SCALED by allocate_maxmin, not dropped.
    The re-sized stake respects the cap and placement completes. Regression catcher:
    if the closure built fresh quotes WITHOUT the cap (max_stake=None), HOME would
    re-size to ~46 and check_leg would abort — so COMPLETED here proves the cap is
    carried into the fresh quote."""
    g, n = _guard(), _FakeNotifier()
    opp = detect_arbitrage(list(_arb1_quotes(home_max_stake=30.0)), 200.0, 1.0)
    assert opp is not None
    assert opp.total_stake < 200.0  # the cap scaled the allocation down

    async def reverify(leg: Leg) -> float:
        return leg.odds  # no drift; re-price at the same odds

    placer = _CountingPlacer()
    ex = _executor(g, n, _FakeRecovery(), placer, reverify)
    res = await execute_opportunity(ex, opp, opp_id="t", budget=200.0, min_margin_pct=1.0)
    assert res.outcome is ExecutionOutcome.COMPLETED
    fills = list(res.legs)
    assert fills[2].stake_filled <= 30.0  # HOME (index 2) cap respected post-reprice


async def test_reprice_uses_per_arb_budget_not_original_total_stake() -> None:
    """Re-pricing allocates against the per-arb ``budget`` kwarg, NOT
    ``opp.total_stake``. A leg drifts so a formerly-binding cap no longer binds, and
    the re-sized allocation GROWS beyond the original capped total — proving the
    budget (not the shrunken original total) drove re-sizing."""
    g, n = _guard(), _FakeNotifier()
    # 2-leg arb; the betwarrior leg carries a binding 50 cap at detection odds
    # (2.0), so detection's total is capped well below the 200 budget.
    quotes = [
        OddsQuote(
            platform="betwarrior",
            market_id="m",
            outcome="home",
            decimal_odds=2.0,
            max_stake=50.0,
            timestamp=0.0,
        ),
        OddsQuote(
            platform="betsson",
            market_id="m",
            outcome="away",
            decimal_odds=3.0,
            max_stake=5000.0,
            timestamp=0.0,
        ),
    ]
    opp = detect_arbitrage(quotes, 200.0, 1.0)
    assert opp is not None
    assert opp.total_stake < 150.0  # capped below budget at detection odds

    async def reverify(leg: Leg) -> float:
        # betwarrior drifts UP to 5.0 → its uncapped stake drops, the 50 cap stops
        # binding, so the re-priced allocation can grow toward the 200 budget.
        if leg.platform == "betwarrior":
            return 5.0
        return leg.odds

    placer = _CountingPlacer()
    ex = _executor(g, n, _FakeRecovery(), placer, reverify)
    res = await execute_opportunity(ex, opp, opp_id="t", budget=200.0, min_margin_pct=1.0)
    assert res.outcome is ExecutionOutcome.COMPLETED
    total = sum(f.stake_filled for f in res.legs)
    # re-priced total grew well past the original capped total → budget drove it
    assert total > opp.total_stake + 20.0


async def test_cap_refresh_caps_flow_into_revalidate() -> None:
    """Phase C: the cap_refresh hook's per-leg live caps are collected in phase 1
    and passed to revalidate (3rd arg), so re-pricing can size dynamic legs to the
    real book ceiling. Only collected when a revalidate callback is set."""
    seen: list[list[float | None]] = []

    def revalidate(legs: list[Leg], odds: list[float], caps: list[float | None]) -> list[Leg]:
        seen.append(list(caps))
        return legs  # accept legs unchanged — we only assert cap-passing here

    async def cap_refresh(leg: Leg) -> float | None:
        return 12_345.0 if leg.platform == "betano" else None

    g, n = _guard(), _FakeNotifier()
    ex = _executor(g, n, _FakeRecovery(), _CountingPlacer(), cap_refresh=cap_refresh)
    res = await ex.execute_n_leg("o", list(_three_legs()), revalidate=revalidate)
    assert res.outcome is ExecutionOutcome.COMPLETED
    # betsson None, betano live cap, betwarrior None — order matches _three_legs()
    assert seen == [[None, 12_345.0, None]]


async def test_cap_refresh_not_called_when_no_revalidate() -> None:
    """Phase C: caps are only collected when a revalidate callback is set. Without
    one, cap_refresh is never invoked — so the strict-tolerance path never probes the
    book or builds a slip. Pins the dispatch guard in _run."""
    calls = 0

    async def cap_refresh(leg: Leg) -> float | None:
        nonlocal calls
        calls += 1
        return None

    g, n = _guard(), _FakeNotifier()
    ex = _executor(g, n, _FakeRecovery(), _CountingPlacer(), cap_refresh=cap_refresh)
    res = await ex.execute_n_leg("o", list(_three_legs()))  # NO revalidate= callback
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert calls == 0  # cap_refresh never invoked without a revalidate callback


# ---- BetWarrior auto re-auth + bounded retry (rescue on a placement 401) -----------


class _ReauthStub:
    """Async stand-in for the injected ReauthHandler. Records its call count."""

    def __init__(self, returns: bool = True) -> None:
        self.returns = returns
        self.calls = 0

    async def __call__(self, leg: Leg) -> bool:
        self.calls += 1
        return self.returns


class _AuthFailFirstThenAccept:
    """place() auth-fails (HTTP 401) on the FIRST call only, then accepts — simulates a
    successful re-auth refreshing the bearer so the retry AND any later BW leg land."""

    def __init__(self) -> None:
        self.calls = 0

    async def place(self, leg: Leg) -> PlacementResult:
        i = self.calls
        self.calls += 1
        if i == 0:
            return PlacementResult(
                accepted=False, auth_failed=True, detail="HTTP 401: Unauthorized"
            )
        return PlacementResult(
            accepted=True, stake_filled=leg.stake_ars, odds_filled=leg.odds, ref=f"ref{i}"
        )


class _AuthFailLegThenAccept:
    """Auth-fails the first attempt of a chosen place() call index, then accepts — for
    targeting a specific leg (e.g. leg C of a 3-leg arb)."""

    def __init__(self, fail_index: int) -> None:
        self.fail_index = fail_index
        self.calls = 0
        self._failed = False

    async def place(self, leg: Leg) -> PlacementResult:
        i = self.calls
        self.calls += 1
        if i == self.fail_index and not self._failed:
            self._failed = True
            return PlacementResult(
                accepted=False, auth_failed=True, detail="HTTP 401: Unauthorized"
            )
        return PlacementResult(
            accepted=True, stake_filled=leg.stake_ars, odds_filled=leg.odds, ref=f"ref{i}"
        )


class _AcceptFirstRejectRestAuth:
    """Accepts the first place() (leg A goes live), auth-fails every later call —
    simulates a re-auth that could NOT refresh the session (reauth returns False) or a
    leg that keeps 401-ing after a successful re-auth."""

    def __init__(self) -> None:
        self.calls = 0

    async def place(self, leg: Leg) -> PlacementResult:
        i = self.calls
        self.calls += 1
        if i == 0:
            return PlacementResult(
                accepted=True, stake_filled=leg.stake_ars, odds_filled=leg.odds, ref="refA"
            )
        return PlacementResult(accepted=False, auth_failed=True, detail="HTTP 401")


class _DriftOnBwRetryReverify:
    """reverify that returns steady odds everywhere EXCEPT on the betwarrior-pba leg's
    3rd+ reverify — its post-re-auth re-check (for that leg: upfront pass, pre-place,
    then the retry gate) — where odds drift UNFAVORABLY (-50%, well beyond the 1%
    tolerance, so ``odds_still_acceptable`` refuses) and the leg is NOT retried. Isolates
    the retry-path tolerance check without aborting leg A or the bw leg's own pre-place."""

    def __init__(self) -> None:
        self.bw_calls = 0

    async def __call__(self, leg: Leg) -> float:
        if leg.platform == "betwarrior-pba":
            self.bw_calls += 1
            if self.bw_calls >= 3:
                return leg.odds * 0.5
        return leg.odds


def _arb_betsson_bw() -> tuple[Leg, Leg]:
    # 2-leg arb: leg A betsson (always succeeds), leg B betwarrior-pba.
    return (
        Leg("betsson", "m1", "1X2", "home", 100.0, 2.0),
        Leg("betwarrior-pba", "m1", "1X2", "away", 100.0, 2.1),
    )


def _arb_two_bw() -> tuple[Leg, Leg]:
    # 2-leg arb with BOTH legs betwarrior-pba — proves re-auth is bounded to once per
    # execution (only the first failing leg triggers it).
    return (
        Leg("betwarrior-pba", "m1", "1X2", "home", 100.0, 2.0),
        Leg("betwarrior-pba", "m1", "1X2", "away", 100.0, 2.1),
    )


def _arb_three_bw_last() -> tuple[Leg, Leg, Leg]:
    # 3-leg 1X2 arb; leg C is betwarrior-pba (the 401'ing rescue target).
    return (
        Leg("betsson", "m1", "1X2", "home", 60.0, 3.0),
        Leg("betano", "m1", "1X2", "draw", 60.0, 3.1, live_max_stake_ars=5000.0),
        Leg("betwarrior-pba", "m1", "1X2", "away", 60.0, 3.2),
    )


async def test_reauth_retry_completes_arb() -> None:
    """Leg-C (betwarrior-pba) 401s once with auth_failed; the injected re-auth returns
    True, odds re-verify steady, the single retry is accepted → arb COMPLETED; re-auth
    called exactly once."""
    g, n = _guard(), _FakeNotifier()
    reauth = _ReauthStub(returns=True)
    placer = _AuthFailLegThenAccept(fail_index=2)  # leg C's first attempt 401s
    ex = _executor(g, n, _FakeRecovery(), placer, reauth=reauth)
    res = await ex.execute_n_leg("opp", list(_arb_three_bw_last()))
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert reauth.calls == 1
    assert any("re-authenticating BetWarrior" in t for t in n.sent)
    assert any("COMPLETE" in t for t in n.sent)


class _AuthFailFirstAttemptPerLeg:
    """Auth-fails the FIRST attempt of each distinct leg (keyed by outcome), then accepts
    its retry — so a 2nd auth_failed leg proves the ``reauthed`` guard: only the FIRST
    failing leg gets a re-auth + retry; the second's 401 must NOT re-invoke reauth."""

    def __init__(self) -> None:
        self.calls = 0
        self._seen: set[str] = set()

    async def place(self, leg: Leg) -> PlacementResult:
        i = self.calls
        self.calls += 1
        if leg.outcome not in self._seen:
            self._seen.add(leg.outcome)
            return PlacementResult(
                accepted=False, auth_failed=True, detail="HTTP 401: Unauthorized"
            )
        return PlacementResult(
            accepted=True, stake_filled=leg.stake_ars, odds_filled=leg.odds, ref=f"ref{i}"
        )


async def test_reauth_only_once_per_execution() -> None:
    """Two betwarrior-pba legs: the first 401s and triggers the single re-auth; the
    second succeeds on the now-fresh bearer WITHOUT a second 401 (happy single-failure
    path). re-auth is called exactly once for the whole execution."""
    g, n = _guard(), _FakeNotifier()
    reauth = _ReauthStub(returns=True)
    placer = _AuthFailFirstThenAccept()  # only the very first place() 401s
    ex = _executor(g, n, _FakeRecovery(), placer, reauth=reauth)
    res = await ex.execute_n_leg("opp", list(_arb_two_bw()))
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert reauth.calls == 1  # bounded: one re-auth for the whole execution


async def test_second_auth_failed_leg_does_not_re_reauth() -> None:
    """The ``reauthed`` guard bites on a SECOND auth_failed leg: when BOTH betwarrior-pba
    legs 401, only the first triggers a re-auth (its retry lands, leg A live); the
    second's 401 must NOT re-invoke reauth or retry — it falls through to naked. Proves
    the per-execution bound is enforced, not just the happy single-401 path."""
    g, n = _guard(), _FakeNotifier()
    reauth = _ReauthStub(returns=True)
    placer = _AuthFailFirstAttemptPerLeg()  # BOTH bw legs 401 on their first attempt
    ex = _executor(g, n, _FakeRecovery(), placer, reauth=reauth)
    res = await ex.execute_n_leg("opp", list(_arb_two_bw()))
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE  # leg A live, leg B rejected
    assert reauth.calls == 1  # the second 401 did NOT re-invoke reauth
    assert placer.calls == 3  # leg A (place + retry) + leg B (single failed attempt, NO retry)


async def test_reauth_failure_falls_back_to_naked() -> None:
    """≥1 leg already live, then a later betwarrior-pba leg 401s; the re-auth returns
    False (challenge / failed). No retry → the leg rejects → NAKED_EXPOSURE (today's
    behavior), exactly one re-auth attempt, a NAKED alert. The rescue never adds exposure."""
    g, n = _guard(), _FakeNotifier()
    reauth = _ReauthStub(returns=False)
    placer = _AcceptFirstRejectRestAuth()  # leg A live, leg B keeps 401-ing
    ex = _executor(g, n, _FakeRecovery(), placer, reauth=reauth)
    res = await ex.execute_n_leg("opp", list(_arb_betsson_bw()))
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE
    assert reauth.calls == 1
    assert any("NAKED" in t for t in n.sent)


async def test_reauth_then_drift_aborts_or_naked() -> None:
    """Re-auth succeeds, BUT the post-reauth re-verify drifts beyond tolerance → the
    failed leg is NOT retried → it goes naked (leg A is live) exactly as today
    (conservative: the rescue never hedges at unfavorable odds). No retry placement."""
    g, n = _guard(), _FakeNotifier()
    reauth = _ReauthStub(returns=True)
    placer = _AcceptFirstRejectRestAuth()  # leg A live, leg B 401s
    # reverify steady everywhere EXCEPT the bw leg's post-reauth re-check (its 3rd
    # reverify: upfront pass, pre-place, then the retry gate), which drifts
    # UNFAVORABLY (-50%) → tolerance fails → no retry → naked (leg A stays live).
    reverify = _DriftOnBwRetryReverify()
    ex = _executor(g, n, _FakeRecovery(), placer, reauth=reauth, reverify=reverify)
    res = await ex.execute_n_leg("opp", list(_arb_betsson_bw()))
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE
    assert reauth.calls == 1
    assert placer.calls == 2  # leg A placed + leg B's single (failed) attempt — NO retry


async def test_non_auth_reject_does_not_reauth() -> None:
    """A non-auth reject (auth_failed=False — e.g. odds invalid / suspended outcome) must
    NOT trigger a re-auth: today's abort/naked runs unchanged."""
    g, n = _guard(), _FakeNotifier()
    reauth = _ReauthStub(returns=True)
    placer = _CountingPlacer(reject_index=1)  # leg B rejected, auth_failed defaults False
    ex = _executor(g, n, _FakeRecovery(), placer, reauth=reauth)
    res = await ex.execute_n_leg("opp", list(_arb_betsson_bw()))
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE
    assert reauth.calls == 0  # never re-authed on a non-auth reject
