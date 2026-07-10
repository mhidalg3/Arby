"""Tests for CanonicalizingQuoteSource: partition assembly, best-per-cell, gates."""

from __future__ import annotations

import asyncio
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

    def __init__(
        self, refs: list[tuple[str, str]], snaps_by_event: dict[str, list[RawOddsSnapshot]]
    ):
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
            platform="betano",
            platform_event_id="b-evt",
            platform_market_id="b-mkt",
            platform_outcome_id=f"betano-{cell}",
            raw_event_name=name,
            raw_market_name="1X2",
            raw_outcome_name=cell,
            decimal_odds=odds,
            max_stake=5000.0,
            timestamp=0.0,
        )

    anchor = _FakeScraper(
        [
            _ev("Panama vs Dominicana", CELL_HOME, 2.0),
            _ev("Panama vs Dominicana", CELL_DRAW, 3.5),
            _ev("Panama vs Dominicana", CELL_AWAY, 4.0),
        ]
    )
    # Linker lists a MATCHING fixture + an unrelated one; only the match's odds exist.
    linker = _FakeLinker(
        refs=[
            ("ev-match", "futbol/x/panama-dominicana"),
            ("ev-other", "futbol/x/some-other-teams"),
        ],
        snaps_by_event={
            "ev-match": [_bsnap(CELL_HOME, 2.1), _bsnap(CELL_DRAW, 3.4), _bsnap(CELL_AWAY, 4.2)]
        },
    )
    src = OverlapQuoteSource(
        bulk_sources=[anchor],
        linkers=[linker],
        canonicalizer=_StubCanonicalizer(),
        now_fn=lambda: 0.0,
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
            platform=platform,
            platform_event_id=f"{platform}-e",
            platform_market_id="m",
            platform_outcome_id=f"{platform}-{cell}",
            raw_event_name=name,
            raw_market_name="1X2",
            raw_outcome_name=cell,
            decimal_odds=odds,
            max_stake=5000.0,
            timestamp=0.0,
        )

    betano = _FakeScraper([_ev("betano", "Panama vs Dominicana", CELL_HOME, 2.5)])  # best HOME
    betwarrior = _FakeScraper(
        [_ev("betwarrior-pba", "Panama vs Dominicana", CELL_DRAW, 3.9)]
    )  # DRAW
    linker = _FakeLinker(
        refs=[("ev", "futbol/x/panama-dominicana")],
        snaps_by_event={"ev": [_ev("betsson-pba", "Home vs Away", CELL_AWAY, 4.5)]},  # best AWAY
    )
    src = OverlapQuoteSource(
        bulk_sources=[betano, betwarrior],
        linkers=[linker],
        canonicalizer=_StubCanonicalizer(),
        now_fn=lambda: 0.0,
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
        bulk_sources=[betano, betwarrior],
        linkers=[linker],
        canonicalizer=_StubCanonicalizer(),
        now_fn=lambda: clock["t"],
    )
    await src.fetch()  # t=0: both bulk produced data; linker listed refs
    assert src.stale_platforms(10.0) == {}

    betano._snaps = []  # betano goes dark (block); betwarrior + linker still live
    clock["t"] = 100.0
    await src.fetch()
    stale = src.stale_platforms(10.0)
    assert set(stale) == {"betano"}  # betano stale; betwarrior + betsson-pba refreshed at t=100
    assert stale["betano"] == 100.0


# ---- Phase E: cycle_spreads (GARCH adaptive threshold feed) ----


async def test_cycle_spreads_returns_signed_cross_platform_spread() -> None:
    """Two bulk platforms quoting the same cell populate ``_last_canonical``;
    ``cycle_spreads`` emits the signed PAIR_ORDER spread (betano − betsson-pba)."""
    clock = {"t": 0.0}
    betano = _FakeScraper([_snap("betano", CELL_HOME, 2.0)])  # prob 0.5
    betsson = _FakeScraper([_snap("betsson-pba", CELL_HOME, 2.5)])  # prob 0.4
    src = OverlapQuoteSource(
        bulk_sources=[betano, betsson],
        linkers=[],
        canonicalizer=_StubCanonicalizer(),
        now_fn=lambda: clock["t"],
    )
    await src.fetch()
    spreads = src.cycle_spreads()
    assert len(spreads) == 1
    obs = spreads[0]
    assert obs.market_id == _MKT_ID
    assert obs.market_type == "1x2"
    assert obs.cell == CELL_HOME
    # PAIR_ORDER: betano first, betsson-pba third → spread = 1/2 − 1/2.5 = 0.1
    assert abs(obs.spread - (1.0 / 2.0 - 1.0 / 2.5)) < 1e-9


