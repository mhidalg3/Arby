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
The same is true of an APPROVED arb in the high-margin band
(`RiskDecision.high_margin_warning`): it is alerted and audit-recorded but never
auto-placed — the operator verifies the quotes are genuine before placing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Protocol

import structlog

from src.arbitrage.dutch_book import ArbitrageOpportunity, detect_arbitrage
from src.arbitrage.garch import AdaptiveThreshold
from src.arbitrage.quotes import OddsQuote
from src.execution.arb_executor import execute_opportunity, order_opportunity_for_execution
from src.execution.audit import AuditRecorder, NullRecorder
from src.execution.executor import ExecutionResult, Executor
from src.execution.guardrails import Guardrails
from src.execution.notify import Notifier, NullNotifier
from src.risk.decision import Verdict
from src.risk.evaluator import RiskEvaluator

log = structlog.get_logger(__name__)


def format_arb_alert(
    opp_id: str, opp: ArbitrageOpportunity, home_team: str = "", away_team: str = ""
) -> str:
    """Operator-facing arb alert: enough to place it manually if auto-placement
    is suspended (each leg's platform, outcome, odds and stake). Team names are
    shown when the quote source exposes them (duck-typed ``market_names``); the
    canonical ``opp_id`` is always included so the operator can cross-reference."""
    fixture = f"{home_team} vs {away_team}" if home_team and away_team else opp_id
    lines = [
        f"🎯 ARB {fixture} | ROI {opp.realized_roi_pct:.2f}% | stake {sum(opp.stakes):.0f} ARS"
    ]
    if home_team and away_team:
        lines.append(f"  market {opp_id}")
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
        staleness_rank: Mapping[str, Mapping[str, float]] | None = None,
        empty_alert_after: int = 5,
        platform_stale_after_sec: float = 180.0,
        adaptive_threshold: AdaptiveThreshold | None = None,
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
        self._staleness_rank = staleness_rank
        # GARCH adaptive margin threshold; None ⇒ byte-identical static behavior.
        self._adaptive = adaptive_threshold
        self._log = log.bind(component="orchestrator")

    def stop(self) -> None:
        """Ask `run_forever` to exit after the current cycle (operator shutdown)."""
        self._stop = True

    async def run_once(self) -> list[ExecutionResult]:
        """One detection pass over every fresh market. Approved arbs are alerted
        (and auto-placed when the kill switch is clear and the margin is below the
        high-margin band; handed off for manual placement/verification otherwise).
        Detection runs regardless of the kill switch. Returns the execution results
        auto-placed this pass."""
        results: list[ExecutionResult] = []
        by_market = await self._quotes.fetch()
        # GARCH adaptive threshold: evolve σ²_t once per FULL cycle (never on the
        # trigger path — the recursion needs evenly spaced sampling instants). The
        # values sampled here may have been refreshed by trigger bursts via the union
        # cache; the cadence contract governs WHEN we sample, not the provenance.
        if self._adaptive is not None:
            spreads_fn = getattr(self._quotes, "cycle_spreads", None)
            if spreads_fn is not None:
                self._adaptive.observe_cycle(spreads_fn())
        # Team names for human-readable alerts (duck-typed like `stale_platforms`;
        # the source rebuilds this on every fetch, so it is never stale across cycles).
        market_names: dict[str, tuple[str, str]] = getattr(self._quotes, "market_names", {})
        await self._track_ingestion(len(by_market))
        await self._track_platform_freshness()
        for market_id, quotes in by_market.items():
            res = await self._process_market(market_id, quotes, market_names)
            if res is not None:
                results.append(res)
        return results

    async def _process_market(
        self,
        market_id: str,
        quotes: list[OddsQuote],
        market_names: dict[str, tuple[str, str]],
    ) -> ExecutionResult | None:
        """Detect → risk → order → alert → record → execute for ONE market.

        Shared by ``run_once`` (full cycle) and the trigger loop in ``run_forever``
        (burst cycle). The ``_executed`` dedup, leg ordering, and all placement
        verification apply identically in both paths — a trigger-driven arb gets
        NO shortcut. Returns the execution result when auto-placed, None otherwise
        (rejected / high-margin / kill-switch / already-executed)."""
        if market_id in self._executed or len(quotes) < 2:
            return None
        # Per-market adaptive threshold (GARCH σ²_t ratio). None ⇒ static base —
        # the SAME threshold admits the arb at detect AND reverify time (below).
        if self._adaptive is not None:
            tdec = self._adaptive.decide(market_id)
            min_margin, garch_var = tdec.threshold_pct, tdec.garch_variance
        else:
            min_margin, garch_var = self._min_margin_pct, None
        try:
            opp = detect_arbitrage(quotes, self._budget, min_margin)
        except ValueError as exc:  # malformed quotes — skip this market
            self._log.warning("orchestrator.detect_error", market_id=market_id, error=str(exc))
            return None
        if opp is None:
            return None
        decision = self._risk.evaluate(opp)
        if decision.verdict is not Verdict.APPROVED:
            self._log.info("orchestrator.rejected", market_id=market_id, reason=decision.reason)
            return None
        # Approved. Act once per market (dedup) — alert the operator either way.
        self._executed.add(market_id)
        # Permute legs into placement order ONCE, before any consumer: the alert,
        # record_opportunity, execute_opportunity, and record_execution all zip
        # positionally against opp.legs/opp.stakes, so a single permuted opp keeps
        # them mutually consistent (leg_placement_order_decision.md).
        opp = order_opportunity_for_execution(opp, staleness_rank=self._staleness_rank)
        self._log.info(
            "orchestrator.arb_found",
            market_id=market_id,
            roi_pct=opp.realized_roi_pct,
            placement_order=[q.platform for q in opp.legs],
            threshold_pct=min_margin,
            garch_variance=garch_var,
        )
        home, away = market_names.get(market_id, ("", ""))
        await self._notifier.send(format_arb_alert(market_id, opp, home, away))
        opp_db_id = await self._recorder.record_opportunity(
            market_id,
            opp,
            decision,
            adaptive_threshold_pct=min_margin if self._adaptive is not None else None,
            garch_variance=garch_var,
        )
        if decision.high_margin_warning:
            self._log.warning(
                "orchestrator.high_margin_handoff",
                market_id=market_id,
                roi_pct=opp.realized_roi_pct,
            )
            await self._notifier.send(
                f"⚠️ {market_id}: ROI {opp.realized_roi_pct:.2f}% is in the high-margin band "
                f"(≥{self._risk.policy.high_margin_warning_pct:.0f}%) — NOT auto-placed. "
                "Verify the quotes are genuine (stale odds? in-play? mismatched partition?) "
                "and place MANUALLY only if the arb is real."
            )
            return None
        if self._guardrails.kill_switch_tripped:
            self._log.warning("orchestrator.manual_handoff", market_id=market_id)
            await self._notifier.send(
                f"✋ {market_id}: auto-placement suspended "
                f"({self._guardrails.kill_switch_reason}) — place this one MANUALLY."
            )
            return None
        self._log.info("orchestrator.executing", market_id=market_id)
        res = await execute_opportunity(
            self._executor,
            opp,
            opp_id=market_id,
            dynamic_stake_cap_ars=self._dynamic_cap,
            budget=self._budget,
            min_margin_pct=min_margin,
        )
        self._log.info("orchestrator.executed", market_id=market_id, outcome=res.outcome)
        if opp_db_id is not None:
            await self._recorder.record_execution(opp_db_id, opp, res)
        return res

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

    async def run_forever(
        self,
        poll_interval_sec: float = 5.0,
        trigger_interval_sec: float = 0.0,
    ) -> None:
        """Poll until the operator calls :meth:`stop` (or cancels). Per-cycle errors
        are alerted (de-duped) and the loop continues — detection NEVER halts on a
        fault or a tripped kill switch.

        When ``trigger_interval_sec > 0`` and the source supports ``trigger_fetch``,
        trigger mini-cycles run between full cycles. ``trigger_interval_sec=0``
        (default) disables triggering; behavior is byte-identical to today."""
        self._log.info(
            "orchestrator.start",
            budget_ars=self._budget,
            poll=poll_interval_sec,
            trigger=trigger_interval_sec,
        )
        while not self._stop:
            try:
                await self.run_once()
                self._last_error = None
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                self._log.error("orchestrator.cycle_error", error=msg)
                if msg != self._last_error:
                    await self._notifier.send(f"⚠️ Loop error (detection continues): {msg}")
                    self._last_error = msg
            if trigger_interval_sec > 0 and hasattr(self._quotes, "trigger_fetch"):
                # Wall-clock deadline so trigger_fetch/_process_market WORK time counts
                # toward the full-poll cadence (no drift — the next full cycle fires on
                # schedule regardless of how long the mini-cycles take).
                deadline = time.monotonic() + poll_interval_sec
                while not self._stop:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(trigger_interval_sec, remaining))
                    if self._stop:
                        break
                    try:
                        by_market = await self._quotes.trigger_fetch()
                        if by_market:
                            names = getattr(self._quotes, "market_names", {})
                            self._log.info("orchestrator.trigger_cycle", moved=len(by_market))
                            for mid, quotes in by_market.items():
                                await self._process_market(mid, quotes, names)
                    except Exception as exc:  # noqa: BLE001
                        self._log.warning("orchestrator.trigger_error", error=str(exc))
            else:
                await asyncio.sleep(poll_interval_sec)
        self._log.warning("orchestrator.stopped", reason="operator stop")
