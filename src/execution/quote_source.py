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
from src.semantic.team_normalize import normalize_team_name, team_similarity

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
    """Latency + anti-bot optimized two-platform source.

    The `anchor` (Betano) returns all its fixtures+odds in one bulk call and
    registers fixtures. We then fetch the `linker`'s (Betsson) odds ONLY for
    fixtures the anchor also covers — found by matching the anchor's
    ``"{home} {away}"`` against the linker's cheap fixture-list slugs. An arb
    needs both books, so the overlap is all we need; this cuts the linker from
    ~200 per-event calls to a handful — much faster AND far fewer repeated
    requests (the real anti-bot risk for continuous polling). Canonicalization
    re-validates every quote, so a loose name-match only wastes/drops a fetch."""

    anchor: LiveScraper
    linker: LinkerScraper
    canonicalizer: QuoteCanonicalizer
    match_threshold: float = 0.80
    staleness_sec: float = 45.0
    now_fn: Callable[[], float] = field(default=time.time)

    async def fetch(self) -> dict[str, list[OddsQuote]]:
        by_market: dict[str, list[CanonicalQuote]] = defaultdict(list)
        targets: set[str] = set()  # normalized "home away" per anchor fixture

        # 1) Anchor: bulk fetch + canonicalize (registers fixtures); collect names.
        try:
            async for snap in self.anchor.fetch_live_soccer():
                if " vs " in snap.raw_event_name:
                    home, away = snap.raw_event_name.split(" vs ", 1)
                    label = f"{normalize_team_name(home)} {normalize_team_name(away)}".strip()
                    if label:
                        targets.add(label)
                cq = await self.canonicalizer.canonicalize(snap)
                if cq is not None:
                    by_market[cq.odds_quote.market_id].append(cq)
        except Exception as exc:  # noqa: BLE001 — no anchor → nothing to link against
            log.warning("quote_source.anchor_error", error=str(exc))
            return {}

        # 2) Linker fixture list (one cheap call) → the events the anchor also has.
        overlap: list[tuple[str, str]] = []  # (event_id, slug)
        try:
            for event_id, slug in await self.linker.list_fixture_refs():
                slug_teams = slug.rsplit("/", 1)[-1].replace("-", " ")
                if any(team_similarity(t, slug_teams) >= self.match_threshold for t in targets):
                    overlap.append((event_id, slug))
        except Exception as exc:  # noqa: BLE001
            log.warning("quote_source.linker_list_error", error=str(exc))

        # 3) Fetch linker odds ONLY for the overlap events. The slug MUST be passed:
        # it seeds raw_event_name, which the fixture resolver parses for both team
        # names to link the event (without it nothing links → no cross-platform market).
        for event_id, slug in overlap:
            try:
                for snap in await self.linker.fetch_event_quotes(event_id, slug):
                    cq = await self.canonicalizer.canonicalize(snap)
                    if cq is not None:
                        by_market[cq.odds_quote.market_id].append(cq)
            except Exception as exc:  # noqa: BLE001 — one bad event mustn't sink the cycle
                log.warning("quote_source.linker_event_error", event_id=event_id, error=str(exc))

        log.info(
            "overlap_quote_source.fetched", anchor_fixtures=len(targets), overlap_events=len(overlap)
        )
        return assemble_partitions(by_market, self.now_fn(), self.staleness_sec)