async def test_cycle_spreads_single_platform_no_observation() -> None:
    """Only one platform quoting a cell → no cross-platform pair → empty."""
    clock = {"t": 0.0}
    betano = _FakeScraper([_snap("betano", CELL_HOME, 2.0)])
    src = OverlapQuoteSource(
        bulk_sources=[betano],
        linkers=[],
        canonicalizer=_StubCanonicalizer(),
        now_fn=lambda: clock["t"],
    )
    await src.fetch()
    assert src.cycle_spreads() == []


async def test_cycle_spreads_stale_quotes_excluded() -> None:
    """Quotes older than ``staleness_sec`` are filtered out → no observation once the
    clock advances past the window. Uses a mutable clock (the helper's fixed ``now``
    lambda can't be advanced after fetch)."""
    clock = {"t": 0.0}
    betano = _FakeScraper([_snap("betano", CELL_HOME, 2.0)])  # timestamp 0.0
    betsson = _FakeScraper([_snap("betsson-pba", CELL_HOME, 2.5)])  # timestamp 0.0
    src = OverlapQuoteSource(
        bulk_sources=[betano, betsson],
        linkers=[],
        canonicalizer=_StubCanonicalizer(),
        staleness_sec=45.0,
        now_fn=lambda: clock["t"],
    )
    await src.fetch()
    assert src.cycle_spreads()  # fresh at t=0
    clock["t"] = 100.0  # past staleness_sec=45 → cached quotes (ts=0) are stale
    assert src.cycle_spreads() == []


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
            platform="bplay-pba",
            platform_event_id="b-evt",
            platform_market_id="b-mkt",
            platform_outcome_id=f"bp-{outcome}",
            raw_event_name=name,
            raw_market_name=market,
            raw_outcome_name=outcome,
            decimal_odds=odds,
            max_stake=5000.0,
            timestamp=0.0,
        )

    # Anchor (Bplay) registers the fixture via "{home} vs {away}".
    anchor = _FakeScraper(
        [
            _ev("Boca Juniors vs River Plate", "1-X-2", "Boca Juniors", 2.0),
            _ev("Boca Juniors vs River Plate", "1-X-2", "Empate", 3.5),
            _ev("Boca Juniors vs River Plate", "1-X-2", "River Plate", 4.0),
        ]
    )

    # Linker (Betsson) builds raw_event_name FROM THE SLUG it's handed — exactly like
    # the real scraper (empty slug ⇒ empty name ⇒ no link). Markets/outcomes resolvable.
    def _bets(slug: str, outcome: str, odds: float) -> RawOddsSnapshot:
        name = slug.rsplit("/", 1)[-1].replace("-", " ")  # mirrors _event_name_from_slug
        return RawOddsSnapshot(
            platform="betsson-pba",
            platform_event_id="ev-match",
            platform_market_id="m",
            platform_outcome_id=f"bs-{outcome}",
            raw_event_name=name,
            raw_market_name="Ganador del partido",
            raw_outcome_name=outcome,
            decimal_odds=odds,
            max_stake=5000.0,
            timestamp=0.0,
        )

    slug = "futbol/argentina/lpf/boca-juniors-river-plate"
    linker = _FakeLinker(
        refs=[("ev-match", slug)],
        snaps_by_event={
            "ev-match": [
                _bets(slug, "Boca Juniors", 2.1),
                _bets(slug, "Empate", 3.4),
                _bets(slug, "River Plate", 4.2),
            ]
        },
    )
    src = OverlapQuoteSource(
        bulk_sources=[anchor],
        linkers=[linker],
        canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
        now_fn=lambda: 0.0,
    )
    out = await src.fetch()
    assert linker.fetched_slugs == [slug]  # the slug was threaded, not dropped
    assert len(out) == 1  # one cross-platform 1X2 market formed
    (quotes,) = out.values()
    assert {q.platform for q in quotes} == {"bplay-pba", "betsson-pba"}  # both books linked


