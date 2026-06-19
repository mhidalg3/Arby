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
from datetime import UTC, datetime

import structlog

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.execution.audit import AuditRecorder
from src.execution.executor import ExecutionOutcome, ExecutionResult
from src.risk.decision import RiskDecision
from src.storage.db import get_session
from src.storage.models import Opportunity, OpportunityStatus, Placement

log = structlog.get_logger(__name__)

# Audit writes must never stall the armed hot loop beyond this. A healthy
# localhost write returns in milliseconds; the bound only trips when Postgres is
# unhealthy (lock wait, disk full, hung connection), where best-effort audit is
# the correct degradation — never delay a time-sensitive arb placement on audit.
_AUDIT_TIMEOUT_SEC = 3.0

# ExecutionOutcome → OpportunityStatus. Total over the four members; the `.get`
# default below is unreachable defensive cover (a future outcome must be mapped
# here, else it lands as EXPIRED).
_OUTCOME_TO_STATUS: dict[ExecutionOutcome, OpportunityStatus] = {
    ExecutionOutcome.COMPLETED: OpportunityStatus.COMPLETED,
    ExecutionOutcome.ABORTED: OpportunityStatus.ABORTED_PRE_EXECUTION,
    ExecutionOutcome.NAKED_EXPOSURE: OpportunityStatus.ABORTED_POST_LEG_A,
    ExecutionOutcome.FROZEN: OpportunityStatus.FROZEN,
}


class PostgresAuditRecorder(AuditRecorder):
    async def record_opportunity(
        self, market_id: str, opp: ArbitrageOpportunity, decision: RiskDecision
    ) -> int | None:
        try:
            return await asyncio.wait_for(
                self._write_opportunity(market_id, opp, decision),
                timeout=_AUDIT_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001 — audit must never break the armed loop
            log.warning("audit.opportunity_failed", market_id=market_id, error=str(exc))
            return None

    async def _write_opportunity(
        self, market_id: str, opp: ArbitrageOpportunity, decision: RiskDecision
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
                    }
                    for q, stake in zip(opp.legs, opp.stakes, strict=True)
                ],
                expected_margin_pct=opp.realized_roi_pct,
                expected_profit=opp.guaranteed_profit,
                risk_confidence=decision.confidence,
                high_margin_warning=decision.high_margin_warning,
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
                        raw_response=None,
                    )
                )
