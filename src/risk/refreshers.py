"""Tier-2 surgical quote refreshers — per-platform direct refetch.

Each refresher implements `QuoteRefresher` and calls the platform's
read API directly for a single leg's event. The
`MultiPlatformRefresher` dispatcher routes legs to the right
per-platform refresher and falls back to Tier-1 (stream-cache)
when:

  - The leg lacks `platform_event_id` or `platform_outcome_id`
    (legacy opportunity or non-Tier-2-enabled platform like Bplay SSE)
  - The per-platform refresher raises an exception
  - The per-platform refresher returns `decimal_odds=None` (market
    not found in fresh data) — Tier-1 confirms before declaring
    MARKET_UNAVAILABLE

Per-platform freshness characteristics:

| Platform              | API call           | Per-event? | Latency      |
|-----------------------|--------------------|------------|--------------|
| betsson-pba           | accordion/v1       | Yes        | ~200-500ms   |
| betwarrior-pba        | betoffer/event/.. | Yes        | ~300-500ms   |
| bplay-pba (XML)       | odds-competition  | No (scan)  | ~200ms × N   |
| bplay-pba (SSE)       | (push-only)        | -          | degrades T1  |

Bplay's two sources share `platform_name = "bplay-pba"`. The
Bplay XML refresher tries; if the leg's outcome isn't found in
any target competition feed, the refresher returns
`decimal_odds=None` and the dispatcher falls back to Tier-1.
That fallback catches the SSE-sourced legs without explicit branching.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import structlog

from src.arbitrage.quotes import OddsQuote
from src.ingestion.scrapers.base import RawOddsSnapshot
from src.ingestion.scrapers.betano import BetanoContractError, BetanoScraper
from src.ingestion.scrapers.betsson import BetssonContractError, BetssonScraper
from src.ingestion.scrapers.betwarrior import (
    BetWarriorContractError,
    BetWarriorPbaDepthScraper,
)
from src.ingestion.scrapers.bplay import BplayContractError, BplayPbaScraper
from src.risk.verifier import FreshQuote, StreamCacheRefresher

log = structlog.get_logger(__name__)


class QuoteRefresher(Protocol):
    """Per-platform Tier-2 refresher contract."""

    platform_name: str

    async def refresh(self, leg: OddsQuote) -> FreshQuote: ...


# -------- Per-platform refreshers --------


def _no_ids_sentinel(leg: OddsQuote) -> FreshQuote:
    """Return a tier=0 `FreshQuote` for legs without surgical IDs."""
    return FreshQuote(
        platform=leg.platform,
        platform_outcome_id=leg.platform_outcome_id or "",
        decimal_odds=None,
        observed_at=None,
        tier=0,
    )


def _not_found_quote(leg: OddsQuote) -> FreshQuote:
    """Tier-2 fetch succeeded but the outcome isn't in the fresh response."""
    return FreshQuote(
        platform=leg.platform,
        platform_outcome_id=leg.platform_outcome_id or "",
        decimal_odds=None,
        observed_at=time.time(),
        tier=2,
    )


@dataclass
class BetssonQuoteRefresher:
    """Calls Betsson's accordion endpoint directly per event."""

    scraper: BetssonScraper
    platform_name: str = "betsson-pba"

    async def refresh(self, leg: OddsQuote) -> FreshQuote:
        if not leg.platform_event_id or not leg.platform_outcome_id:
            return _no_ids_sentinel(leg)
        try:
            snapshots = await self.scraper.fetch_event_quotes(leg.platform_event_id)
        except BetssonContractError as exc:
            log.warning(
                "refresher.betsson.fetch_failed",
                event_id=leg.platform_event_id,
                error=str(exc),
            )
            raise
        for snap in snapshots:
            if snap.platform_outcome_id == leg.platform_outcome_id:
                return FreshQuote(
                    platform=snap.platform,
                    platform_outcome_id=snap.platform_outcome_id,
                    decimal_odds=snap.decimal_odds,
                    observed_at=snap.timestamp,
                    tier=2,
                )
        return _not_found_quote(leg)


@dataclass
class BetWarriorQuoteRefresher:
    """Calls BetWarrior's per-event betoffer endpoint directly.

    The depth scraper's `fetch_event_quotes` returns ALL v1 markets
    (1X2 + BTTS + OU goals) from one HTTP call — so a single
    refresher covers legs from both the list-view AND depth
    scrapers (they share `platform_name = "betwarrior-pba"`).
    """

    scraper: BetWarriorPbaDepthScraper
    platform_name: str = "betwarrior-pba"

    async def refresh(self, leg: OddsQuote) -> FreshQuote:
        if not leg.platform_event_id or not leg.platform_outcome_id:
            return _no_ids_sentinel(leg)
        try:
            snapshots = await self.scraper.fetch_event_quotes(leg.platform_event_id)
        except BetWarriorContractError as exc:
            log.warning(
                "refresher.betwarrior.fetch_failed",
                event_id=leg.platform_event_id,
                error=str(exc),
            )
            raise
        for snap in snapshots:
            if snap.platform_outcome_id == leg.platform_outcome_id:
                return FreshQuote(
                    platform=snap.platform,
                    platform_outcome_id=snap.platform_outcome_id,
                    decimal_odds=snap.decimal_odds,
                    observed_at=snap.timestamp,
                    tier=2,
                )
        return _not_found_quote(leg)


