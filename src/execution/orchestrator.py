"""The arbitrage orchestration loop: quotes → detect → risk → execute.

Ties the deterministic layers together: a `QuoteSource` yields canonical quotes
grouped by market (the cross-platform canonicalization — team/outcome alignment,
LLM partition validation — lives upstream in `src/semantic` and is hidden behind
this seam); for each market we run `detect_arbitrage`, gate it through the
`RiskEvaluator`, and place an APPROVED N-leg opportunity (2-outcome O/U or
3-outcome 1X2) via the `Executor` (through `arb_executor.execute_opportunity`).

Deliberately conservative: opportunities execute **sequentially** (so the shared
guardrails' exposure caps are respected, not raced); each market is acted on at
most once (`_executed`) so a persistent arb isn't re-placed (or re-alerted) every
poll.

Detection **never halts** — the loop polls until the operator stops it, not when
the kill switch trips. The kill switch gates only *auto-placement*: while it's
tripped (a cold session, a freeze, a daily-loss stop) the loop keeps detecting and,
on each found arb, alerts the operator to place it MANUALLY rather than silently
doing nothing. This is the "notify + manual assist, never stop watching" model.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import structlog

from src.arbitrage.dutch_book import ArbitrageOpportunity, detect_arbitrage
from src.arbitrage.quotes import OddsQuote
from src.execution.arb_executor import execute_opportunity
from src.execution.audit import AuditRecorder, NullRecorder
from src.execution.executor import ExecutionResult, Executor
from src.execution.guardrails import Guardrails
from src.execution.notify import Notifier, NullNotifier
from src.risk.decision import Verdict
from src.risk.evaluator import RiskEvaluator

log = structlog.get_logger(__name__)


def format_arb_alert(opp_id: str, opp: ArbitrageOpportunity) -> str:
    """Operator-facing arb alert: enough to place it manually if auto-placement
    is suspended (each leg's platform, outcome, odds and stake)."""
    lines = [
        f"🎯 ARB {opp_id} | ROI {opp.realized_roi_pct:.2f}% | stake {sum(opp.stakes):.0f} ARS"
    ]
    for q, stake in zip(opp.legs, opp.stakes, strict=True):
        lines.append(f"  • {q.platform} {q.outcome} @ {q.decimal_odds} — {stake:.0f} ARS")
    return "\n".join(lines)


class QuoteSource(Protocol):
    async def fetch(self) -> dict[str, list[OddsQuote]]:
        """Current canonical quotes grouped by `market_id` (cells of a partition,
        one quote per platform per outcome). Implemented by the semantic layer."""
        ...


class ArbOrchestrator:
    """detect → risk → execute, polled."""

    def __init__(
        self,
        *,
        quote_source: QuoteSource,
        risk_evaluator: RiskEvaluator,
        executor: Executor,
        guardrails: Guardrails,
        budget_ars: float,
        min_margin_pct: float = 1.0,
        dynamic_stake_cap_ars: float | None = None,
        notifier: Notifier | None = None,
        recorder: AuditRecorder | None = None,
        empty_alert_after: int = 5,
        platform_stale_after_sec: float = 180.0,
    ) -> None:
        self._quotes = quote_source
        self._risk = risk_evaluator
        self._executor = executor
        self._guardrails = guardrails
        self._budget = budget_ars
        self._min_margin_pct = min_margin_pct
        # Conservative fallback cap for dynamic-limit legs (Betano) whose feed
        # carries no max_stake — without it their guardrail fail-closes.
        self._dynamic_cap = dynamic_stake_cap_ars
        self._notifier = notifier or NullNotifier()
        self._recorder = recorder or NullRecorder()
        # Alert once if detection produces no market data for this many consecutive
        # cycles (ingestion stalled/blocked) — without halting; detection retries.
        self._empty_alert_after = empty_alert_after
        self._empty_cycles = 0
        # Per-platform ingestion liveness: a single book can go dark (WAF 403 / block)
        # while the others still overlap — invisible in the aggregate count above. If the
        # quote source reports per-book freshness, alert when one is stale this long.
        self._platform_stale_after_sec = platform_stale_after_sec
        self._stale_alerted: set[str] = set()  # books currently in a stale-alert state
        self._executed: set[str] = set()  # market_ids already acted on (dedup)
        self._last_error: str | None = None  # de-dup repeated loop-error alerts
        self._stop = False  # operator stop; the kill switch never stops detection
        self._log = log.bind(component="orchestrator")

    def stop(self) -> None:
        """Ask `run_forever` to exit after the current cycle (operator shutdown)."""
        self._stop = True

    async def run_once(self) -> list[ExecutionResult]:
        """One detection pass over every fresh market. Approved arbs are alerted
        (and auto-placed when the kill switch is clear; handed off for manual
        placement when it's tripped). Detection runs regardless of the kill switch.
        Returns the execution results auto-placed this pass."""
        results: list[ExecutionResult] = []
        by_market = await self._quotes.fetch()
        await self._track_ingestion(len(by_market))
        await self._track_platform_freshness()
        for market_id, quotes in by_market.items():
            if market_id in self._executed or len(quotes) < 2:
                continue
            try:
                opp = detect_arbitrage(quotes, self._budget, self._min_margin_pct)
            except ValueError as exc:  # malformed quotes — skip this market
                self._log.warning("orchestrator.detect_error", market_id=market_id, error=str(exc))
                continue
            if opp is None:
                continue
            decision = self._risk.evaluate(opp)
            if decision.verdict is not Verdict.APPROVED:
                self._log.info("orchestrator.rejected", market_id=market_id, reason=decision.reason)
                continue
            # Approved. Act once per market (dedup) — alert the operator either way.
            self._executed.add(market_id)
            self._log.info("orchestrator.arb_found", market_id=market_id, roi_pct=opp.realized_roi_pct)
            await self._notifier.send(format_arb_alert(market_id, opp))
            opp_db_id = await self._recorder.record_opportunity(market_id, opp, decision)
            if self._guardrails.kill_switch_tripped:
                # Auto-placement suspended (cold session / freeze / daily loss) →
                # hand off to the operator; keep detecting the rest.
                self._log.warning("orchestrator.manual_handoff", market_id=market_id)
                await self._notifier.send(
                    f"✋ {market_id}: auto-placement suspended "
                    f"({self._guardrails.kill_switch_reason}) — place this one MANUALLY."
                )
                continue
            self._log.info("orchestrator.executing", market_id=market_id)
            res = await execute_opportunity(
                self._executor, opp, opp_id=market_id, dynamic_stake_cap_ars=self._dynamic_cap
            )
            self._log.info("orchestrator.executed", market_id=market_id, outcome=res.outcome)
            if opp_db_id is not None:
                await self._recorder.record_execution(opp_db_id, opp, res)
            results.append(res)
        return results

    async def _track_ingestion(self, n_markets: int) -> None:
        """Alert (once) if detection goes dry for `_empty_alert_after` cycles, and
        again when data returns — so a stalled/blocked scrape is visible without
        halting the loop."""
        if n_markets > 0:
            if self._empty_cycles >= self._empty_alert_after:
                await self._notifier.send(f"✅ Ingestion recovered — {n_markets} market(s) again.")
            self._empty_cycles = 0
            return
        self._empty_cycles += 1
        if self._empty_cycles == self._empty_alert_after:
            await self._notifier.send(
                f"⚠️ No market data for {self._empty_cycles} cycles — ingestion may be "
                "stalled/blocked. Detection is still running; check the scrapers."
            )

    async def _track_platform_freshness(self) -> None:
        """Per-book ingestion liveness. If the quote source exposes `stale_platforms`,
        alert (once) when a single book's scrape goes dark and again when it recovers —
        the gap the aggregate ingestion check can't see, since the surviving books keep
        completing partitions. Detection never halts; this only makes the block visible."""
        reporter = getattr(self._quotes, "stale_platforms", None)
        if reporter is None:
            return
        stale: dict[str, float] = reporter(self._platform_stale_after_sec)
        for platform, age in stale.items():
            if platform not in self._stale_alerted:
                self._stale_alerted.add(platform)
                self._log.warning("orchestrator.platform_stale", platform=platform, age_sec=age)
                await self._notifier.send(
                    f"⚠️ {platform} ingestion stale ({age:.0f}s with no fresh odds) — that book "
                    "may be blocked. Detection continues on the others; check the scraper."
                )
        for platform in list(self._stale_alerted):
            if platform not in stale:
                self._stale_alerted.discard(platform)
                await self._notifier.send(f"✅ {platform} ingestion recovered.")

    async def run_forever(self, poll_interval_sec: float = 5.0) -> None:
        """Poll until the operator calls :meth:`stop` (or cancels). Per-cycle errors
        are alerted (de-duped) and the loop continues — detection NEVER halts on a
        fault or a tripped kill switch."""
        self._log.info("orchestrator.start", budget_ars=self._budget, poll=poll_interval_sec)
        while not self._stop:
            try:
                await self.run_once()
                self._last_error = None  # a clean cycle re-arms the error alert
            except Exception as exc:  # noqa: BLE001 — keep polling through transient faults
                msg = str(exc)
                self._log.error("orchestrator.cycle_error", error=msg)
                if msg != self._last_error:  # don't spam the same fault every poll
                    await self._notifier.send(f"⚠️ Loop error (detection continues): {msg}")
                    self._last_error = msg
            await asyncio.sleep(poll_interval_sec)
        self._log.warning("orchestrator.stopped", reason="operator stop")
