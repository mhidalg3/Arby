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

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from src.arbitrage.quotes import OddsQuote
from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.canonical import EXPECTED_CELLS, CanonicalQuote
from src.semantic.team_normalize import team_similarity

log = structlog.get_logger(__name__)


class LiveScraper(Protocol):
    def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]: ...


class LinkerScraper(Protocol):
    """A scraper that can list fixtures cheaply (no odds) and fetch one event's
    odds on demand — so we fetch odds only for fixtures the anchor also covers."""

    async def list_fixture_refs(self) -> list[tuple[str, str]]: ...  # (event_id, slug)
    # slug seeds raw_event_name → the fixture link needs it (both-team match).
    async def fetch_event_quotes(self, event_id: str, slug: str = "") -> list[RawOddsSnapshot]: ...


class QuoteCanonicalizer(Protocol):
    async def canonicalize(self, snapshot: RawOddsSnapshot) -> CanonicalQuote | None: ...


def assemble_partitions(
    by_market: dict[str, list[CanonicalQuote]], now: float, staleness_sec: float
) -> dict[str, list[OddsQuote]]:
    """Per market: best (highest) odds per cell across platforms (fresh quotes
    only); keep a market only when its partition is COMPLETE. The completeness
    gate is a correctness guard — a partial would let detect_arbitrage 'find' a
    book that loses on the uncovered outcome."""
    out: dict[str, list[OddsQuote]] = {}
    for market_id, cqs in by_market.items():
        expected = EXPECTED_CELLS.get(cqs[0].outcome.market.code)
        if expected is None:
            continue
        best: dict[str, CanonicalQuote] = {}
        for cq in cqs:
            if now - cq.odds_quote.timestamp > staleness_sec:
                continue
            cell = cq.outcome.cell
            current = best.get(cell)
            if current is None or cq.odds_quote.decimal_odds > current.odds_quote.decimal_odds:
                best[cell] = cq
        if set(best.keys()) == expected:
            out[market_id] = [best[cell].odds_quote for cell in sorted(expected)]
    return out


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

        return assemble_partitions(by_market, self.now_fn(), self.staleness_sec)


@dataclass
class OverlapQuoteSource:
    """Latency + anti-bot optimized multi-platform source.

    Each `bulk_sources` scraper (Betano, BetWarrior — Kambi) returns all its
    fixtures+odds in one cheap call and REGISTERS fixtures (they're anchors: a
    parseable ``home``/``away``, so a match both bulk books cover links directly).
    Then for each `linkers` scraper (Betsson — per-event, ~200 calls if fetched
    whole) we fetch odds ONLY for fixtures a bulk source also covers, matched by
    the bulk fixtures' canonical ``"{home} {away}"`` against the linker's cheap
    fixture-list slugs. An arb needs ≥2 books, so the overlap is all we need —
    far fewer repeated requests (the real anti-bot risk for continuous polling).
    Canonicalization re-validates every quote, so a loose name-match only
    wastes/drops a fetch."""

    bulk_sources: Sequence[LiveScraper]
    linkers: Sequence[LinkerScraper]
    canonicalizer: QuoteCanonicalizer
    match_threshold: float = 0.80
    staleness_sec: float = 45.0
    now_fn: Callable[[], float] = field(default=time.time)
    # Per-event linker fetches run concurrently up to this many. Kept modest so the
    # per-cycle burst doesn't look like a scraper to the linker's WAF (Betsson 403s +
    # circuit-breaks under a large fast burst — the multi-book overlap can be 100+).
    max_concurrent_linker_fetches: int = 4
    # Hard cap on linker (Betsson) per-event fetches PER CYCLE. With several bulk books
    # the raw overlap balloons (200+ bulk fixtures ⇒ 130+ matched Betsson events); a
    # burst that size trips Betsson's WAF. Cap it so the footprint stays sustainable.
    max_linker_events: int = 50

    async def fetch(self) -> dict[str, list[OddsQuote]]:
        by_market: dict[str, list[CanonicalQuote]] = defaultdict(list)
        targets: set[str] = set()  # canonical "home away" of every bulk fixture

        # 1) Bulk sources: full fetch + canonicalize (registers/links fixtures). Build
        #    overlap targets from the CANONICAL fixture names — separator-agnostic
        #    (Betano " vs " vs BetWarrior " - ") and already reserve-base-normalized.
        for source in self.bulk_sources:
            try:
                async for snap in source.fetch_live_soccer():
                    cq = await self.canonicalizer.canonicalize(snap)
                    if cq is not None:
                        by_market[cq.odds_quote.market_id].append(cq)
                        targets.add(f"{cq.fixture.home_team} {cq.fixture.away_team}".strip())
            except Exception as exc:  # noqa: BLE001 — one source must not sink the cycle
                log.warning("quote_source.bulk_error", error=str(exc))
        if not targets:
            return {}  # no bulk data → nothing to link against

        # 2) Each linker: list fixtures (one cheap call), fetch odds ONLY for events a
        #    bulk source also covers. The slug MUST be passed — it seeds raw_event_name,
        #    which the fixture resolver parses for both team names to link the event.
        overlap_events = 0
        for linker in self.linkers:
            try:
                refs = await linker.list_fixture_refs()
            except Exception as exc:  # noqa: BLE001
                log.warning("quote_source.linker_list_error", error=str(exc))
                continue
            overlap = [
                (event_id, slug)
                for event_id, slug in refs
                if any(
                    team_similarity(t, slug.rsplit("/", 1)[-1].replace("-", " "))
                    >= self.match_threshold
                    for t in targets
                )
            ]
            if len(overlap) > self.max_linker_events:
                log.warning(
                    "quote_source.linker_overlap_capped",
                    matched=len(overlap),
                    cap=self.max_linker_events,
                )
                overlap = overlap[: self.max_linker_events]
            overlap_events += len(overlap)
            # Fetch the matched events concurrently (bounded) — sequential here is what
            # blew the staleness window once the overlap set grew with multi-book bulk.
            sem = asyncio.Semaphore(self.max_concurrent_linker_fetches)

            async def _fetch(
                event_id: str,
                slug: str,
                _linker: LinkerScraper = linker,
                _sem: asyncio.Semaphore = sem,
            ) -> list[RawOddsSnapshot]:
                async with _sem:
                    try:
                        return await _linker.fetch_event_quotes(event_id, slug)
                    except Exception as exc:  # noqa: BLE001 — one bad event mustn't sink the cycle
                        log.warning(
                            "quote_source.linker_event_error", event_id=event_id, error=str(exc)
                        )
                        return []

            for snaps in await asyncio.gather(*(_fetch(eid, slug) for eid, slug in overlap)):
                for snap in snaps:
                    cq = await self.canonicalizer.canonicalize(snap)
                    if cq is not None:
                        by_market[cq.odds_quote.market_id].append(cq)

        log.info(
            "overlap_quote_source.fetched", bulk_fixtures=len(targets), overlap_events=overlap_events
        )
        return assemble_partitions(by_market, self.now_fn(), self.staleness_sec)