@dataclass
class BetanoQuoteRefresher:
    """Betano exposes no per-event read endpoint, so refresh re-scrapes the bulk feed
    (one ``top-events-v2`` / live call) and finds the leg's outcome — one HTTP
    round-trip, just larger than a per-event call. Matches the leg by
    ``platform_outcome_id`` (no event id needed)."""

    scraper: BetanoScraper
    platform_name: str = "betano"

    async def refresh(self, leg: OddsQuote) -> FreshQuote:
        if not leg.platform_outcome_id:
            return _no_ids_sentinel(leg)
        try:
            async for snap in self.scraper.fetch_live_soccer():
                if snap.platform_outcome_id == leg.platform_outcome_id:
                    return FreshQuote(
                        platform=snap.platform,
                        platform_outcome_id=snap.platform_outcome_id,
                        decimal_odds=snap.decimal_odds,
                        observed_at=snap.timestamp,
                        tier=2,
                    )
        except BetanoContractError as exc:
            log.warning("refresher.betano.fetch_failed", error=str(exc))
            raise
        return _not_found_quote(leg)


@dataclass
class BplayXMLQuoteRefresher:
    """Scans Bplay's target XML competition feeds in parallel for the
    leg's event. Each feed is ~30 KB; 5 in parallel costs ~150 KB
    per verification. Acceptable for the moderate verification rate.

    Returns `_not_found_quote` for legs sourced from the SSE feed
    (Argentine domestic in-play); the dispatcher falls back to
    Tier-1 for those.
    """

    scraper: BplayPbaScraper
    platform_name: str = "bplay-pba"

    async def refresh(self, leg: OddsQuote) -> FreshQuote:
        if not leg.platform_event_id or not leg.platform_outcome_id:
            return _no_ids_sentinel(leg)
        comp_ids = list(self.scraper._competitions.keys())
        if not comp_ids:
            return _not_found_quote(leg)
        # Fetch all competition feeds in parallel; first match wins.
        tasks = [self._fetch_silenced(cid) for cid in comp_ids]
        all_results = await asyncio.gather(*tasks)
        for snapshots in all_results:
            for snap in snapshots:
                if (
                    snap.platform_event_id == leg.platform_event_id
                    and snap.platform_outcome_id == leg.platform_outcome_id
                ):
                    return FreshQuote(
                        platform=snap.platform,
                        platform_outcome_id=snap.platform_outcome_id,
                        decimal_odds=snap.decimal_odds,
                        observed_at=snap.timestamp,
                        tier=2,
                    )
        # Outcome not in any XML feed — likely SSE-sourced, or market gone.
        return _not_found_quote(leg)

    async def _fetch_silenced(self, comp_id: int) -> list[RawOddsSnapshot]:
        """Catch per-competition errors and return [] — one bad feed
        shouldn't fail the whole verification."""
        try:
            return await self.scraper.fetch_competition_quotes(comp_id)
        except BplayContractError as exc:
            log.warning(
                "refresher.bplay.fetch_failed",
                competition_id=comp_id,
                error=str(exc),
            )
            return []


# -------- Multi-platform dispatcher --------


@dataclass
class MultiPlatformRefresher:
    """Dispatches legs to per-platform Tier-2 refreshers with
    Tier-1 fallback.

    Construct with a `dict[platform_name, QuoteRefresher]` and a
    `StreamCacheRefresher` for fallback. The verifier calls
    `refresh_batch(legs)`; the dispatcher runs Tier-2 refreshers in
    parallel for the legs that have surgical IDs + a configured
    refresher, then batches the remainder through Tier-1.
    """

    per_platform: dict[str, QuoteRefresher]
    tier_1_fallback: StreamCacheRefresher

    async def refresh(self, leg: OddsQuote) -> FreshQuote:
        """Single-leg refresh. Convenience for tests; production
        uses `refresh_batch`."""
        results = await self.refresh_batch([leg])
        return results[0]

    async def refresh_batch(
        self, legs: Sequence[OddsQuote]
    ) -> list[FreshQuote]:
        """Two-phase: Tier-2 in parallel for eligible legs, then
        Tier-1 batched fallback for the rest.
        """
        n = len(legs)
        results: list[FreshQuote | None] = [None] * n

        # Phase 1: Tier-2 dispatch
        tier_2_indices: list[int] = []
        tier_2_tasks = []
        for i, leg in enumerate(legs):
            refresher = self.per_platform.get(leg.platform)
            if refresher is None:
                continue
            if not leg.platform_event_id or not leg.platform_outcome_id:
                continue
            tier_2_indices.append(i)
            tier_2_tasks.append(refresher.refresh(leg))

        if tier_2_tasks:
            tier_2_results: list[FreshQuote | BaseException] = await asyncio.gather(
                *tier_2_tasks, return_exceptions=True
            )
            for idx, result in zip(tier_2_indices, tier_2_results, strict=True):
                if isinstance(result, BaseException):
                    # Tier-2 errored → leave None so Tier-1 fallback fires.
                    log.warning(
                        "refresher.tier_2_exception",
                        platform=legs[idx].platform,
                        event_id=legs[idx].platform_event_id,
                        error=str(result),
                    )
                    continue
                if result.decimal_odds is None:
                    # Tier-2 ran but didn't find the outcome.
                    # For SSE-sourced legs on Bplay this is expected;
                    # let Tier-1 confirm before declaring market gone.
                    continue
                results[idx] = result

        # Phase 2: Tier-1 fallback for legs not yet resolved
        fallback_indices = [i for i, r in enumerate(results) if r is None]
        if fallback_indices:
            fallback_legs = [legs[i] for i in fallback_indices]
            tier_1_results = await self.tier_1_fallback.refresh_batch(fallback_legs)
            for idx, result in zip(fallback_indices, tier_1_results, strict=True):
                results[idx] = result

        # All filled by Phase 2 fallback; cast Nones away.
        out: list[FreshQuote] = []
        for r in results:
            assert r is not None  # Phase 2 fills every remaining slot
            out.append(r)
        return out
