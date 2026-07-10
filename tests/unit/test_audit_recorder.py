"""Unit tests for `PostgresAuditRecorder` outage/recovery alerting.

Exercises the alert state machine without a real DB by monkeypatching the
module-level `get_session` (the async engine in `src/storage/db.py` is created at
import but never connected). Verifies: alert-once-per-outage, recovery-once,
fail-soft on write paths, and the periodic watchdog probe.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

import pytest

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.arbitrage.quotes import OddsQuote
from src.execution.executor import ExecutionOutcome, ExecutionResult, PlacementResult
from src.risk.decision import RiskDecision, Verdict
from src.storage import audit_recorder
from src.storage.audit_recorder import PostgresAuditRecorder, audit_watchdog


class _Notifier:
    """Recording notifier — captures every message sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


def _quote(platform: str, outcome: str, odds: float) -> OddsQuote:
    return OddsQuote(
        platform=platform,
        market_id="TEST|FIX1|1X2",
        outcome=outcome,
        decimal_odds=odds,
        max_stake=5000.0,
        timestamp=0.0,
        platform_outcome_id=f"{platform}-sel",
        platform_event_id=f"{platform}-evt",
    )


def _arb() -> ArbitrageOpportunity:
    legs = (_quote("betsson", "home", 2.1), _quote("betano", "away", 2.1))
    stakes = (100.0, 95.0)
    return ArbitrageOpportunity(
        legs=legs,
        stakes=stakes,
        total_stake=195.0,
        guaranteed_profit=5.0,
        margin_pct=4.76,
        realized_roi_pct=2.5,
        capital_utilization=195.0,
    )


def _decision(market_id: str) -> RiskDecision:
    return RiskDecision(
        verdict=Verdict.APPROVED,
        reason="approved",
        rules_evaluated=("confidence",),
        confidence=1.0,
        high_margin_warning=False,
        fixture_id="TEST|FIX1",
        market_id=market_id,
        realized_roi_pct=2.5,
        platforms=("betsson", "betano"),
        evaluated_at=0.0,
    )


def _failing_session() -> object:
    @asynccontextmanager
    async def _cm() -> None:
        raise ConnectionError("connection refused")
        yield  # pragma: no cover

    return _cm


def _ok_session(calls: list[object] | None = None) -> object:
    class _S:
        async def execute(self, *a: object, **k: object) -> None:
            if calls is not None:
                calls.append(a)

    @asynccontextmanager
    async def _cm() -> None:
        yield _S()

    return _cm


async def test_healthcheck_down_alerts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    note = _Notifier()
    rec = PostgresAuditRecorder(notifier=note)
    monkeypatch.setattr(audit_recorder, "get_session", _failing_session())

    assert await rec.healthcheck() is False
    assert await rec.healthcheck() is False
    await asyncio.sleep(0)  # drain fire-and-forget notification task

    downs = [m for m in note.sent if "AUDIT STORE DOWN" in m]
    assert len(downs) == 1


async def test_recovery_alerts_once_after_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    note = _Notifier()
    rec = PostgresAuditRecorder(notifier=note)

    monkeypatch.setattr(audit_recorder, "get_session", _failing_session())
    assert await rec.healthcheck() is False

    monkeypatch.setattr(audit_recorder, "get_session", _ok_session())
    assert await rec.healthcheck() is True
    await asyncio.sleep(0)  # drain fire-and-forget notification tasks
    recovers = [m for m in note.sent if "recovered" in m]
    assert len(recovers) == 1

    # A further healthy probe adds no message.
    assert await rec.healthcheck() is True
    assert len(note.sent) == 2


