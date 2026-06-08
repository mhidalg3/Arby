"""Tests for the arbitrage orchestration loop (detect → risk → execute)."""

from __future__ import annotations

from src.arbitrage.quotes import OddsQuote
from src.execution.executor import ExecutionOutcome, Executor, Leg, PlacementResult
from src.execution.guardrails import Guardrails
from src.execution.orchestrator import ArbOrchestrator
from src.risk.evaluator import RiskEvaluator
from src.risk.policy import RiskPolicy


def _quote(platform: str, outcome: str, odds: float) -> OddsQuote:
    return OddsQuote(
        platform=platform,
        market_id="FIX1|1X2",
        outcome=outcome,
        decimal_odds=odds,
        max_stake=5000.0,
        timestamp=0.0,
        platform_outcome_id=f"{platform}-sel",
        platform_event_id=f"{platform}-evt",
    )


class _FakeQuoteSource:
    def __init__(self, by_market: dict[str, list[OddsQuote]]) -> None:
        self.by_market = by_market
        self.fetches = 0

    async def fetch(self) -> dict[str, list[OddsQuote]]:
        self.fetches += 1
        return self.by_market


class _Placer:
    def __init__(self) -> None:
        self.calls = 0

    async def place(self, leg: Leg) -> PlacementResult:
        self.calls += 1
        return PlacementResult(accepted=True, stake_filled=leg.stake_ars, odds_filled=leg.odds)


class _Notifier:
    async def send(self, text: str) -> bool:
        return True


class _Recovery:
    async def recover(self, reason: str):  # type: ignore[no-untyped-def]
        from src.execution.recovery import RecoveryOutcome

        return RecoveryOutcome.UNRESOLVED


def _guard() -> Guardrails:
    return Guardrails(
        max_position_per_match_ars=100_000.0,
        max_total_exposure_ars=1_000_000.0,
        max_daily_loss_ars=50_000.0,
        odds_tolerance_pct=100.0,
    )


def _risk() -> RiskEvaluator:
    # Permissive policy: low margin floor, platforms fully reliable, no confidence floor.
    return RiskEvaluator(
        policy=RiskPolicy(
            min_margin_pct=0.5,
            min_confidence=0.0,
            platform_reliability={"betsson": 1.0, "betano": 1.0},
        )
    )


def _orch(
    source: _FakeQuoteSource, guard: Guardrails, placers: dict[str, _Placer]
) -> ArbOrchestrator:
    ex = Executor(
        guardrails=guard,
        notifier=_Notifier(),
        recovery=_Recovery(),
        placers=placers,  # type: ignore[arg-type]
    )
    return ArbOrchestrator(
        quote_source=source,
        risk_evaluator=_risk(),
        executor=ex,
        guardrails=guard,
        budget_ars=1000.0,
        min_margin_pct=1.0,
    )


# A genuine two-platform Dutch book: 1/2.1 + 1/2.1 = 0.952 < 1 → ~5% margin.
def _arb_market() -> dict[str, list[OddsQuote]]:
    return {"FIX1|1X2": [_quote("betsson", "home", 2.1), _quote("betano", "away", 2.1)]}


async def test_executes_an_approved_arb() -> None:
    g = _guard()
    bp, ap = _Placer(), _Placer()
    orch = _orch(_FakeQuoteSource(_arb_market()), g, {"betsson": bp, "betano": ap})
    results = await orch.run_once()
    assert len(results) == 1 and results[0].outcome is ExecutionOutcome.COMPLETED
    assert bp.calls == 1 and ap.calls == 1


async def test_no_arb_market_does_nothing() -> None:
    g = _guard()
    bp, ap = _Placer(), _Placer()
    # 1/1.5 + 1/1.5 = 1.33 > 1 → no arb.
    flat = {"FIX1|1X2": [_quote("betsson", "home", 1.5), _quote("betano", "away", 1.5)]}
    orch = _orch(_FakeQuoteSource(flat), g, {"betsson": bp, "betano": ap})
    results = await orch.run_once()
    assert results == [] and bp.calls == 0 and ap.calls == 0


async def test_market_executed_once_across_polls() -> None:
    g = _guard()
    bp, ap = _Placer(), _Placer()
    source = _FakeQuoteSource(_arb_market())
    orch = _orch(source, g, {"betsson": bp, "betano": ap})
    await orch.run_once()
    await orch.run_once()  # same persistent arb next poll
    assert bp.calls == 1 and ap.calls == 1  # NOT re-placed


async def test_kill_switch_stops_execution() -> None:
    g = _guard()
    g.trip_kill_switch("manual halt")
    bp, ap = _Placer(), _Placer()
    orch = _orch(_FakeQuoteSource(_arb_market()), g, {"betsson": bp, "betano": ap})
    results = await orch.run_once()
    assert results == [] and bp.calls == 0