# ---- Phase C: trigger_fetch (leader-triggered burst scanning) ----


class _FakeBulkScraper:
    """Bulk scraper with controllable odds and platform_name for cache scoping."""

    def __init__(self, snaps: list[RawOddsSnapshot], platform_name: str = "betano") -> None:
        self._snaps = snaps
        self.platform_name = platform_name

    async def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]:
        for s in self._snaps:
            yield s


class _FakeTriggerLinker:
    """Linker scraper for trigger tests: records fetch_event_quotes calls."""

    platform_name = "betsson-pba"

    def __init__(self, snaps_by_event: dict[str, list[RawOddsSnapshot]] | None = None) -> None:
        self.snaps_by_event = snaps_by_event or {}
        self.fetched: list[str] = []

    async def list_fixture_refs(self) -> list[tuple[str, str]]:
        return []

    async def fetch_event_quotes(self, event_id: str, slug: str = "") -> list[RawOddsSnapshot]:
        self.fetched.append(event_id)
        return self.snaps_by_event.get(event_id, [])


_LAG_MODEL = {
    "burst_eligible_market_types": ["1x2"],
    "per_market_type": {
        "1x2": {"lag_p90_s": 60.0},
    },
    "staleness_rank": {},
}


def _overlap_source(
    bulk: _FakeBulkScraper,
    linker: _FakeTriggerLinker | None = None,
    lag_model: dict | None = None,
    now: float = 1020.0,
    snapshot_sink: asyncio.Queue | None = None,
) -> OverlapQuoteSource:
    return OverlapQuoteSource(
        bulk_sources=[bulk],
        linkers=[linker] if linker else [],
        canonicalizer=_StubCanonicalizer(),
        staleness_sec=45.0,
        lag_model=lag_model,
        now_fn=lambda: now,
        snapshot_sink=snapshot_sink,
    )


def _full_3way(
    odds_home: float, odds_draw: float, odds_away: float, ts: float = 1000.0
) -> list[RawOddsSnapshot]:
    """A complete 3-cell 1X2 bulk snapshot set."""
    return [
        _snap("betano", CELL_HOME, odds_home, ts=ts),
        _snap("betano", CELL_DRAW, odds_draw, ts=ts),
        _snap("betano", CELL_AWAY, odds_away, ts=ts),
    ]


async def test_trigger_fetch_no_lag_model_bursts_with_defaults() -> None:
    """The gate is TRIGGER_POLL, not the artifact: with lag_model=None, trigger_fetch
    uses built-in defaults and skips the burst_eligible filter (still bounded by
    burst_budget). A real move after a baseline bursts."""
    bulk = _FakeBulkScraper(_full_3way(2.0, 3.0, 3.5, ts=1000.0))
    src = _overlap_source(bulk, lag_model=None)
    await src.fetch()  # baseline
    bulk._snaps = _full_3way(2.2, 3.0, 3.5, ts=1010.0)  # HOME moves ≥0.5%
    result = await src.trigger_fetch()
    assert len(result) >= 1  # bursts despite no artifact (defaults + no eligibility filter)


async def test_trigger_fetch_returns_empty_when_no_burst_eligible() -> None:
    """Gate: lag_model without burst_eligible_market_types → no trigger."""
    bulk = _FakeBulkScraper(_full_3way(2.0, 3.0, 3.5))
    src = _overlap_source(bulk, lag_model={"burst_eligible_market_types": []})
    result = await src.trigger_fetch()
    assert result == {}


async def test_trigger_fetch_detects_move_and_returns_hot_market() -> None:
    """A ≥0.5% odds change triggers a hot market; the complete cached partition is returned."""
    bulk = _FakeBulkScraper(_full_3way(2.0, 3.0, 3.5, ts=1000.0))
    src = _overlap_source(bulk, lag_model=_LAG_MODEL)
    await src.fetch()
    bulk._snaps = _full_3way(2.2, 3.0, 3.5, ts=1010.0)
    result = await src.trigger_fetch()
    assert len(result) >= 1


