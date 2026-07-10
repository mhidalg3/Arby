"""Cross-platform arbitrage detector.

Consumes raw odds snapshots from the `odds:raw` Redis stream,
canonicalizes them through the semantic layer, groups by canonical
`(fixture, market)`, and runs `dutch_book.detect_arbitrage` when a
group has full cell coverage. Detected opportunities are emitted to
the `arb:opportunities` stream.

Pipeline:

    odds:raw  ─ XREAD ─►  Canonicalizer.canonicalize
                              │
                              ▼
                         CanonicalQuote
                              │
                              ▼
                         per-(fixture, market) sliding state:
                              latest CanonicalQuote per (cell, platform)
                              │
                              ▼
                         best quote per cell  (highest decimal_odds,
                                                staleness-filtered)
                              │
                              ▼
                         if all EXPECTED_CELLS covered:
                              dutch_book.detect_arbitrage
                              │
                              ▼
                         emit_throttle gate (debounce per market_key)
                              │
                              ▼
    arb:opportunities ◄─ XADD

Design choices:

- **Best-odds-per-cell across platforms.** When multiple platforms
  quote the same cell, we pick the highest decimal_odds — that
  maximizes the arb margin, which is the whole point.
- **Staleness filter.** Quotes older than `staleness_threshold_sec`
  relative to the snapshot that triggered detection are excluded
  from the candidate set. Default 30s.
- **Emission throttle, with improvement override.** A given
  `(fixture_id, market_id)` emits at most once per `emit_throttle_sec`
  *unless* the new opportunity has a strictly higher
  `realized_roi_pct` than the last one we emitted — in which case
  we emit immediately. This suppresses noise from a persistent
  arb (same odds re-observed every poll cycle) while ensuring
  material improvements (e.g. a slower platform arrives with
  better odds and improves the margin) surface as soon as they
  happen. Default throttle 5s matches the scraper poll interval.
- **No partition_validator call for v1.** 1X2 cells are
  exhaustive and mutually exclusive by canonical-layer construction
  (`EXPECTED_CELLS[H2H_3WAY]` is the literal partition definition).
  When OU/AH ship — where market lines can overlap (Over 2.5 vs
  Under 3.5) — `partition_validator.validate` becomes load-bearing
  and slots in between cell-coverage check and dutch_book.
- **Stateless detector w.r.t. emission.** No dedup table beyond the
  throttle. Two consecutive detections with different odds DO emit
  separately — the consumer can dedup if it cares.
- **State is bounded.** A future cleanup pass should GC fixtures
  not seen for >`staleness_threshold_sec * 2`; for v1 the state
  grows linearly with active fixtures and shrinks naturally on
  process restart. Argentine soccer never has more than ~500
  active matches simultaneously, so memory pressure isn't a concern.

Not in scope for v1:
- Consumer groups / replay semantics — `XREAD $` tails the latest
  only; missed entries on detector restart are gone.
- Postgres opportunity persistence — emit to Redis stream only.
- Risk-layer gating — the detector emits everything that meets the
  margin threshold; the risk layer (separate process, future) is
  what decides whether to actually place a bet.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
from redis.asyncio import Redis

from src.arbitrage.dutch_book import ArbitrageOpportunity, detect_arbitrage
from src.arbitrage.quotes import OddsQuote
from src.ingestion.redis_sink import STREAM_NAME as INPUT_STREAM_NAME
from src.ingestion.redis_sink import stream_fields_to_snapshot
from src.semantic.canonical import (
    EXPECTED_CELLS,
    CanonicalMarketCode,
    CanonicalQuote,
)
from src.semantic.canonicalizer import Canonicalizer

log = structlog.get_logger(__name__)

OUTPUT_STREAM_NAME: Final[str] = "arb:opportunities"

# Approximate stream cap (mirrors odds:raw policy). 50k entries ×
# ~600 bytes/entry = ~30MB.
DEFAULT_OUTPUT_MAXLEN: Final[int] = 50_000

# Default detector tuning. Tune via constructor; defaults are
# conservative for the MVP.
#
# `DEFAULT_BUDGET` is the fallback used by `default_budget_fn` —
# applied when no per-opportunity `budget_fn` is injected. In
# production the daemon wires up a `StakeSizer.compute_budget`
# from `src.risk.stake_sizing`, which computes the budget from
# (total_capital, confidence) per opportunity.
DEFAULT_BUDGET: Final[float] = 1_000.0
DEFAULT_MIN_MARGIN_PCT: Final[float] = 1.0
DEFAULT_STALENESS_SEC: Final[float] = 30.0
DEFAULT_EMIT_THROTTLE_SEC: Final[float] = 5.0
DEFAULT_XREAD_BLOCK_MS: Final[int] = 1_000
DEFAULT_XREAD_COUNT: Final[int] = 500


def default_budget_fn(_quotes: Sequence[OddsQuote]) -> float:
    """Fallback budget function — returns the static `DEFAULT_BUDGET`.

    Used when no `budget_fn` is injected (e.g., test scenarios,
    one-off scripts). Production daemon wires up
    `StakeSizer.compute_budget` instead so budgets scale with
    total capital and per-opportunity confidence.
    """
    return DEFAULT_BUDGET


def opportunity_to_stream_fields(
    opp: ArbitrageOpportunity,
    fixture_id: str,
    market_id: str,
    home_team: str,
    away_team: str,
    detected_at: float | None = None,
) -> dict[str, str]:
    """Flatten an `ArbitrageOpportunity` into Redis-stream field-value entries.

    Sequence fields (legs + stakes) are JSON-encoded into a single
    `legs_json` field. The summary scalars (margin, ROI, profit, total
    stake, capital utilization) are top-level for easy filtering with
    `XREAD` + a downstream parser without needing JSON decode.
    """
    return {
        "fixture_id": fixture_id,
        "market_id": market_id,
        "home_team": home_team,
        "away_team": away_team,
        "margin_pct": f"{opp.margin_pct:.6f}",
        "realized_roi_pct": f"{opp.realized_roi_pct:.6f}",
        "guaranteed_profit": f"{opp.guaranteed_profit:.6f}",
        "total_stake": f"{opp.total_stake:.6f}",
        "capital_utilization": f"{opp.capital_utilization:.6f}",
        "legs_json": json.dumps(
            [
                {
                    "platform": leg.platform,
                    "outcome": leg.outcome,
                    "decimal_odds": leg.decimal_odds,
                    "stake": stake,
                    "max_stake": leg.max_stake,
                    "timestamp": leg.timestamp,
                    "platform_outcome_id": leg.platform_outcome_id,
                    "platform_event_id": leg.platform_event_id,
                }
                for leg, stake in zip(opp.legs, opp.stakes, strict=True)
            ]
        ),
        "detected_at": f"{(detected_at if detected_at is not None else time.time()):.6f}",
    }


@dataclass
class _MarketState:
    """Sliding state for one canonical (fixture_id, market_id).

    Stores the latest CanonicalQuote per (cell, platform) — best odds
    per cell are computed on demand from the latest map. Keeping
    per-platform avoids losing the second-best quote when the
    best-quote platform updates with worse odds.
    """

    market_code: CanonicalMarketCode
    home_team: str
    away_team: str
    # (cell, platform) → latest CanonicalQuote
    quotes_by_cell_platform: dict[tuple[str, str], CanonicalQuote] = field(default_factory=dict)


@dataclass
class ArbDetector:
    """Async loop: ingest snapshots, detect arbs, emit opportunities.

    Construct once per detector process. Call `run(stop_event)` from
    the main coroutine.
    """

    redis_client: Redis
    canonicalizer: Canonicalizer
    # `budget_fn(legs)` returns the budget (ARS) to allocate to this
    # opportunity. Injected by the daemon as `StakeSizer.compute_budget`
    # so budgets scale with total capital and per-opportunity
    # confidence. The static `budget` field below is the fallback for
    # tests + the legacy detector-with-fixed-budget path.
    budget_fn: Callable[[Sequence[OddsQuote]], float] = default_budget_fn
    budget: float = (
        DEFAULT_BUDGET  # retained for back-compat; ignored when budget_fn is the non-default
    )
    min_margin_pct: float = DEFAULT_MIN_MARGIN_PCT
    staleness_threshold_sec: float = DEFAULT_STALENESS_SEC
    emit_throttle_sec: float = DEFAULT_EMIT_THROTTLE_SEC
    input_stream: str = INPUT_STREAM_NAME
    output_stream: str = OUTPUT_STREAM_NAME
    output_maxlen: int = DEFAULT_OUTPUT_MAXLEN
    xread_block_ms: int = DEFAULT_XREAD_BLOCK_MS
    xread_count: int = DEFAULT_XREAD_COUNT

    # State
    _markets: dict[tuple[str, str], _MarketState] = field(default_factory=dict)
    _last_emit_at: dict[tuple[str, str], float] = field(default_factory=dict)
    _last_emit_roi: dict[tuple[str, str], float] = field(default_factory=dict)
    _log: structlog.BoundLogger = field(init=False)

    def __post_init__(self) -> None:
        self._log = log.bind(component="arb_detector")

    async def run(self, stop_event: asyncio.Event) -> None:
        """Tail `odds:raw` and detect arbs until `stop_event` is set.

        Starts at the live tail (`$`), so entries written before the
        detector started are NOT replayed. That's deliberate — the
        scrapers are continuously emitting, so the next 5s of fresh
        snapshots is what we want.
        """
        last_id: str = "$"
        self._log.info(
            "detector.started",
            input_stream=self.input_stream,
            output_stream=self.output_stream,
            budget=self.budget,
            min_margin_pct=self.min_margin_pct,
        )

        snapshots_seen = 0
        opportunities_emitted = 0
        while not stop_event.is_set():
            try:
                entries = await self.redis_client.xread(
                    {self.input_stream: last_id},
                    block=self.xread_block_ms,
                    count=self.xread_count,
                )
            except Exception as exc:
                self._log.exception("detector.xread_failed", error=str(exc))
                # Brief backoff so a sustained Redis outage doesn't
                # tight-loop. Respect stop_event during the wait.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                    break
                except TimeoutError:
                    continue
            if not entries:
                continue
            # `entries` shape: list of (stream_name, list of (entry_id, fields))
            for _stream_name, items in entries:
                for entry_id, fields in items:
                    last_id = entry_id if isinstance(entry_id, str) else entry_id.decode()
                    snapshots_seen += 1
                    emitted = await self._process_entry(fields)
                    if emitted:
                        opportunities_emitted += 1
            if snapshots_seen % 1000 == 0 and snapshots_seen:
                self._log.debug(
                    "detector.progress",
                    snapshots_seen=snapshots_seen,
                    opportunities_emitted=opportunities_emitted,
                )

        self._log.info(
            "detector.stopped",
            snapshots_seen=snapshots_seen,
            opportunities_emitted=opportunities_emitted,
        )

    async def _process_entry(self, fields: dict[str, Any]) -> bool:
        """Process one snapshot. Returns True iff an opportunity was emitted."""
        # The redis-py async client returns either bytes or str depending
        # on `decode_responses`. The daemon uses `decode_responses=True`;
        # we coerce defensively here for tests that pass bytes.
        decoded = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in fields.items()
        }
        snapshot = stream_fields_to_snapshot(decoded)
        if snapshot is None:
            self._log.warning("detector.malformed_entry", fields=decoded)
            return False

        quote = await self.canonicalizer.canonicalize(snapshot)
        if quote is None:
            return False

        market_key = (quote.fixture.fixture_id, quote.odds_quote.market_id)
        state = self._markets.get(market_key)
        if state is None:
            state = _MarketState(
                market_code=quote.outcome.market.code,
                home_team=quote.fixture.home_team,
                away_team=quote.fixture.away_team,
            )
            self._markets[market_key] = state

        state.quotes_by_cell_platform[(quote.outcome.cell, quote.odds_quote.platform)] = quote

        return await self._maybe_emit(market_key, state, quote.odds_quote.timestamp)

    async def _maybe_emit(
        self,
        market_key: tuple[str, str],
        state: _MarketState,
        now_ts: float,
    ) -> bool:
        expected = EXPECTED_CELLS.get(state.market_code)
        if expected is None:
            return False

        # Best (highest) decimal_odds per cell, after staleness filter.
        best_per_cell: dict[str, CanonicalQuote] = {}
        for (cell, _platform), q in state.quotes_by_cell_platform.items():
            if now_ts - q.odds_quote.timestamp > self.staleness_threshold_sec:
                continue
            current_best = best_per_cell.get(cell)
            if (
                current_best is None
                or q.odds_quote.decimal_odds > current_best.odds_quote.decimal_odds
            ):
                best_per_cell[cell] = q

        if set(best_per_cell.keys()) != expected:
            # Not fully covered yet.
            return False

        # Order legs by sorted cell name for deterministic emission.
        ordered_cells = sorted(expected)
        legs: tuple[OddsQuote, ...] = tuple(best_per_cell[c].odds_quote for c in ordered_cells)
        # Stake sizer decides the budget. Returns 0.0 to signal skip
        # (too-low confidence, or below the min-stake floor).
        budget = self.budget_fn(legs)
        if budget <= 0.0:
            return False
        try:
            opp = detect_arbitrage(legs, budget=budget, min_margin_pct=self.min_margin_pct)
        except ValueError as exc:
            self._log.warning(
                "detector.detect_arbitrage_error",
                market_key=market_key,
                error=str(exc),
            )
            return False
        if opp is None:
            return False

        # Throttle gate: within the throttle window, only emit if the
        # new opportunity is strictly better than the last emission.
        last_emit_at = self._last_emit_at.get(market_key, 0.0)
        last_emit_roi = self._last_emit_roi.get(market_key, float("-inf"))
        within_window = (now_ts - last_emit_at) < self.emit_throttle_sec
        if within_window and opp.realized_roi_pct <= last_emit_roi:
            return False

        await self._emit(market_key, state, opp, now_ts)
        self._last_emit_at[market_key] = now_ts
        self._last_emit_roi[market_key] = opp.realized_roi_pct
        return True

    async def _emit(
        self,
        market_key: tuple[str, str],
        state: _MarketState,
        opp: ArbitrageOpportunity,
        detected_at: float,
    ) -> None:
        fixture_id, market_id = market_key
        fields = opportunity_to_stream_fields(
            opp,
            fixture_id=fixture_id,
            market_id=market_id,
            home_team=state.home_team,
            away_team=state.away_team,
            detected_at=detected_at,
        )
        try:
            await self.redis_client.xadd(
                self.output_stream,
                fields,  # type: ignore[arg-type]
                maxlen=self.output_maxlen,
                approximate=True,
            )
        except Exception as exc:
            # Best-effort emission. A failed XADD does not regress
            # detector state; the next detection on the same market
            # will re-emit (subject to the throttle).
            self._log.warning(
                "detector.emit_failed",
                market_key=market_key,
                error=str(exc),
            )
            return
        self._log.info(
            "detector.opportunity",
            fixture_id=fixture_id,
            market_id=market_id,
            home_team=state.home_team,
            away_team=state.away_team,
            margin_pct=opp.margin_pct,
            realized_roi_pct=opp.realized_roi_pct,
            guaranteed_profit=opp.guaranteed_profit,
            total_stake=opp.total_stake,
            platforms=[leg.platform for leg in opp.legs],
        )
