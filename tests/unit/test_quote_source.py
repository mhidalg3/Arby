"""Tests for CanonicalizingQuoteSource: partition assembly, best-per-cell, gates."""

from __future__ import annotations

from collections.abc import AsyncIterator

from src.arbitrage.quotes import OddsQuote
from src.execution.quote_source import CanonicalizingQuoteSource
from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.canonical import (
    CELL_AWAY,
    CELL_DRAW,
    CELL_HOME,
    CanonicalFixture,
    CanonicalMarket,
    CanonicalMarketCode,
    CanonicalOutcome,
    CanonicalQuote,
)
from src.semantic.canonicalizer import canonical_market_id

_MARKET = CanonicalMarket(code=CanonicalMarketCode.H2H_3WAY)
_MKT_ID = canonical_market_id("FIX1", _MARKET)


def _snap(platform: str, cell: str, odds: float, ts: float = 0.0) -> RawOddsSnapshot:
    return RawOddsSnapshot(
        platform=platform,
        platform_event_id=f"{platform}-evt",
        platform_market_id=f"{platform}-mkt",
        platform_outcome_id=f"{platform}-{cell}",
        raw_event_name="Home vs Away",
        raw_market_name="1X2",
        raw_outcome_name=cell,
        decimal_odds=odds,
        max_stake=5000.0,
        timestamp=ts,
    )


def _cq(snap: RawOddsSnapshot, cell: str) -> CanonicalQuote:
    return CanonicalQuote(
        fixture=CanonicalFixture(fixture_id="FIX1", home_team="home", away_team="away"),
        outcome=CanonicalOutcome(market=_MARKET, cell=cell),
        odds_quote=OddsQuote(
            platform=snap.platform,
            market_id=_MKT_ID,
            outcome=cell,
            decimal_odds=snap.decimal_odds,
            max_stake=snap.max_stake,
            timestamp=snap.timestamp,
            platform_outcome_id=snap.platform_outcome_id,
            platform_event_id=snap.platform_event_id,
        ),
    )


class _FakeScraper:
    def __init__(self, snaps: list[RawOddsSnapshot]) -> None:
        self._snaps = snaps

    async def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]:
        for s in self._snaps:
            yield s


class _StubCanonicalizer:
    """Maps a snapshot to a canned CanonicalQuote by its outcome label."""

    async def canonicalize(self, snapshot: RawOddsSnapshot) -> CanonicalQuote | None:
        cell = snapshot.raw_outcome_name
        if cell not in {CELL_HOME, CELL_DRAW, CELL_AWAY}:
            return None
        return _cq(snapshot, cell)


def _source(*scrapers: _FakeScraper, now: float = 0.0) -> CanonicalizingQuoteSource:
    return CanonicalizingQuoteSource(
        scrapers=list(scrapers),
        canonicalizer=_StubCanonicalizer(),
        staleness_sec=30.0,
        now_fn=lambda: now,
    )


async def test_complete_partition_takes_best_odds_per_cell() -> None:
    # Betsson and Betano both quote all three cells; best (highest) odds per cell wins.
    betsson = _FakeScraper(
        [
            _snap("betsson", CELL_HOME, 2.0),
            _snap("betsson", CELL_DRAW, 3.5),
            _snap("betsson", CELL_AWAY, 4.0),
        ]
    )
    betano = _FakeScraper(
        [
            _snap("betano", CELL_HOME, 2.1),
            _snap("betano", CELL_DRAW, 3.3),
            _snap("betano", CELL_AWAY, 4.2),
        ]
    )
    out = await _source(betsson, betano).fetch()
    assert set(out.keys()) == {_MKT_ID}
    legs = {q.outcome: q for q in out[_MKT_ID]}
    assert legs[CELL_HOME].platform == "betano" and legs[CELL_HOME].decimal_odds == 2.1
    assert legs[CELL_DRAW].platform == "betsson" and legs[CELL_DRAW].decimal_odds == 3.5
    assert legs[CELL_AWAY].platform == "betano" and legs[CELL_AWAY].decimal_odds == 4.2


async def test_incomplete_partition_is_dropped() -> None:
    # Only HOME and AWAY present (no DRAW) → not a valid 1X2 partition → dropped.
    betsson = _FakeScraper([_snap("betsson", CELL_HOME, 2.0), _snap("betsson", CELL_AWAY, 4.0)])
    betano = _FakeScraper([_snap("betano", CELL_AWAY, 4.2)])
    out = await _source(betsson, betano).fetch()
    assert out == {}  # never hand the detector a partial partition


async def test_stale_cell_makes_partition_incomplete() -> None:
    # DRAW quote is stale (ts far in the past) → filtered → partition incomplete → dropped.
    betsson = _FakeScraper(
        [
            _snap("betsson", CELL_HOME, 2.0, ts=100.0),
            _snap("betsson", CELL_DRAW, 3.5, ts=0.0),  # stale
            _snap("betsson", CELL_AWAY, 4.0, ts=100.0),
        ]
    )
    out = await _source(betsson, now=100.0).fetch()  # staleness 30s; draw is 100s old
    assert out == {}


async def test_one_scraper_failing_does_not_sink_the_cycle() -> None:
    class _Boom:
        async def fetch_live_soccer(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("network down")
            yield  # unreachable; makes it an async generator

    good = _FakeScraper(
        [
            _snap("betsson", CELL_HOME, 2.0),
            _snap("betsson", CELL_DRAW, 3.5),
            _snap("betsson", CELL_AWAY, 4.0),
        ]
    )
    src = CanonicalizingQuoteSource(
        scrapers=[_Boom(), good], canonicalizer=_StubCanonicalizer(), now_fn=lambda: 0.0
    )
    out = await src.fetch()
    assert set(out.keys()) == {_MKT_ID}  # the good scraper still produced a partition
