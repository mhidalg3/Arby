"""The arbitrage orchestration loop: quotes → detect → risk → execute.

Ties the deterministic layers together: a `QuoteSource` yields canonical quotes
grouped by market (the cross-platform canonicalization — team/outcome alignment,
LLM partition validation — lives upstream in `src/semantic` and is hidden behind
this seam); for each market we run `detect_arbitrage`, gate it through the
`RiskEvaluator`, and place an APPROVED two-leg opportunity via the `Executor`
(through `arb_executor.execute_opportunity`).

Deliberately conservative: opportunities execute **sequentially** (so the shared
guardrails' exposure caps are respected, not raced); each market is executed at
most once (`_executed`) so a persistent arb isn't re-placed every poll; and the
loop stops as soon as the kill switch trips.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import structlog

from src.arbitrage.dutch_book import detect_arbitrage
from src.arbitrage.quotes import OddsQuote
from src.execution.arb_executor import execute_opportunity
from src.execution.executor import ExecutionResult, Executor
from src.execution.guardrails import Guardrails
from src.risk.decision import Verdict
from src.risk.evaluator import RiskEvaluator

log = structlog.get_logger(__name__)


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
    ) -> None:
        self._quotes = quote_source
        self._risk = risk_evaluator
        self._executor = executor
        self._guardrails = guardrails
        self._budget = budget_ars
        self._min_margin_pct = min_margin_pct
        self._executed: set[str] = set()  # market_ids already acted on (dedup)
        self._log = log.bind(component="orchestrator")

    async def run_once(self) -> list[ExecutionResult]:
        """One pass: detect + (if approved) execute every fresh market. Returns the
        execution results produced this pass."""
        if self._guardrails.kill_switch_tripped:
            return []
        results: list[ExecutionResult] = []
        by_market = await self._quotes.fetch()
        for market_id, quotes in by_market.items():
            if self._guardrails.kill_switch_tripped:
                break
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
            # Mark BEFORE executing: we don't retry a market on failure either (a
            # failed/naked leg needs human attention, not an automatic re-fire).
            self._executed.add(market_id)
            self._log.info(
                "orchestrator.executing", market_id=market_id, roi_pct=opp.realized_roi_pct
            )
            res = await execute_opportunity(self._executor, opp, opp_id=market_id)
            self._log.info("orchestrator.executed", market_id=market_id, outcome=res.outcome)
            results.append(res)
        return results

    async def run_forever(self, poll_interval_sec: float = 5.0) -> None:
        """Poll until the kill switch trips. Per-cycle errors are logged and the
        loop continues (resilience); a tripped kill switch stops it."""
        self._log.info("orchestrator.start", budget_ars=self._budget, poll=poll_interval_sec)
        while not self._guardrails.kill_switch_tripped:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 — keep polling through transient faults
                self._log.error("orchestrator.cycle_error", error=str(exc))
            await asyncio.sleep(poll_interval_sec)
        self._log.warning("orchestrator.stopped", reason="kill switch tripped")
