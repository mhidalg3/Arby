"""Integration test for `PostgresAuditRecorder` against the running Postgres.

Exercises the full persistence path end-to-end: an APPROVED arb + its execution
outcome land in `opportunities`/`placements` with the right shape and status,
readable back via the ORM. Cleans up after itself (ON DELETE CASCADE removes
the placements) so the shared DB stays empty.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.arbitrage.quotes import OddsQuote
from src.execution.executor import ExecutionOutcome, ExecutionResult, PlacementResult
from src.risk.decision import RiskDecision, Verdict
from src.storage.audit_recorder import PostgresAuditRecorder
from src.storage.db import get_session
from src.storage.models import Opportunity, OpportunityStatus, Placement

pytestmark = pytest.mark.integration


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


async def test_record_opportunity_and_execution_round_trip() -> None:
    rec = PostgresAuditRecorder()
    market = f"TEST|{uuid.uuid4()}"
    opp = _arb()
    decision = _decision(market)
    oid: int | None = None
    try:
        oid = await rec.record_opportunity(market, opp, decision)
        assert oid is not None

        await rec.record_execution(
            oid,
            opp,
            ExecutionResult(
                ExecutionOutcome.COMPLETED,
                legs=(
                    PlacementResult(accepted=True, stake_filled=100.0, odds_filled=2.05, ref="X1"),
                    PlacementResult(accepted=True, stake_filled=95.0, odds_filled=2.10, ref="Y1"),
                ),
            ),
        )

        async with get_session() as s:
            row = await s.get(Opportunity, oid)
            assert row is not None
            assert row.market_id == market
            assert row.status is OpportunityStatus.COMPLETED
            assert len(row.legs) == 2
            assert row.legs[0]["platform"] == "betsson"
            assert row.legs[0]["target_stake"] == 100.0
            assert row.risk_confidence == 1.0
            assert row.execution_reason is None  # COMPLETED carries no abort reason

            placed = (
                (await s.execute(select(Placement).where(Placement.opportunity_id == oid)))
                .scalars()
                .all()
            )
            assert len(placed) == 2
            assert {p.leg for p in placed} == {"a", "b"}
            by_leg = {p.leg: p for p in placed}
            assert by_leg["a"].target_stake == 100.0
            assert by_leg["a"].actual_stake == 100.0
            assert by_leg["a"].actual_filled_odds == 2.05
            assert by_leg["a"].platform_bet_id == "X1"
            assert by_leg["a"].success is True
            assert by_leg["b"].target_stake == 95.0
            assert by_leg["b"].actual_stake == 95.0
    finally:
        if oid is not None:
            async with get_session() as s:
                row = await s.get(Opportunity, oid)
                if row is not None:
                    await s.delete(row)  # ON DELETE CASCADE removes placements


async def test_record_execution_aborted_writes_no_placements() -> None:
    """An ABORTED execution (no legs placed) updates status + reason but writes
    no placement rows."""
    rec = PostgresAuditRecorder()
    market = f"TEST|{uuid.uuid4()}"
    opp = _arb()
    decision = _decision(market)
    oid: int | None = None
    try:
        oid = await rec.record_opportunity(market, opp, decision)
        assert oid is not None

        await rec.record_execution(
            oid, opp, ExecutionResult(ExecutionOutcome.ABORTED, reason="kill switch tripped")
        )

        async with get_session() as s:
            row = await s.get(Opportunity, oid)
            assert row is not None
            assert row.status is OpportunityStatus.ABORTED_PRE_EXECUTION
            assert row.execution_reason == "kill switch tripped"
            placed = (
                (await s.execute(select(Placement).where(Placement.opportunity_id == oid)))
                .scalars()
                .all()
            )
            assert placed == []
    finally:
        if oid is not None:
            async with get_session() as s:
                row = await s.get(Opportunity, oid)
                if row is not None:
                    await s.delete(row)