async def test_trigger_fetch_no_move_returns_empty() -> None:
    """Unchanged odds → no move detected → empty dict."""
    bulk = _FakeBulkScraper(_full_3way(2.0, 3.0, 3.5, ts=1000.0))
    src = _overlap_source(bulk, lag_model=_LAG_MODEL)
    await src.fetch()
    bulk._snaps = _full_3way(2.0, 3.0, 3.5, ts=1010.0)
    result = await src.trigger_fetch()
    assert result == {}


async def test_trigger_fetch_respects_burst_budget() -> None:
    """At most burst_budget linker refetches per trigger call."""
    bulk = _FakeBulkScraper(_full_3way(2.0, 3.0, 3.5, ts=1000.0))
    linker = _FakeTriggerLinker(
        snaps_by_event={
            "evt1": [_snap("betsson-pba", CELL_HOME, 2.5, ts=1010.0)],
        }
    )
    src = _overlap_source(bulk, linker=linker, lag_model=_LAG_MODEL)
    await src.fetch()
    src._linker_event_by_market[_MKT_ID] = ("evt1", "slug1")
    bulk._snaps = _full_3way(2.2, 3.0, 3.5, ts=1010.0)
    await src.trigger_fetch(burst_budget=0)
    assert linker.fetched == []


async def test_trigger_fetch_malformed_nested_shapes_no_crash() -> None:
    """A valid-dict artifact with malformed nested shapes degrades to safe defaults
    (non-list burst_eligible → hard-disable; never crashes the hot loop)."""
    bulk = _FakeBulkScraper(_full_3way(2.0, 3.0, 3.5, ts=1000.0))
    src = _overlap_source(bulk, lag_model={"burst_eligible_market_types": "1x2"})
    await src.fetch()
    bulk._snaps = _full_3way(2.2, 3.0, 3.5, ts=1010.0)
    result = await src.trigger_fetch()
    assert result == {}  # non-list burst_eligible → treated as empty → no bursting


# ---- Recording tee (snapshot_sink) ----


async def test_fetch_tees_raw_snapshots_to_sink_queue() -> None:
    """fetch() copies every raw bulk snapshot it scrapes into snapshot_sink,
    unchanged and before canonicalization."""
    snaps = _full_3way(2.0, 3.0, 3.5, ts=1000.0)
    bulk = _FakeBulkScraper(snaps)
    queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
    src = _overlap_source(bulk, snapshot_sink=queue)
    await src.fetch()
    teed = [queue.get_nowait() for _ in range(queue.qsize())]
    assert teed == snaps  # same objects, same order
    assert [s.platform for s in teed] == ["betano", "betano", "betano"]


async def test_trigger_fetch_tees_bulk_snapshots() -> None:
    """Both the baseline fetch() and the trigger_fetch() re-scrape tee their
    raw bulk snapshots into the sink."""
    baseline = _full_3way(2.0, 3.0, 3.5, ts=1000.0)
    bulk = _FakeBulkScraper(baseline)
    queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
    src = _overlap_source(bulk, lag_model=_LAG_MODEL, snapshot_sink=queue)
    await src.fetch()
    moved = _full_3way(2.2, 3.0, 3.5, ts=1010.0)
    bulk._snaps = moved
    await src.trigger_fetch()
    teed = [queue.get_nowait() for _ in range(queue.qsize())]
    assert teed == baseline + moved  # both cycles' raw snaps present


async def test_tee_queue_full_drops_without_raising() -> None:
    """A full sink never back-pressures the money path: put_nowait drops the
    overflow and bumps _tee_dropped instead of raising."""
    snaps = _full_3way(2.0, 3.0, 3.5, ts=1000.0)
    bulk = _FakeBulkScraper(snaps)
    queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue(maxsize=1)
    src = _overlap_source(bulk, snapshot_sink=queue)
    await src.fetch()  # must not raise
    assert queue.qsize() == 1
    assert src._tee_dropped == 2