async def test_record_opportunity_failure_drives_down_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _Notifier()
    rec = PostgresAuditRecorder(notifier=note)
    monkeypatch.setattr(audit_recorder, "get_session", _failing_session())

    # Fail-soft preserved: returns None, does not raise.
    result = await rec.record_opportunity("M1", _arb(), _decision("M1"))
    assert result is None
    await asyncio.sleep(0)  # drain fire-and-forget notification task
    downs = [m for m in note.sent if "AUDIT STORE DOWN" in m]
    assert len(downs) == 1

    # Second failure does not re-alert.
    await rec.record_opportunity("M1", _arb(), _decision("M1"))
    assert len(note.sent) == 1


async def test_healthy_recorder_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    note = _Notifier()
    rec = PostgresAuditRecorder(notifier=note)
    monkeypatch.setattr(audit_recorder, "get_session", _ok_session())

    assert await rec.healthcheck() is True
    assert note.sent == []


async def test_audit_watchdog_probes_repeatedly(monkeypatch: pytest.MonkeyPatch) -> None:
    note = _Notifier()
    rec = PostgresAuditRecorder(notifier=note)
    calls: list[object] = []
    monkeypatch.setattr(audit_recorder, "get_session", _ok_session(calls))

    task = asyncio.create_task(audit_watchdog(rec, interval_sec=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # Probe-first + at least one repeat within the sleep window.
    assert len(calls) >= 2
    assert note.sent == []


async def test_record_execution_persists_placement_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """An accepted fill's raw_response lands in the Placement row (receipt capture) —
    the column that was previously hardcoded to None."""
    note = _Notifier()
    rec = PostgresAuditRecorder(notifier=note)
    added: list[object] = []

    class _OppRow:
        status = None
        status_updated_at = None
        execution_reason = None

    class _S:
        async def get(self, cls: object, oid: object) -> object:
            return _OppRow()  # non-None → the execution writes its placement rows

        def add(self, obj: object) -> None:
            added.append(obj)

    @asynccontextmanager
    async def _cm() -> None:
        yield _S()

    monkeypatch.setattr(audit_recorder, "get_session", _cm)
    result = ExecutionResult(
        outcome=ExecutionOutcome.COMPLETED,
        legs=(
            PlacementResult(
                accepted=True,
                stake_filled=100.0,
                odds_filled=2.1,
                ref="C1",
                raw_response={"couponStatus": {"couponId": "C1"}},
            ),
        ),
    )
    await rec.record_execution(1, _arb(), result)
    assert len(added) == 1
    placement = added[0]
    assert placement.raw_response == {"couponStatus": {"couponId": "C1"}}  # type: ignore[attr-defined]


async def test_record_opportunity_persists_adaptive_threshold_and_variance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adaptive_threshold_pct + garch_variance land on the Opportunity row (the two
    previously-always-NULL columns); omitted kwargs → None (backward-compatible)."""
    rec = PostgresAuditRecorder()
    added: list[object] = []

    class _S:
        def add(self, obj: object) -> None:
            added.append(obj)

        async def flush(self) -> None:
            for r in added:
                r.id = 1  # simulate PK assignment on flush

    @asynccontextmanager
    async def _cm() -> None:
        yield _S()

    monkeypatch.setattr(audit_recorder, "get_session", _cm)
    oid = await rec.record_opportunity(
        "FIX1|1x2",
        _arb(),
        _decision("FIX1|1x2"),
        adaptive_threshold_pct=1.5,
        garch_variance=99.0,
    )
    assert oid == 1
    assert len(added) == 1
    row = added[0]
    assert row.adaptive_threshold_pct == 1.5  # type: ignore[attr-defined]
    assert row.garch_variance == 99.0  # type: ignore[attr-defined]

    # Omitted kwargs → None (the shape before adaptive thresholds).
    added.clear()
    oid2 = await rec.record_opportunity("FIX1|1x2", _arb(), _decision("FIX1|1x2"))
    assert oid2 == 1
    row2 = added[0]
    assert row2.adaptive_threshold_pct is None  # type: ignore[attr-defined]
    assert row2.garch_variance is None  # type: ignore[attr-defined]
