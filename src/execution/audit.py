"""Audit-persistence seam for the orchestrator.

Mirrors the orchestrator's existing `notifier: Notifier | None` → `NullNotifier()`
pattern: the orchestrator depends on this Protocol (no storage import), and the
concrete Postgres adapter is injected only at the composition root (the armed
`run_hot_loop`). So importing the orchestrator never creates the DB engine.

An `AuditRecorder` durably records each APPROVED arb (as `record_opportunity`,
returning its new row id) and its subsequent execution outcome
(`record_execution`, writing the per-leg fills). Every implementation MUST be
fail-soft: a persistence error MUST NEVER raise into the loop or block a
placement — see `PostgresAuditRecorder` for the canonical wrapping.
"""

from __future__ import annotations

from typing import Protocol

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.execution.executor import ExecutionResult
from src.risk.decision import RiskDecision


class AuditRecorder(Protocol):
    async def record_opportunity(
        self, market_id: str, opp: ArbitrageOpportunity, decision: RiskDecision
    ) -> int | None:
        """Persist an APPROVED arb. Returns its durable id, or None if persistence
        failed / unavailable (the caller proceeds regardless)."""
        ...

    async def record_execution(
        self, opportunity_id: int, opp: ArbitrageOpportunity, result: ExecutionResult
    ) -> None:
        """Record the outcome of executing `opp` (per-leg fills + final status).
        No-op when `opportunity_id` is None."""
        ...


class NullRecorder:
    """Default no-op recorder (dry-run / tests / DB-less)."""

    async def record_opportunity(
        self, market_id: str, opp: ArbitrageOpportunity, decision: RiskDecision
    ) -> int | None:
        return None

    async def record_execution(
        self, opportunity_id: int, opp: ArbitrageOpportunity, result: ExecutionResult
    ) -> None:
        return None
