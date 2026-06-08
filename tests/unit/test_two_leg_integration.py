"""Full-chain two-leg integration: real Executor → real LegPlacers → fake transports.

The unit tests cover the Executor (with fake placers) and the placers (with fake
transports) SEPARATELY. This wires the real components together — exactly the seam
the live two-leg run exercises — and pins the outcomes deterministically across the
happy path and both failure modes, so robustness doesn't depend on a live fluke.
"""

from __future__ import annotations

from typing import Any

from src.execution.executor import ExecutionOutcome, Executor, Leg
from src.execution.guardrails import Guardrails
from src.execution.leg_placer import BetanoLegPlacer, BetssonLegPlacer
from src.execution.recovery import RecoveryOutcome

_CTX = {"x-sb-user-context-id": "ctx-1", "sessiontoken": "JWT", "brandid": "B"}
_BETSSON_OK = {
    "couponStatus": {
        "couponStatusPollingResult": "Success",
        "couponId": "C-OK",
        "couponPlacementErrors": [],
    }
}
_BETSSON_REJECT = {
    "couponStatus": {"couponStatusPollingResult": "Failure", "couponPlacementErrors": ["nope"]}
}


def _betano_slip() -> dict[str, Any]:
    return {
        "data": {
            "hash": "H",
            "slipData": "H",
            "betslipTrackId": "T",
            "legs": [{"id": "S"}],
            "bets": [{"id": "1:SGL:S", "odds": 1.3, "amount": 0}],
        }
    }


def _betano_place(accepted: bool) -> dict[str, Any]:
    if not accepted:
        return {"data": {"accepted": False, "errors": [{"code": "X"}]}}
    return {
        "data": {
            "accepted": True,
            "receipts": [{"betId": "B-OK", "totalAmount": 50, "totalOdds": 1.3}],
        }
    }


class FakeBetssonTransport:
    def __init__(self, body: dict[str, Any], ctx: dict[str, str] | None = _CTX) -> None:
        self._body = body
        self._ctx = ctx

    async def prepare_betsson_context(self) -> dict[str, str] | None:
        return self._ctx

    async def fetch(self, method: str, url: str, **_: Any) -> tuple[int, dict[str, Any]]:
        return 200, self._body


class SeqBetanoTransport:
    """Returns queued responses per call (plain-leg, updatebets, place)."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = responses
        self.calls = 0

    async def fetch(self, method: str, url: str, **_: Any) -> tuple[int, dict[str, Any]]:
        r = self._responses[self.calls]
        self.calls += 1
        return 200, r


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
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


def _legs() -> tuple[Leg, Leg]:
    leg_betsson = Leg(
        platform="betsson-pba",
        match_id="m-bs",
        market="1X2",
        outcome="home",
        stake_ars=50.0,
        odds=2.0,
        platform_outcome_id="s-x",
        platform_event_ref="slug",
    )
    leg_betano = Leg(
        platform="betano-pba",
        match_id="m-bn",
        market="1X2",
        outcome="home",
        stake_ars=50.0,
        odds=1.3,
        platform_outcome_id="S",
        live_max_stake_ars=1000.0,
    )
    return leg_betsson, leg_betano


def _executor(betsson_t: object, betano_t: object, g: Guardrails, n: _Notifier) -> Executor:
    return Executor(
        guardrails=g,
        notifier=n,
        recovery=_Recovery(),
        placers={
            "betsson-pba": BetssonLegPlacer(betsson_t),  # type: ignore[arg-type]
            "betano-pba": BetanoLegPlacer(betano_t),  # type: ignore[arg-type]
        },
        dry_run=True,
    )


async def test_full_chain_both_legs_complete_and_record_exposure() -> None:
    g, n = _guard(), _Notifier()
    betsson_t = FakeBetssonTransport(_BETSSON_OK)
    betano_t = SeqBetanoTransport([_betano_slip(), _betano_slip(), _betano_place(True)])
    res = await _executor(betsson_t, betano_t, g, n).execute_two_leg("opp", *_legs())
    assert res.outcome is ExecutionOutcome.COMPLETED
    assert res.leg_a is not None and res.leg_a.accepted and res.leg_a.ref == "C-OK"
    assert res.leg_b is not None and res.leg_b.accepted and res.leg_b.ref == "B-OK"
    # exposure = both legs; Betsson echoes no stake so its placer falls back to requested 50.
    assert g.total_exposure_ars == 100.0
    assert betano_t.calls == 3  # plain-leg → updatebets → place


async def test_full_chain_betano_rejection_is_naked_exposure() -> None:
    # Betsson (Leg A) fills, Betano (Leg B) place rejected → naked exposure, Leg A only.
    g, n = _guard(), _Notifier()
    betsson_t = FakeBetssonTransport(_BETSSON_OK)
    betano_t = SeqBetanoTransport([_betano_slip(), _betano_slip(), _betano_place(False)])
    res = await _executor(betsson_t, betano_t, g, n).execute_two_leg("opp", *_legs())
    assert res.outcome is ExecutionOutcome.NAKED_EXPOSURE
    assert g.total_exposure_ars == 50.0  # only Leg A is live
    assert any("NAKED EXPOSURE" in m for m in n.sent)  # operator IS alerted


async def test_full_chain_betsson_rejection_aborts_before_betano() -> None:
    # Leg A rejected → abort before touching Betano (no naked leg).
    g, n = _guard(), _Notifier()
    betsson_t = FakeBetssonTransport(_BETSSON_REJECT)
    betano_t = SeqBetanoTransport([_betano_slip(), _betano_slip(), _betano_place(True)])
    res = await _executor(betsson_t, betano_t, g, n).execute_two_leg("opp", *_legs())
    assert res.outcome is ExecutionOutcome.ABORTED
    assert g.total_exposure_ars == 0.0
    assert betano_t.calls == 0  # Betano never touched
