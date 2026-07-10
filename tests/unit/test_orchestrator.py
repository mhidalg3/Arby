"""Tests for the arbitrage orchestration loop (detect → risk → execute)."""

from __future__ import annotations

import asyncio

from src.arbitrage.dutch_book import detect_arbitrage
from src.arbitrage.quotes import OddsQuote
from src.execution.executor import ExecutionOutcome, Executor, Leg, PlacementResult
from src.execution.guardrails import Guardrails
from src.execution.orchestrator import ArbOrchestrator, format_arb_alert
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


class _Recorder:
    """Recording audit recorder — captures every opportunity/execution call."""

    def __init__(self) -> None:
        self.opps: list[tuple] = []
        self.execs: list[tuple] = []
        self._next = 1

    async def record_opportunity(
        self,
        market_id,  # type: ignore[no-untyped-def]
        opp,
        decision,
        *,
        adaptive_threshold_pct: float | None = None,
        garch_variance: float | None = None,
    ) -> int:
        self.opps.append((market_id, opp, decision, adaptive_threshold_pct, garch_variance))
        oid = self._next
        self._next += 1
        return oid

    async def record_execution(self, opportunity_id, opp, result) -> None:  # type: ignore[no-untyped-def]
        self.execs.append((opportunity_id, opp, result))


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
    recorder: _Recorder | None = None,
    risk: RiskEvaluator | None = None,
    empty_alert_after: int = 5,
    adaptive: object | None = None,
) -> ArbOrchestrator:
    ex = Executor(
        guardrails=guard,
        notifier=notifier or _Notifier(),
        recovery=_Recovery(),
        placers=placers,  # type: ignore[arg-type]
    )
    return ArbOrchestrator(
        quote_source=source,
        risk_evaluator=risk or _risk(),
        executor=ex,
        guardrails=guard,
        budget_ars=1000.0,
        min_margin_pct=1.0,
        notifier=notifier,
        recorder=recorder,  # type: ignore[arg-type]
        empty_alert_after=empty_alert_after,
        adaptive_threshold=adaptive,  # type: ignore[arg-type]
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


def test_format_arb_alert_shows_team_names_when_provided() -> None:
    """The duck-typed `market_names` seam renders the fixture name in the alert
    while keeping the canonical market id for cross-reference; without names the
    alert falls back to the market id only (backwards-compatible)."""
    opp = detect_arbitrage(
        [_quote("betsson", "home", 2.1), _quote("betano", "away", 2.1)],
        budget=1000.0,
        min_margin_pct=0.5,
    )
    assert opp is not None

    with_names = format_arb_alert("fx-abc123|1x2", opp, "Botafogo-PB", "Brusque-SC")
    assert "Botafogo-PB vs Brusque-SC" in with_names
    assert "fx-abc123|1x2" in with_names  # market id retained for cross-reference

    plain = format_arb_alert("fx-abc123|1x2", opp)
    assert "fx-abc123|1x2" in plain
    assert " vs " not in plain  # no fixture line when names are absent


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
    orch = _orch(
        empty, g, {"betsson": _Placer(), "betano": _Placer()}, notifier=note, empty_alert_after=2
    )
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


async def test_approved_arb_records_opportunity_and_execution() -> None:
    """An approved, auto-placed arb is recorded both as an opportunity (with the
    durable id returned) and as its execution outcome."""
    g = _guard()
    bp, ap = _Placer(), _Placer()
    rec = _Recorder()
    orch = _orch(_FakeQuoteSource(_arb_market()), g, {"betsson": bp, "betano": ap}, recorder=rec)
    await orch.run_once()
    assert len(rec.opps) == 1
    assert len(rec.execs) == 1
    assert rec.execs[0][0] == 1  # the id returned by record_opportunity


async def test_kill_switch_handoff_records_opportunity_not_execution() -> None:
    """A tripped kill switch hands the arb off for manual placement: the
    opportunity is still recorded (as APPROVED) but no execution is."""
    g = _guard()
    g.trip_kill_switch("session cold")
    bp, ap = _Placer(), _Placer()
    rec = _Recorder()
    orch = _orch(_FakeQuoteSource(_arb_market()), g, {"betsson": bp, "betano": ap}, recorder=rec)
    await orch.run_once()
    assert len(rec.opps) == 1
    assert rec.execs == []


def _risky() -> RiskEvaluator:
    # Same permissive shape as _risk(), but the warning band starts at 1% so the
    # standard ~5%-ROI fixture lands inside it (flag set, still APPROVED).
    return RiskEvaluator(
        policy=RiskPolicy(
            min_margin_pct=0.5,
            min_confidence=0.0,
            platform_reliability={"betsson": 1.0, "betano": 1.0},
            high_margin_warning_pct=1.0,
        )
    )


async def test_high_margin_arb_not_auto_placed_and_handed_off() -> None:
    """An APPROVED arb inside the high-margin band is alerted + audit-recorded but
    handed to the operator for manual verification — never auto-placed."""
    g, note = _guard(), _Notifier()
    bp, ap = _Placer(), _Placer()
    rec = _Recorder()
    orch = _orch(
        _FakeQuoteSource(_arb_market()),
        g,
        {"betsson": bp, "betano": ap},
        notifier=note,
        recorder=rec,
        risk=_risky(),
    )
    results = await orch.run_once()
    assert results == [] and bp.calls == 0 and ap.calls == 0  # nothing auto-placed
    assert any(m.startswith("🎯 ARB") for m in note.sent)  # still alerted
    assert any("NOT auto-placed" in m for m in note.sent)  # warning reached Telegram
    assert len(rec.opps) == 1 and rec.execs == []  # audit row, no execution


async def test_high_margin_handoff_takes_precedence_over_kill_switch() -> None:
    """Both gates tripped → only the high-margin (verify-first) message fires."""
    g, note = _guard(), _Notifier()
    g.trip_kill_switch("session cold")
    orch = _orch(
        _FakeQuoteSource(_arb_market()),
        g,
        {"betsson": _Placer(), "betano": _Placer()},
        notifier=note,
        risk=_risky(),
    )
    await orch.run_once()
    assert any("NOT auto-placed" in m for m in note.sent)
    assert not any("auto-placement suspended" in m for m in note.sent)


async def test_orchestrator_orders_bw_first_and_records_permuted_opp() -> None:
    """The orchestrator permutes legs into placement order (BW-first) at the seam, so
    the executor places BetWarrior before Betsson AND record_execution receives the
    SAME permuted opp — positional consistency between placement order and audit
    (record_execution pairs result.legs[i] with opp.legs[i])."""
    order: list[str] = []

    class _OrderPlacer:
        def __init__(self) -> None:
            self.calls = 0

        async def place(self, leg: Leg) -> PlacementResult:
            self.calls += 1
            order.append(leg.platform)
            return PlacementResult(accepted=True, stake_filled=leg.stake_ars, odds_filled=leg.odds)

    bw, bs = _OrderPlacer(), _OrderPlacer()
    # Detector order is Betsson-first; the orchestrator permutes to BW-first.
    market = {
        "FIX1|1X2": [
            _quote("betsson-pba", "away", 2.1),
            _quote("betwarrior-pba", "home", 2.1),
        ]
    }
    rec = _Recorder()
    orch = _orch(
        _FakeQuoteSource(market),
        _guard(),
        {"betwarrior-pba": bw, "betsson-pba": bs},
        recorder=rec,
    )
    results = await orch.run_once()
    assert len(results) == 1 and results[0].outcome is ExecutionOutcome.COMPLETED
    # BetWarrior leg placed FIRST (fragile-auth ordering), Betsson second.
    assert order == ["betwarrior-pba", "betsson-pba"]
    # record_opportunity AND record_execution both received the SAME permuted opp —
    # the seam is before both, so audit intent and execution stay positionally aligned.
    assert len(rec.opps) == 1
    assert [q.platform for q in rec.opps[0][1].legs] == ["betwarrior-pba", "betsson-pba"]
    assert len(rec.execs) == 1
    recorded_opp = rec.execs[0][1]
    assert [q.platform for q in recorded_opp.legs] == ["betwarrior-pba", "betsson-pba"]


# ---- Phase C: trigger path tests ----


class _TriggerSource:
    """Quote source with both fetch() and trigger_fetch() — tracks calls separately."""

    def __init__(
        self,
        full: dict[str, list[OddsQuote]],
        trigger: dict[str, list[OddsQuote]] | None = None,
    ) -> None:
        self._full = full
        self._trigger = trigger or {}
        self.fetch_calls = 0
        self.trigger_calls = 0

    async def fetch(self) -> dict[str, list[OddsQuote]]:
        self.fetch_calls += 1
        return self._full

    async def trigger_fetch(self) -> dict[str, list[OddsQuote]]:
        self.trigger_calls += 1
        return self._trigger

    @property
    def market_names(self) -> dict[str, tuple[str, str]]:
        return {}


async def test_process_market_dedup_prevents_reexecution() -> None:
    """A market executed via _process_market is not re-executed on the next call
    (the _executed dedup that both full-cycle and trigger paths share)."""
    g = _guard()
    bp, ap = _Placer(), _Placer()
    src = _TriggerSource(_arb_market())
    orch = _orch(src, g, {"betsson": bp, "betano": ap})  # type: ignore[arg-type]
    market_id = "FIX1|1X2"
    quotes = _arb_market()[market_id]
    # First call: executes the arb
    res1 = await orch._process_market(market_id, quotes, {})
    assert res1 is not None
    assert bp.calls == 1 and ap.calls == 1
    # Second call: deduped (market already in _executed)
    res2 = await orch._process_market(market_id, quotes, {})
    assert res2 is None
    assert bp.calls == 1 and ap.calls == 1  # no additional placement


async def test_trigger_fetch_zero_interval_makes_no_trigger_calls() -> None:
    """With trigger_interval_sec=0, run_forever never calls trigger_fetch.
    (Byte-identical to today — verified by the source's trigger_calls counter.)"""
    g = _guard()
    bp, ap = _Placer(), _Placer()
    src = _TriggerSource(_arb_market(), trigger=_arb_market())
    orch = _orch(src, g, {"betsson": bp, "betano": ap})  # type: ignore[arg-type]

    # Run one iteration of run_forever with trigger disabled, then stop
    async def _stop_after_one() -> None:
        await asyncio.sleep(0.1)
        orch.stop()

    await asyncio.gather(
        orch.run_forever(poll_interval_sec=0.05, trigger_interval_sec=0.0),
        _stop_after_one(),
    )
    assert src.trigger_calls == 0


# ---- Phase E: GARCH adaptive threshold ----


class _StubAdaptive:
    """Minimal AdaptiveThreshold stub: a fixed decide() result + observe counter."""

    def __init__(self, threshold: float, variance: float | None = 42.0) -> None:
        from src.arbitrage.garch import ThresholdDecision

        self._decision = ThresholdDecision(threshold_pct=threshold, garch_variance=variance)
        self.observed = 0

    def observe_cycle(self, observations: object) -> None:
        self.observed += sum(1 for _ in observations)  # type: ignore[arg-type]

    def decide(self, market_id: str):  # type: ignore[no-untyped-def]
        return self._decision


async def test_adaptive_threshold_above_margin_rejects_arb() -> None:
    """When decide() returns a threshold above the arb's ~5% margin, detect_arbitrage
    finds no arb → nothing recorded, nothing placed."""
    g = _guard()
    bp, ap = _Placer(), _Placer()
    rec = _Recorder()
    adaptive = _StubAdaptive(threshold=10.0)  # 10% gate > ~5% arb margin
    orch = _orch(
        _FakeQuoteSource(_arb_market()),
        g,
        {"betsson": bp, "betano": ap},
        recorder=rec,
        adaptive=adaptive,
    )
    await orch.run_once()
    assert rec.opps == [] and bp.calls == 0  # rejected at detection — no record, no place


async def test_adaptive_threshold_below_margin_finds_arb_and_records_kwargs() -> None:
    """When decide() returns a threshold below the arb's margin, the arb is found and
    the fake recorder captures adaptive_threshold_pct + garch_variance."""
    g = _guard()
    bp, ap = _Placer(), _Placer()
    rec = _Recorder()
    adaptive = _StubAdaptive(threshold=0.5, variance=123.4)  # 0.5% gate < ~5% margin
    orch = _orch(
        _FakeQuoteSource(_arb_market()),
        g,
        {"betsson": bp, "betano": ap},
        recorder=rec,
        adaptive=adaptive,
    )
    await orch.run_once()
    assert len(rec.opps) == 1
    _, _, _, thr_pct, gvar = rec.opps[0]
    assert thr_pct == 0.5
    assert gvar == 123.4
    assert bp.calls == 1 and ap.calls == 1  # placed


async def test_adaptive_threshold_no_cycle_spreads_attribute_no_crash() -> None:
    """A quote source without cycle_spreads + adaptive set → the getattr guard skips
    observe_cycle; detection still runs (the None path)."""
    g = _guard()
    bp, ap = _Placer(), _Placer()
    adaptive = _StubAdaptive(threshold=0.5)
    # _FakeQuoteSource has no cycle_spreads attribute.
    orch = _orch(
        _FakeQuoteSource(_arb_market()), g, {"betsson": bp, "betano": ap}, adaptive=adaptive
    )
    await orch.run_once()  # must not raise
    assert adaptive.observed == 0  # observe_cycle never called (no cycle_spreads)
    assert bp.calls == 1  # arb still found and placed (static threshold unaffected)
