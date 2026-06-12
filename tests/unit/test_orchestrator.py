"""Tests for the arbitrage orchestration loop (detect → risk → execute)."""

from __future__ import annotations

import asyncio

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


class _FreshnessSource:
    """Quote source that also reports per-platform ingestion staleness (the optional
    `stale_platforms` seam the orchestrator duck-types)."""

    def __init__(self) -> None:
        self.by_market: dict[str, list[OddsQuote]] = {}
        self.stale: dict[str, float] = {}

    async def fetch(self) -> dict[str, list[OddsQuote]]:
        return self.by_market

    def stale_platforms(self, max_age_sec: float) -> dict[str, float]:
        return dict(self.stale)


class _Placer:
    def __init__(self) -> None:
        self.calls = 0

    async def place(self, leg: Leg) -> PlacementResult:
        self.calls += 1
        return PlacementResult(accepted=True, stake_filled=leg.stake_ars, odds_filled=leg.odds)


class _Notifier:
    """Recording notifier — captures every message sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
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
    source: _FakeQuoteSource,
    guard: Guardrails,
    placers: dict[str, _Placer],
    notifier: _Notifier | None = None,
    empty_alert_after: int = 5,
) -> ArbOrchestrator:
    ex = Executor(
        guardrails=guard,
        notifier=notifier or _Notifier(),
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
        notifier=notifier,
        empty_alert_after=empty_alert_after,
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


async def test_arb_found_is_alerted() -> None:
    g, note = _guard(), _Notifier()
    bp, ap = _Placer(), _Placer()
    orch = _orch(_FakeQuoteSource(_arb_market()), g, {"betsson": bp, "betano": ap}, notifier=note)
    await orch.run_once()
    assert any(m.startswith("🎯 ARB") for m in note.sent)


async def test_kill_switch_suspends_autoplacement_but_hands_off_manually() -> None:
    """A tripped kill switch no longer silently does nothing: the arb is still
    detected + alerted, and the operator is told to place it manually. No auto-bet."""
    g, note = _guard(), _Notifier()
    g.trip_kill_switch("session cold")
    bp, ap = _Placer(), _Placer()
    orch = _orch(_FakeQuoteSource(_arb_market()), g, {"betsson": bp, "betano": ap}, notifier=note)
    results = await orch.run_once()
    assert results == [] and bp.calls == 0 and ap.calls == 0  # nothing auto-placed
    assert any(m.startswith("🎯 ARB") for m in note.sent)  # still alerted
    assert any("MANUALLY" in m for m in note.sent)  # handed off


async def test_run_forever_keeps_detecting_through_kill_switch() -> None:
    """Detection never halts on a tripped kill switch — the loop keeps polling
    until the operator stops it."""
    g, note = _guard(), _Notifier()
    g.trip_kill_switch("session cold")
    src = _FakeQuoteSource(_arb_market())
    orch = _orch(src, g, {"betsson": _Placer(), "betano": _Placer()}, notifier=note)

    async def _stop_after_a_few() -> None:
        await asyncio.sleep(0.05)
        orch.stop()

    await asyncio.gather(orch.run_forever(poll_interval_sec=0.01), _stop_after_a_few())
    assert src.fetches >= 2  # kept detecting despite the kill switch


async def test_ingestion_stall_alerts_once_then_recovers() -> None:
    g, note = _guard(), _Notifier()
    empty = _FakeQuoteSource({})
    orch = _orch(empty, g, {"betsson": _Placer(), "betano": _Placer()}, notifier=note,
                 empty_alert_after=2)
    await orch.run_once()  # 1 empty
    await orch.run_once()  # 2 empty → alert
    await orch.run_once()  # 3 empty → no repeat alert
    stalls = [m for m in note.sent if "No market data" in m]
    assert len(stalls) == 1
    empty.by_market = _arb_market()
    await orch.run_once()  # data returns → recovery alert
    assert any("recovered" in m for m in note.sent)


async def test_per_platform_ingestion_stall_alerts_once_then_recovers() -> None:
    """A single book going dark (here betsson-pba) is alerted per-platform — the gap
    the aggregate market-count check can't see, since the surviving books still
    complete partitions — then a recovery alert when its scrape returns."""
    g, note = _guard(), _Notifier()
    src = _FreshnessSource()
    orch = _orch(src, g, {"betsson": _Placer(), "betano": _Placer()}, notifier=note)  # type: ignore[arg-type]
    await orch.run_once()  # all books live
    assert not any("ingestion stale" in m for m in note.sent)
    src.stale = {"betsson-pba": 200.0}
    await orch.run_once()  # betsson goes dark → alert
    await orch.run_once()  # still dark → no repeat
    stalls = [m for m in note.sent if "betsson-pba ingestion stale" in m]
    assert len(stalls) == 1
    src.stale = {}
    await orch.run_once()  # scrape returns → recovery alert
    assert any("betsson-pba ingestion recovered" in m for m in note.sent)
