"""Concrete `AuditRecorder` backed by Postgres.

Lives in `storage/` (the persistence adapter) and is imported ONLY by the
composition root (`scripts/run_hot_loop`), so the async engine is created only
in the armed path. Translates the orchestrator's domain objects
(`ArbitrageOpportunity`, `RiskDecision`, `ExecutionResult`) into `opportunities`
+ `placements` rows.

Every DB call is wrapped fail-soft AND time-bounded: a Postgres error, a stall,
or an unresponsive connection MUST NEVER raise into the hot loop or block a
placement. Writes are capped at `_AUDIT_TIMEOUT_SEC` wall-clock — if Postgres
can't confirm the audit within that window (a localhost write is sub-50ms; the
bound only bites when the DB is genuinely unhealthy), the write is abandoned
(TimeoutError, logged) and the bet still places. `record_opportunity` returns
None on failure (and the orchestrator then skips `record_execution`);
`record_execution` swallows its own errors. Audit silently degrades (logged).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.execution.audit import AuditRecorder
from src.execution.executor import ExecutionOutcome, ExecutionResult
from src.execution.notify import Notifier, NullNotifier
from src.risk.decision import RiskDecision
from src.storage.db import get_session
from src.storage.models import Opportunity, OpportunityStatus, Placement

log = structlog.get_logger(__name__)

# Audit writes must never stall the armed hot loop beyond this. A healthy
# localhost write returns in milliseconds; the bound only trips when Postgres is
# unhealthy (lock wait, disk full, hung connection), where best-effort audit is
# the correct degradation — never delay a time-sensitive arb placement on audit.
_AUDIT_TIMEOUT_SEC = 3.0

# Audit-store outage/recovery alerts. Sent at most once per outage per process
# (matching every existing alert pattern in the codebase) via the injected
# Notifier — placement is never blocked, the hot loop never halts.
_DOWN_ALERT = (
    "🚨 AUDIT STORE DOWN — Postgres unreachable: opportunities/placements are "
    "NOT being recorded. Placement continues (fail-soft). "
    "Restore: docker compose up -d postgres"
)
_RECOVERED_ALERT = "✅ Audit store recovered — audit writes resuming."

# ExecutionOutcome → OpportunityStatus. Total over the five members; the `.get`
# default below is unreachable defensive cover (a future outcome must be mapped
# here, else it lands as EXPIRED).
_OUTCOME_TO_STATUS: dict[ExecutionOutcome, OpportunityStatus] = {
    ExecutionOutcome.COMPLETED: OpportunityStatus.COMPLETED,
    ExecutionOutcome.ABORTED: OpportunityStatus.ABORTED_PRE_EXECUTION,
    ExecutionOutcome.NAKED_EXPOSURE: OpportunityStatus.ABORTED_POST_LEG_A,
    ExecutionOutcome.PENDING_UNKNOWN: OpportunityStatus.PENDING_UNKNOWN,
    ExecutionOutcome.FROZEN: OpportunityStatus.FROZEN,
}


class PostgresAuditRecorder(AuditRecorder):
    def __init__(self, notifier: Notifier | None = None) -> None:
        self._notifier = notifier or NullNotifier()
        self._down = False  # in-outage flag: alert once per outage, recover once
        # Strong refs for fire-and-forget notification tasks — prevents GC before
        # the send completes. The send itself never blocks the placement path.
        self._bg: set[asyncio.Task[Any]] = set()

    def _fire(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Schedule a notification off the placement path — a slow Telegram send
        (10s HTTP timeout) must never delay a bet. Notifier.send never raises
        (protocol contract), so the task cannot propagate an exception."""
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _note_failure(self, error: str) -> None:
        if self._down:
            return
        # Toggle synchronously: the write path and the watchdog share one event
        # loop, so flipping the flag here (before scheduling the send) closes the
        # interleave window — no double alert if both observe the same outage.
        self._down = True
        log.error("audit.store_down", error=error)
        self._fire(self._notifier.send(_DOWN_ALERT))

    def _note_success(self) -> None:
        if not self._down:
            return
        self._down = False
        log.info("audit.store_recovered")
        self._fire(self._notifier.send(_RECOVERED_ALERT))

    async def record_opportunity(
        self,
        market_id: str,
        opp: ArbitrageOpportunity,
        decision: RiskDecision,
        *,
        adaptive_threshold_pct: float | None = None,
        garch_variance: float | None = None,
    ) -> int | None:
        try:
            opp_id = await asyncio.wait_for(
                self._write_opportunity(
                    market_id, opp, decision, adaptive_threshold_pct, garch_variance
                ),
                timeout=_AUDIT_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001 — audit must never break the armed loop
            log.warning("audit.opportunity_failed", market_id=market_id, error=str(exc))
            self._note_failure(str(exc))
            return None
        self._note_success()
        return opp_id

    async def _write_opportunity(
        self,
        market_id: str,
        opp: ArbitrageOpportunity,
        decision: RiskDecision,
        adaptive_threshold_pct: float | None,
        garch_variance: float | None,
    ) -> int:
        now = datetime.now(UTC)
        async with get_session() as s:
            row = Opportunity(
                detected_at=now,
                market_id=market_id,
                partition_pair_id=None,
                legs=[
                    {
                        "platform": q.platform,
                        "outcome": q.outcome,
                        "decimal_odds": q.decimal_odds,
                        "target_stake": stake,
                        "platform_outcome_id": q.platform_outcome_id,
                        "platform_event_id": q.platform_event_id,
                    }
                    for q, stake in zip(opp.legs, opp.stakes, strict=True)
                ],
                expected_margin_pct=opp.realized_roi_pct,
                expected_profit=opp.guaranteed_profit,
                risk_confidence=decision.confidence,
                high_margin_warning=decision.high_margin_warning,
                # σ²_t is in (implied-prob × scale)² units, matching the artifact scale.
                adaptive_threshold_pct=adaptive_threshold_pct,
                garch_variance=garch_variance,
                status=OpportunityStatus.APPROVED,
                status_updated_at=now,
            )
            s.add(row)
            # Flush so the PK is assigned before commit; expire_on_commit=False
            # keeps row.id readable through the return.
            await s.flush()
            return row.id

    async def record_execution(
        self, opportunity_id: int, opp: ArbitrageOpportunity, result: ExecutionResult
    ) -> None:
        try:
            await asyncio.wait_for(
                self._write_execution(opportunity_id, opp, result),
                timeout=_AUDIT_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("audit.execution_failed", opportunity_id=opportunity_id, error=str(exc))
            self._note_failure(str(exc))
            return
        self._note_success()

    async def _write_execution(
        self, opportunity_id: int, opp: ArbitrageOpportunity, result: ExecutionResult
    ) -> None:
        now = datetime.now(UTC)
        async with get_session() as s:
            row = await s.get(Opportunity, opportunity_id)
            if row is None:
                log.warning("audit.execution_no_opportunity", opportunity_id=opportunity_id)
                return
            row.status = _OUTCOME_TO_STATUS.get(result.outcome, OpportunityStatus.EXPIRED)
            row.status_updated_at = now
            row.execution_reason = result.reason or None
            # result.legs is the accepted placed prefix, aligned to opp.legs in
            # placement order — so positional pairing is valid. Aborted/frozen
            # executions have empty result.legs (no placements rows); naked
            # exposure writes only the placed prefix; the full intent is
            # preserved in opportunities.legs.
            for i, fill in enumerate(result.legs):
                target = opp.legs[i]
                s.add(
                    Placement(
                        opportunity_id=opportunity_id,
                        leg=chr(ord("a") + i),
                        platform=target.platform,
                        submitted_at=now,
                        confirmed_at=now if fill.accepted else None,
                        target_odds=target.decimal_odds,
                        actual_filled_odds=fill.odds_filled or None,
                        target_stake=opp.stakes[i],
                        actual_stake=fill.stake_filled or None,
                        platform_bet_id=fill.ref or None,
                        success=fill.accepted,
                        error_message=fill.detail if not fill.accepted else None,
                        raw_response=fill.raw_response,
                    )
                )

    async def healthcheck(self) -> bool:
        """Bounded `SELECT 1` against the audit store. True when reachable.
        Never raises; drives the same down/recovered alert state as the write
        paths."""
        try:
            await asyncio.wait_for(self._probe(), timeout=_AUDIT_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 — liveness probe must never raise
            log.warning("audit.healthcheck_failed", error=str(exc))
            self._note_failure(str(exc))
            return False
        self._note_success()
        return True

    async def _probe(self) -> None:
        async with get_session() as s:
            await s.execute(text("SELECT 1"))


async def audit_watchdog(recorder: PostgresAuditRecorder, *, interval_sec: float = 600.0) -> None:
    """Probe-first liveness loop: an audit-store outage alerts within one
    interval even when no arbs (hence no audit writes) occur — closes the
    silent-week gap. The immediate first probe doubles as the arm-time health
    check."""
    while True:
        await recorder.healthcheck()
        await asyncio.sleep(interval_sec)
