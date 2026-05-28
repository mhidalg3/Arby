"""Risk daemon — tails `arb:opportunities`, emits `arb:risk_decisions`.

The third long-running process in the arby pipeline:

    [scrapers] → odds:raw → [detector] → arb:opportunities → [risk_daemon] → arb:risk_decisions

For every detected opportunity, the daemon runs the deterministic
risk-evaluator cascade and writes the full `RiskDecision` to the
`arb:risk_decisions` stream — APPROVED and REJECTED alike. The
execution agent (future, not in scope) consumes the same stream
filtered on `verdict == "APPROVED"`.

Decisions are emitted for ALL opportunities, even rejected ones,
because:
1. Audit visibility — the operator can grep rejection reasons.
2. Rate analysis — distinguish "low arb rate" from "high rejection rate".
3. Policy iteration — reviewing rejections retroactively informs
   policy tuning without re-running ingestion.

This module is intentionally narrow: parse stream entries → call
evaluator → serialize decision → XADD. No state beyond loop
bookkeeping. Stateful enrichment (persistence checks, fixture
history) is a future enhancement.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
from redis.asyncio import Redis

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.arbitrage.quotes import OddsQuote
from src.risk.decision import RiskDecision, Verdict
from src.risk.evaluator import RiskEvaluator
from src.risk.policy import RiskPolicy
from src.semantic.arb_detector import OUTPUT_STREAM_NAME as DETECTOR_OUTPUT_STREAM

log = structlog.get_logger(__name__)

INPUT_STREAM_NAME: Final[str] = DETECTOR_OUTPUT_STREAM  # "arb:opportunities"
OUTPUT_STREAM_NAME: Final[str] = "arb:risk_decisions"

# Approximate stream cap. ~30 days of audit at observed arb rates
# (~30/hour raw emissions, ~0.5/min) fits well within 50k.
DEFAULT_OUTPUT_MAXLEN: Final[int] = 50_000

# XREAD pacing — same shape as the detector.
DEFAULT_XREAD_BLOCK_MS: Final[int] = 1_000
DEFAULT_XREAD_COUNT: Final[int] = 500


def opportunity_from_stream_fields(
    fields: dict[str, str],
) -> ArbitrageOpportunity | None:
    """Inverse of the detector's `opportunity_to_stream_fields`.

    Returns None on malformed input — caller logs and skips.
    """
    try:
        legs_json = fields["legs_json"]
        legs_data = json.loads(legs_json)
        if not isinstance(legs_data, list) or not legs_data:
            return None
        market_id = fields["market_id"]
        legs: list[OddsQuote] = []
        stakes: list[float] = []
        for leg in legs_data:
            if not isinstance(leg, dict):
                return None
            ms = leg.get("max_stake")
            poid = leg.get("platform_outcome_id")
            peid = leg.get("platform_event_id")
            legs.append(
                OddsQuote(
                    platform=str(leg["platform"]),
                    market_id=market_id,
                    outcome=str(leg["outcome"]),
                    decimal_odds=float(leg["decimal_odds"]),
                    max_stake=float(ms) if isinstance(ms, int | float) else None,
                    timestamp=float(leg["timestamp"]),
                    platform_outcome_id=str(poid) if isinstance(poid, str) else None,
                    platform_event_id=str(peid) if isinstance(peid, str) else None,
                )
            )
            stakes.append(float(leg["stake"]))
        return ArbitrageOpportunity(
            legs=tuple(legs),
            stakes=tuple(stakes),
            total_stake=float(fields["total_stake"]),
            guaranteed_profit=float(fields["guaranteed_profit"]),
            margin_pct=float(fields["margin_pct"]),
            realized_roi_pct=float(fields["realized_roi_pct"]),
            capital_utilization=float(fields["capital_utilization"]),
        )
    except (KeyError, ValueError, TypeError):
        return None


def decision_to_stream_fields(decision: RiskDecision) -> dict[str, str]:
    """Flatten a `RiskDecision` into a Redis stream field-value map.

    Audit-friendly: every field is a top-level entry so the operator
    can `XRANGE arb:risk_decisions - +` and filter without JSON
    decode for the common fields (verdict, reason, fixture_id,
    market_id, realized_roi_pct, confidence).
    """
    return {
        "verdict": decision.verdict.value,
        "reason": decision.reason,
        "rules_evaluated": ",".join(decision.rules_evaluated),
        "confidence": f"{decision.confidence:.6f}",
        "high_margin_warning": "1" if decision.high_margin_warning else "0",
        "fixture_id": decision.fixture_id,
        "market_id": decision.market_id,
        "realized_roi_pct": f"{decision.realized_roi_pct:.6f}",
        "platforms": ",".join(decision.platforms),
        "evaluated_at": f"{decision.evaluated_at:.6f}",
    }


@dataclass
class RiskDaemon:
    """Async loop: read `arb:opportunities` → evaluate → write `arb:risk_decisions`."""

    redis_client: Redis
    evaluator: RiskEvaluator
    input_stream: str = INPUT_STREAM_NAME
    output_stream: str = OUTPUT_STREAM_NAME
    output_maxlen: int = DEFAULT_OUTPUT_MAXLEN
    xread_block_ms: int = DEFAULT_XREAD_BLOCK_MS
    xread_count: int = DEFAULT_XREAD_COUNT
    # Initial XREAD ID. `$` means "only new entries from the moment
    # we start" — the typical mode. Set to `0` to replay the entire
    # existing stream (useful for retroactive evaluation of captured
    # opportunities — e.g., re-evaluating the 30-min run with a
    # different policy).
    start_id: str = "$"

    _log: structlog.BoundLogger = field(init=False)

    def __post_init__(self) -> None:
        self._log = log.bind(component="risk_daemon")

    async def run(self, stop_event: asyncio.Event) -> None:
        last_id = self.start_id
        self._log.info(
            "daemon.started",
            input_stream=self.input_stream,
            output_stream=self.output_stream,
            start_id=self.start_id,
        )

        opportunities_seen = 0
        approved = 0
        rejected = 0

        while not stop_event.is_set():
            try:
                entries = await self.redis_client.xread(
                    {self.input_stream: last_id},
                    block=self.xread_block_ms,
                    count=self.xread_count,
                )
            except Exception as exc:
                self._log.exception("daemon.xread_failed", error=str(exc))
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                    break
                except TimeoutError:
                    continue
            if not entries:
                continue
            for _stream_name, items in entries:
                for entry_id, fields in items:
                    last_id = (
                        entry_id
                        if isinstance(entry_id, str)
                        else entry_id.decode()
                    )
                    opportunities_seen += 1
                    verdict = await self._process_entry(fields)
                    if verdict is Verdict.APPROVED:
                        approved += 1
                    elif verdict is Verdict.REJECTED:
                        rejected += 1
            if opportunities_seen % 100 == 0 and opportunities_seen:
                self._log.debug(
                    "daemon.progress",
                    seen=opportunities_seen,
                    approved=approved,
                    rejected=rejected,
                )

        self._log.info(
            "daemon.stopped",
            opportunities_seen=opportunities_seen,
            approved=approved,
            rejected=rejected,
        )

    async def _process_entry(self, fields: dict[str, Any]) -> Verdict | None:
        decoded = {
            (k.decode() if isinstance(k, bytes) else k): (
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in fields.items()
        }
        opp = opportunity_from_stream_fields(decoded)
        if opp is None:
            self._log.warning("daemon.malformed_entry", fields=decoded)
            return None

        decision = self.evaluator.evaluate(opp, now=time.time())

        try:
            await self.redis_client.xadd(
                self.output_stream,
                decision_to_stream_fields(decision),  # type: ignore[arg-type]
                maxlen=self.output_maxlen,
                approximate=True,
            )
        except Exception as exc:
            # Best-effort emission. Audit visibility is degraded on a
            # write failure but we don't block the loop — same policy
            # as the detector + sink.
            self._log.warning(
                "daemon.emit_failed",
                fixture_id=decision.fixture_id,
                market_id=decision.market_id,
                error=str(exc),
            )
            return decision.verdict

        if decision.verdict is Verdict.APPROVED:
            self._log.info(
                "risk.approved",
                fixture_id=decision.fixture_id,
                market_id=decision.market_id,
                realized_roi_pct=decision.realized_roi_pct,
                confidence=decision.confidence,
                high_margin_warning=decision.high_margin_warning,
                platforms=list(decision.platforms),
            )
        else:
            self._log.info(
                "risk.rejected",
                fixture_id=decision.fixture_id,
                market_id=decision.market_id,
                realized_roi_pct=decision.realized_roi_pct,
                reason=decision.reason,
                rules_evaluated=list(decision.rules_evaluated),
                platforms=list(decision.platforms),
            )
        return decision.verdict


# Re-export the policy default so the script entry point can build a
# `RiskDaemon(evaluator=RiskEvaluator(policy=RiskPolicy()))` without
# importing every submodule.
__all__ = [
    "INPUT_STREAM_NAME",
    "OUTPUT_STREAM_NAME",
    "RiskDaemon",
    "RiskPolicy",
    "decision_to_stream_fields",
    "opportunity_from_stream_fields",
]
