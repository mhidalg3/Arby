"""Dry-run verifier daemon — characterizes detection→verification drift.

Reads `arb:opportunities`, runs `QuoteVerifier` on each entry, emits
the verdict + per-leg drift to `arb:verification_results`. NO bet
placement. Designed for **measurement**: characterize how often
detected arbs survive a re-verification pass under various market
conditions, so the operator can tune the acceptance policy before
execution is wired up.

Pipeline:

    [arb_detector] → arb:opportunities → [verifier_daemon] → arb:verification_results

Unlike the risk daemon, this consumer doesn't filter on
`arb:risk_decisions` verdict — it verifies every detected
opportunity. The downstream execution agent (future) would JOIN
`arb:risk_decisions.verdict=APPROVED` AND
`arb:verification_results.verdict=STILL_VALID` before placing.

Output stream entries are audit records: verdict, fresh margin,
margin delta, per-leg drift JSON, time-since-detection, and the
freshness tier that was used. After a 10-30 minute run the operator
can analyze rate of STILL_VALID vs DRIFT_BELOW_ACCEPTANCE vs
MARKET_UNAVAILABLE and tune the `VerificationPolicy` thresholds
accordingly.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
from redis.asyncio import Redis

from src.risk.risk_daemon import opportunity_from_stream_fields
from src.risk.verifier import (
    QuoteVerifier,
    VerificationResult,
    VerificationVerdict,
)
from src.semantic.arb_detector import OUTPUT_STREAM_NAME as DETECTOR_OUTPUT_STREAM

log = structlog.get_logger(__name__)

INPUT_STREAM_NAME: Final[str] = DETECTOR_OUTPUT_STREAM  # "arb:opportunities"
OUTPUT_STREAM_NAME: Final[str] = "arb:verification_results"

# Cap mirrors arb:opportunities — we emit at most one record per
# input record.
DEFAULT_OUTPUT_MAXLEN: Final[int] = 50_000

DEFAULT_XREAD_BLOCK_MS: Final[int] = 1_000
DEFAULT_XREAD_COUNT: Final[int] = 500


def verification_to_stream_fields(
    result: VerificationResult,
    fixture_id: str,
    market_id: str,
    home_team: str,
    away_team: str,
) -> dict[str, str]:
    """Flatten a `VerificationResult` to Redis stream fields.

    Audit-friendly: each scalar is a top-level field for easy XRANGE
    inspection without JSON decode. Per-leg drift goes into one
    JSON-encoded field because the array length is variable.
    """
    return {
        "verdict": result.verdict.value,
        "fixture_id": fixture_id,
        "market_id": market_id,
        "home_team": home_team,
        "away_team": away_team,
        "detection_margin_pct": f"{result.detection_margin_pct:.6f}",
        "fresh_margin_pct": (
            "" if result.fresh_margin_pct is None else f"{result.fresh_margin_pct:.6f}"
        ),
        "margin_delta_pct": (
            "" if result.margin_delta_pct is None else f"{result.margin_delta_pct:.6f}"
        ),
        "time_since_detection_sec": f"{result.time_since_detection_sec:.3f}",
        "fully_tier_2": "1" if result.fully_tier_2 else "0",
        "verified_at": f"{result.verified_at:.6f}",
        "reason": result.reason,
        "leg_count": str(len(result.per_leg_drift)),
        "drift_per_leg_json": json.dumps(
            [
                {
                    "platform": d.platform,
                    "outcome": d.outcome,
                    "platform_outcome_id": d.platform_outcome_id,
                    "detection_odds": d.detection_odds,
                    "fresh_odds": d.fresh_odds,
                    "odds_delta_pct": d.odds_delta_pct,
                    "tier": d.tier,
                }
                for d in result.per_leg_drift
            ]
        ),
    }


@dataclass
class VerifierDaemon:
    """Tails `arb:opportunities`, runs the verifier, emits results."""

    redis_client: Redis
    verifier: QuoteVerifier
    input_stream: str = INPUT_STREAM_NAME
    output_stream: str = OUTPUT_STREAM_NAME
    output_maxlen: int = DEFAULT_OUTPUT_MAXLEN
    xread_block_ms: int = DEFAULT_XREAD_BLOCK_MS
    xread_count: int = DEFAULT_XREAD_COUNT
    start_id: str = "$"  # tail by default; "0" replays existing stream

    _log: structlog.BoundLogger = field(init=False)

    def __post_init__(self) -> None:
        self._log = log.bind(component="verifier_daemon")

    async def run(self, stop_event: asyncio.Event) -> None:
        last_id = self.start_id
        self._log.info(
            "daemon.started",
            input_stream=self.input_stream,
            output_stream=self.output_stream,
            start_id=self.start_id,
        )

        seen = 0
        by_verdict: dict[str, int] = {v.value: 0 for v in VerificationVerdict}

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
                    last_id = entry_id if isinstance(entry_id, str) else entry_id.decode()
                    seen += 1
                    verdict = await self._process_entry(fields)
                    if verdict is not None:
                        by_verdict[verdict.value] = by_verdict.get(verdict.value, 0) + 1
            if seen % 50 == 0 and seen:
                self._log.debug(
                    "daemon.progress",
                    seen=seen,
                    by_verdict=by_verdict,
                )

        self._log.info(
            "daemon.stopped",
            seen=seen,
            by_verdict=by_verdict,
        )

    async def _process_entry(self, fields: dict[str, Any]) -> VerificationVerdict | None:
        decoded = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in fields.items()
        }
        opp = opportunity_from_stream_fields(decoded)
        if opp is None:
            self._log.warning("daemon.malformed_entry", fields=decoded)
            return None

        try:
            detected_at = float(decoded.get("detected_at", "0") or "0")
        except ValueError:
            detected_at = time.time()
        if detected_at <= 0:
            detected_at = time.time()

        result = await self.verifier.verify_opportunity(opp, detected_at=detected_at)

        fields_out = verification_to_stream_fields(
            result,
            fixture_id=decoded.get("fixture_id", ""),
            market_id=decoded.get("market_id", ""),
            home_team=decoded.get("home_team", ""),
            away_team=decoded.get("away_team", ""),
        )

        try:
            await self.redis_client.xadd(
                self.output_stream,
                fields_out,  # type: ignore[arg-type]
                maxlen=self.output_maxlen,
                approximate=True,
            )
        except Exception as exc:
            self._log.warning(
                "daemon.emit_failed",
                fixture_id=fields_out.get("fixture_id", ""),
                market_id=fields_out.get("market_id", ""),
                error=str(exc),
            )
            return result.verdict

        # Log per-verdict at info level for live readability.
        self._log.info(
            "verifier.result",
            verdict=result.verdict.value,
            fixture_id=fields_out["fixture_id"],
            market_id=fields_out["market_id"],
            home_team=fields_out["home_team"],
            away_team=fields_out["away_team"],
            detection_margin_pct=result.detection_margin_pct,
            fresh_margin_pct=result.fresh_margin_pct,
            margin_delta_pct=result.margin_delta_pct,
            time_since_detection_sec=result.time_since_detection_sec,
        )
        return result.verdict
