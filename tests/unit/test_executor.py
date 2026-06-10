"""Tests for the N-leg execution state machine (dry-run)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

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
) -> Executor:
    kw: dict[str, object] = {
        "guardrails": g,
        "notifier": n,
        "recovery": rec,
        "placer": placer,
    }
    if reverify is not None:
        kw["reverify"] = reverify
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
