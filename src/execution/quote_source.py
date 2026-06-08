"""Live `QuoteSource`: scrapers → canonicalize → assemble complete partitions.

The orchestrator's `QuoteSource` seam, made real. Each scraper's raw snapshots
are canonicalized (cross-platform fixture/market/outcome alignment — the hard
part, owned by `src/semantic`); we group the resulting quotes by canonical
`market_id`, keep the **best decimal odds per cell** across platforms, and emit a
market only when its partition is **complete** (every `EXPECTED_CELLS` cell
covered by a fresh quote).

The completeness gate is a correctness guard, not an optimization: a market with
only 2 of a 1X2's 3 cells would let `detect_arbitrage` "find" a Dutch book that
loses entirely on the uncovered outcome. Incomplete or stale partitions are
dropped here so the detector only ever sees valid ones.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from src.arbitrage.quotes import OddsQuote
from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.canonical import EXPECTED_CELLS, CanonicalQuote

log = structlog.get_logger(__name__)


class LiveScraper(Protocol):
    def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]: ...


class QuoteCanonicalizer(Protocol):
    async def canonicalize(self, snapshot: RawOddsSnapshot) -> CanonicalQuote | None: ...


@dataclass
class CanonicalizingQuoteSource:
    """Poll-friendly `QuoteSource`: one `fetch()` scrapes all platforms once,
    canonicalizes, and returns the complete partitions ready for the detector."""

    scrapers: Sequence[LiveScraper]
    canonicalizer: QuoteCanonicalizer
    staleness_sec: float = 30.0
    now_fn: Callable[[], float] = field(default=time.time)

    async def fetch(self) -> dict[str, list[OddsQuote]]:
        by_market: dict[str, list[CanonicalQuote]] = defaultdict(list)
        for scraper in self.scrapers:
            try:
                async for snap in scraper.fetch_live_soccer():
                    cq = await self.canonicalizer.canonicalize(snap)
                    if cq is not None:
                        by_market[cq.odds_quote.market_id].append(cq)
            except Exception as exc:  # noqa: BLE001 — one scraper must not sink the cycle
                log.warning("quote_source.scraper_error", error=str(exc))

        now = self.now_fn()
        out: dict[str, list[OddsQuote]] = {}
        for market_id, cqs in by_market.items():
            partition = self._complete_partition(cqs, now)
            if partition is not None:
                out[market_id] = partition
        return out

    def _complete_partition(self, cqs: list[CanonicalQuote], now: float) -> list[OddsQuote] | None:
        """Best (highest) odds per cell across platforms, fresh quotes only;
        return one quote per cell iff the partition is complete, else None."""
        expected = EXPECTED_CELLS.get(cqs[0].outcome.market.code)
        if expected is None:
            return None
        best: dict[str, CanonicalQuote] = {}
        for cq in cqs:
            if now - cq.odds_quote.timestamp > self.staleness_sec:
                continue
            cell = cq.outcome.cell
            current = best.get(cell)
            if current is None or cq.odds_quote.decimal_odds > current.odds_quote.decimal_odds:
                best[cell] = cq
        if set(best.keys()) != expected:
            return None  # incomplete partition — never let the detector see it
        return [best[cell].odds_quote for cell in sorted(expected)]
