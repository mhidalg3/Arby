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
    # Derive fixture teams from the event name so OverlapQuoteSource's target-building
    # (now off the canonical fixture) reflects the snapshot ("Panama vs Dominicana").
    if " vs " in snap.raw_event_name:
        home, away = (p.strip().lower() for p in snap.raw_event_name.split(" vs ", 1))
    else:
        home, away = "home", "away"
    return CanonicalQuote(
        fixture=CanonicalFixture(fixture_id="FIX1", home_team=home, away_team=away),
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


# ---- OverlapQuoteSource: fetch linker odds only for anchor-covered fixtures ----

from src.execution.quote_source import OverlapQuoteSource  # noqa: E402


class _FakeLinker:
    """list_fixture_refs() lists many fixtures; fetch_event_quotes only for those
    actually requested (records which)."""

    def __init__(self, refs: list[tuple[str, str]], snaps_by_event: dict[str, list[RawOddsSnapshot]]):
        self._refs = refs
        self._snaps = snaps_by_event
        self.fetched: list[str] = []
        self.fetched_slugs: list[str] = []

    async def list_fixture_refs(self) -> list[tuple[str, str]]:
        return self._refs

    async def fetch_event_quotes(self, event_id: str, slug: str = "") -> list[RawOddsSnapshot]:
        self.fetched.append(event_id)
        self.fetched_slugs.append(slug)
        return self._snaps.get(event_id, [])


def _bsnap(cell: str, odds: float) -> RawOddsSnapshot:
    return _snap("betsson-pba", cell, odds)


async def test_overlap_source_fetches_only_matched_linker_events() -> None:
    # Anchor (Betano) covers one match: "Panama vs Dominicana", all 3 cells.
    def _ev(name: str, cell: str, odds: float) -> RawOddsSnapshot:
        return RawOddsSnapshot(
            platform="betano", platform_event_id="b-evt", platform_market_id="b-mkt",
            platform_outcome_id=f"betano-{cell}", raw_event_name=name, raw_market_name="1X2",
            raw_outcome_name=cell, decimal_odds=odds, max_stake=5000.0, timestamp=0.0,
        )

    anchor = _FakeScraper([_ev("Panama vs Dominicana", CELL_HOME, 2.0),
                           _ev("Panama vs Dominicana", CELL_DRAW, 3.5),
                           _ev("Panama vs Dominicana", CELL_AWAY, 4.0)])
    # Linker lists a MATCHING fixture + an unrelated one; only the match's odds exist.
    linker = _FakeLinker(
        refs=[("ev-match", "futbol/x/panama-dominicana"),
              ("ev-other", "futbol/x/some-other-teams")],
        snaps_by_event={"ev-match": [_bsnap(CELL_HOME, 2.1), _bsnap(CELL_DRAW, 3.4),
                                     _bsnap(CELL_AWAY, 4.2)]},
    )
    src = OverlapQuoteSource(
        bulk_sources=[anchor], linkers=[linker], canonicalizer=_StubCanonicalizer(), now_fn=lambda: 0.0
    )
    out = await src.fetch()
    assert linker.fetched == ["ev-match"]  # only the anchor-covered fixture was fetched
    assert set(out.keys()) == {_MKT_ID}
    # cross-platform partition assembled, best odds per cell
    legs = {q.outcome: q.platform for q in out[_MKT_ID]}
    assert legs[CELL_HOME] == "betsson-pba"  # 2.1 > 2.0
    assert legs[CELL_DRAW] == "betano"  # 3.5 > 3.4


async def test_overlap_source_multiple_bulk_sources_plus_linker_span_one_partition() -> None:
    """Two bulk anchors (Betano + BetWarrior) AND a linker (Betsson) all feed one
    partition; best-per-cell can span all three books — the multi-platform path."""
    def _ev(platform: str, name: str, cell: str, odds: float) -> RawOddsSnapshot:
        return RawOddsSnapshot(
            platform=platform, platform_event_id=f"{platform}-e", platform_market_id="m",
            platform_outcome_id=f"{platform}-{cell}", raw_event_name=name, raw_market_name="1X2",
            raw_outcome_name=cell, decimal_odds=odds, max_stake=5000.0, timestamp=0.0,
        )

    betano = _FakeScraper([_ev("betano", "Panama vs Dominicana", CELL_HOME, 2.5)])  # best HOME
    betwarrior = _FakeScraper([_ev("betwarrior-pba", "Panama vs Dominicana", CELL_DRAW, 3.9)])  # DRAW
    linker = _FakeLinker(
        refs=[("ev", "futbol/x/panama-dominicana")],
        snaps_by_event={"ev": [_ev("betsson-pba", "Home vs Away", CELL_AWAY, 4.5)]},  # best AWAY
    )
    src = OverlapQuoteSource(
        bulk_sources=[betano, betwarrior], linkers=[linker],
        canonicalizer=_StubCanonicalizer(), now_fn=lambda: 0.0,
    )
    out = await src.fetch()
    assert len(out) == 1
    (quotes,) = out.values()
    assert {q.platform for q in quotes} == {"betano", "betwarrior-pba", "betsson-pba"}


async def test_overlap_source_reports_per_platform_staleness() -> None:
    """Per-book ingestion liveness: a bulk book that goes dark (yields nothing) ages
    past the threshold and is reported by `stale_platforms`, while a linker whose
    fixture-list call still succeeds stays fresh — the single-book-down signal the
    post-JOIN market count can't surface."""
    clock = {"t": 0.0}
    betano = _FakeScraper([_snap("betano", CELL_HOME, 2.0)])
    # A second bulk book stays alive, so targets is non-empty and the linker loop runs
    # (when ALL bulk dies the source bails before linking — covered by the aggregate alert).
    betwarrior = _FakeScraper([_snap("betwarrior-pba", CELL_HOME, 2.0)])
    linker = _FakeLinker(refs=[("ev", "futbol/x/home-away")], snaps_by_event={})
    linker.platform_name = "betsson-pba"  # so the linker's liveness is tracked too
    src = OverlapQuoteSource(
        bulk_sources=[betano, betwarrior], linkers=[linker],
        canonicalizer=_StubCanonicalizer(), now_fn=lambda: clock["t"],
    )
    await src.fetch()  # t=0: both bulk produced data; linker listed refs
    assert src.stale_platforms(10.0) == {}

    betano._snaps = []  # betano goes dark (block); betwarrior + linker still live
    clock["t"] = 100.0
    await src.fetch()
    stale = src.stale_platforms(10.0)
    assert set(stale) == {"betano"}  # betano stale; betwarrior + betsson-pba refreshed at t=100
    assert stale["betano"] == 100.0


async def test_overlap_source_threads_slug_so_betsson_links_under_real_canonicalizer() -> None:
    """Regression: the linker's slug seeds raw_event_name, which the REAL fixture
    resolver parses for both team names to link a Betsson event. If the slug isn't
    threaded (the Tier-2-verifier default of ""), the Betsson legs drop and NO
    cross-platform market forms — the bug that silently zeroed cross-platform
    overlap in the live loop. Use the real Canonicalizer so the empty-name drop
    path is actually exercised (a stub canonicalizer hides it)."""
    from src.semantic.canonicalizer import Canonicalizer
    from src.semantic.fixture_resolver import FixtureResolver

    def _ev(name: str, market: str, outcome: str, odds: float) -> RawOddsSnapshot:
        return RawOddsSnapshot(
            platform="bplay-pba", platform_event_id="b-evt", platform_market_id="b-mkt",
            platform_outcome_id=f"bp-{outcome}", raw_event_name=name, raw_market_name=market,
            raw_outcome_name=outcome, decimal_odds=odds, max_stake=5000.0, timestamp=0.0,
        )

    # Anchor (Bplay) registers the fixture via "{home} vs {away}".
    anchor = _FakeScraper([
        _ev("Boca Juniors vs River Plate", "1-X-2", "Boca Juniors", 2.0),
        _ev("Boca Juniors vs River Plate", "1-X-2", "Empate", 3.5),
        _ev("Boca Juniors vs River Plate", "1-X-2", "River Plate", 4.0),
    ])

    # Linker (Betsson) builds raw_event_name FROM THE SLUG it's handed — exactly like
    # the real scraper (empty slug ⇒ empty name ⇒ no link). Markets/outcomes resolvable.
    def _bets(slug: str, outcome: str, odds: float) -> RawOddsSnapshot:
        name = slug.rsplit("/", 1)[-1].replace("-", " ")  # mirrors _event_name_from_slug
        return RawOddsSnapshot(
            platform="betsson-pba", platform_event_id="ev-match", platform_market_id="m",
            platform_outcome_id=f"bs-{outcome}", raw_event_name=name,
            raw_market_name="Ganador del partido", raw_outcome_name=outcome,
            decimal_odds=odds, max_stake=5000.0, timestamp=0.0,
        )

    slug = "futbol/argentina/lpf/boca-juniors-river-plate"
    linker = _FakeLinker(
        refs=[("ev-match", slug)],
        snaps_by_event={"ev-match": [_bets(slug, "Boca Juniors", 2.1),
                                     _bets(slug, "Empate", 3.4),
                                     _bets(slug, "River Plate", 4.2)]},
    )
    src = OverlapQuoteSource(
        bulk_sources=[anchor], linkers=[linker],
        canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()), now_fn=lambda: 0.0,
    )
    out = await src.fetch()
    assert linker.fetched_slugs == [slug]  # the slug was threaded, not dropped
    assert len(out) == 1  # one cross-platform 1X2 market formed
    (quotes,) = out.values()
    assert {q.platform for q in quotes} == {"bplay-pba", "betsson-pba"}  # both books linked
