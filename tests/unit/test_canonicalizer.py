"""Integration tests for the canonicalizer.

These are the most-important tests in the semantic layer: they
verify that three platforms emitting the same real-world fixture
produce three CanonicalQuotes with IDENTICAL `market_id` and the
expected three distinct outcome cells. That's the exact contract
`dutch_book.detect_arbitrage` depends on downstream.

The fixtures here mirror real strings observed during the 30s
3-platform Redis ingestion run on 2026-05-26.
"""

from __future__ import annotations

import pytest

from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.canonical import (
    CELL_AWAY,
    CELL_DRAW,
    CELL_HOME,
    CanonicalMarketCode,
)
from src.semantic.canonicalizer import Canonicalizer, canonical_market_id
from src.semantic.fixture_resolver import FixtureResolver


def _snap(
    platform: str,
    platform_event_id: str,
    raw_event_name: str,
    raw_market_name: str,
    raw_outcome_name: str,
    decimal_odds: float = 2.0,
    timestamp: float = 1000.0,
) -> RawOddsSnapshot:
    return RawOddsSnapshot(
        platform=platform,
        platform_event_id=platform_event_id,
        platform_market_id=f"mkt-{platform_event_id}",
        platform_outcome_id=f"out-{platform_event_id}-{raw_outcome_name}",
        raw_event_name=raw_event_name,
        raw_market_name=raw_market_name,
        raw_outcome_name=raw_outcome_name,
        decimal_odds=decimal_odds,
        max_stake=None,
        timestamp=timestamp,
    )


# ---- The headline test: cross-platform 1X2 → identical market_id ----


class TestCrossPlatformCanonicalization:
    async def test_three_platforms_same_fixture_share_market_id(self) -> None:
        """The contract: snapshots from Betsson + Bplay + BetWarrior
        on the same real-world match must produce CanonicalQuotes
        sharing one canonical `market_id` and covering the three
        distinct 1X2 cells.

        Setup mirrors what would actually happen in the daemon: the
        BetWarrior snapshot arrives first (anchor platform), then
        Bplay (also anchor, links via exact normalized team match),
        then Betsson (uses outcome label to anchor against the
        existing canonical fixture)."""
        c = Canonicalizer(fixture_resolver=FixtureResolver())

        # BetWarrior anchor — position-label outcomes
        bw_home = _snap(
            "betwarrior-pba",
            "k-1027027525",
            "Boca Juniors - River Plate",
            "Resultado Final",
            "1",
            decimal_odds=2.10,
            timestamp=1000.0,
        )
        bw_draw = _snap(
            "betwarrior-pba",
            "k-1027027525",
            "Boca Juniors - River Plate",
            "Resultado Final",
            "X",
            decimal_odds=3.40,
            timestamp=1000.0,
        )
        bw_away = _snap(
            "betwarrior-pba",
            "k-1027027525",
            "Boca Juniors - River Plate",
            "Resultado Final",
            "2",
            decimal_odds=3.20,
            timestamp=1000.0,
        )

        # Bplay — team-name outcomes
        bp_home = _snap(
            "bplay-pba",
            "b-11428880",
            "Boca Juniors vs River Plate",
            "1-X-2",
            "Boca Juniors",
            decimal_odds=2.15,
            timestamp=1001.0,
        )
        bp_draw = _snap(
            "bplay-pba",
            "b-11428880",
            "Boca Juniors vs River Plate",
            "1-X-2",
            "Empate",
            decimal_odds=3.30,
            timestamp=1001.0,
        )
        bp_away = _snap(
            "bplay-pba",
            "b-11428880",
            "Boca Juniors vs River Plate",
            "1-X-2",
            "River Plate",
            decimal_odds=3.25,
            timestamp=1001.0,
        )

        # Betsson — flat event name, team-name outcomes
        bet_home = _snap(
            "betsson-pba",
            "f-abc",
            "boca juniors river plate",
            "Ganador del partido",
            "Boca Juniors",
            decimal_odds=2.05,
            timestamp=1002.0,
        )
        bet_draw = _snap(
            "betsson-pba",
            "f-abc",
            "boca juniors river plate",
            "Ganador del partido",
            "Empate",
            decimal_odds=3.50,
            timestamp=1002.0,
        )
        bet_away = _snap(
            "betsson-pba",
            "f-abc",
            "boca juniors river plate",
            "Ganador del partido",
            "River Plate",
            decimal_odds=3.10,
            timestamp=1002.0,
        )

        # Process in realistic order
        quotes = []
        for snap in (
            bw_home,
            bw_draw,
            bw_away,
            bp_home,
            bp_draw,
            bp_away,
            bet_home,
            bet_draw,
            bet_away,
        ):
            q = await c.canonicalize(snap)
            assert q is not None, f"failed to canonicalize: {snap}"
            quotes.append(q)

        # Exactly one canonical fixture across all 9 snapshots
        fixture_ids = {q.fixture.fixture_id for q in quotes}
        assert len(fixture_ids) == 1

        # Exactly one canonical market_id across all 9
        market_ids = {q.odds_quote.market_id for q in quotes}
        assert len(market_ids) == 1

        # 3 distinct outcome cells, 3 snapshots per cell
        from collections import Counter

        cells = Counter(q.outcome.cell for q in quotes)
        assert cells == {CELL_HOME: 3, CELL_DRAW: 3, CELL_AWAY: 3}

        # Each platform contributed each cell exactly once
        per_platform_cells = {
            "betwarrior-pba": Counter(),
            "bplay-pba": Counter(),
            "betsson-pba": Counter(),
        }
        for q in quotes:
            per_platform_cells[q.odds_quote.platform][q.outcome.cell] += 1
        for plat in per_platform_cells:
            assert per_platform_cells[plat] == {CELL_HOME: 1, CELL_DRAW: 1, CELL_AWAY: 1}

    async def test_market_id_format_includes_code(self) -> None:
        c = Canonicalizer(fixture_resolver=FixtureResolver())
        snap = _snap(
            "betwarrior-pba",
            "k-1",
            "A - B",
            "Resultado Final",
            "1",
            timestamp=1000.0,
        )
        q = await c.canonicalize(snap)
        assert q is not None
        assert q.odds_quote.market_id == f"{q.fixture.fixture_id}|1x2"

    async def test_btts_two_platforms_share_market_id(self) -> None:
        """BTTS expansion: Betsson + Bplay BTTS snapshots on the same
        fixture should produce CanonicalQuotes with identical
        canonical market_id and cover {YES, NO}.

        Realistic snapshot order: 1X2 from each platform arrives FIRST
        (anchoring `(platform, platform_event_id)` to the canonical
        fixture via the existing team-name + slug paths), then BTTS
        snapshots from the same events ride the cache fast path. In
        production this is automatic — Betsson's accordion endpoint
        returns 1X2 + BTTS + OU in one HTTP, in arrival order; Bplay's
        XML same."""
        c = Canonicalizer(fixture_resolver=FixtureResolver())
        # Anchor each platform's event_id to the fixture via 1X2.
        await c.canonicalize(
            _snap(
                "betwarrior-pba",
                "k-1",
                "Boca - River",
                "Resultado Final",
                "1",
                2.0,
                timestamp=1000.0,
            )
        )
        await c.canonicalize(
            _snap(
                "betsson-pba",
                "f-1",
                "boca river",
                "Ganador del partido",
                "Boca",
                2.0,
                timestamp=1000.0,
            )
        )
        await c.canonicalize(
            _snap("bplay-pba", "b-1", "Boca vs River", "1-X-2", "Boca", 2.0, timestamp=1000.0)
        )
        # Now BTTS hits cache fast path on both platforms.
        bet_yes = _snap(
            "betsson-pba", "f-1", "boca river", "Ambos equipos anotan", "Si", 1.85, timestamp=1001.0
        )
        bet_no = _snap(
            "betsson-pba", "f-1", "boca river", "Ambos equipos anotan", "No", 1.95, timestamp=1001.0
        )
        bp_yes = _snap(
            "bplay-pba",
            "b-1",
            "Boca vs River",
            "Ambos equipos anotan",
            "Si",
            1.90,
            timestamp=1001.0,
        )
        bp_no = _snap(
            "bplay-pba",
            "b-1",
            "Boca vs River",
            "Ambos equipos anotan",
            "No",
            1.90,
            timestamp=1001.0,
        )
        quotes = []
        for snap in (bet_yes, bet_no, bp_yes, bp_no):
            q = await c.canonicalize(snap)
            assert q is not None
            quotes.append(q)
        market_ids = {q.odds_quote.market_id for q in quotes}
        assert len(market_ids) == 1
        sole_market_id = next(iter(market_ids))
        assert sole_market_id.endswith("|btts")
        cells = {q.outcome.cell for q in quotes}
        assert cells == {"YES", "NO"}

    async def test_ou_market_id_keyed_by_line(self) -> None:
        """OU 2.5 on Betsson + Bplay → same canonical market_id.
        Different lines (2.5 vs 3.5) → different market_ids on the
        same fixture, so dutch_book doesn't mix them."""
        c = Canonicalizer(fixture_resolver=FixtureResolver())
        # Anchor each platform via 1X2 first (realistic order).
        await c.canonicalize(
            _snap(
                "betwarrior-pba",
                "k-1",
                "Boca - River",
                "Resultado Final",
                "1",
                2.0,
                timestamp=1000.0,
            )
        )
        await c.canonicalize(
            _snap(
                "betsson-pba",
                "f-1",
                "boca river",
                "Ganador del partido",
                "Boca",
                2.0,
                timestamp=1000.0,
            )
        )
        await c.canonicalize(
            _snap("bplay-pba", "b-1", "Boca vs River", "1-X-2", "Boca", 2.0, timestamp=1000.0)
        )

        bet_over_25 = _snap(
            "betsson-pba",
            "f-1",
            "boca river",
            "Total de goles 2.5",
            "más de 2.5",
            1.95,
            timestamp=1001.0,
        )
        bp_over_25 = _snap(
            "bplay-pba",
            "b-1",
            "Boca vs River",
            "Más de / Menos de 2.5",
            "Más",
            1.90,
            timestamp=1001.0,
        )
        bet_over_35 = _snap(
            "betsson-pba",
            "f-1",
            "boca river",
            "Total de goles 3.5",
            "más de 3.5",
            2.50,
            timestamp=1001.0,
        )

        q25_bet = await c.canonicalize(bet_over_25)
        q25_bp = await c.canonicalize(bp_over_25)
        q35_bet = await c.canonicalize(bet_over_35)
        assert q25_bet is not None and q25_bp is not None and q35_bet is not None
        # Same line → same market_id across platforms
        assert q25_bet.odds_quote.market_id == q25_bp.odds_quote.market_id
        assert q25_bet.odds_quote.market_id.endswith("|ou_goals|2.5")
        # Different line → different market_id
        assert q25_bet.odds_quote.market_id != q35_bet.odds_quote.market_id
        assert q35_bet.odds_quote.market_id.endswith("|ou_goals|3.5")

    async def test_betsson_integer_ou_line_dropped(self) -> None:
        """Push lines (integer goal totals) must not produce canonical
        quotes — they don't form a clean partition."""
        c = Canonicalizer(fixture_resolver=FixtureResolver())
        await c.canonicalize(
            _snap(
                "betwarrior-pba",
                "k-1",
                "Boca - River",
                "Resultado Final",
                "1",
                2.0,
                timestamp=1000.0,
            )
        )
        await c.canonicalize(
            _snap(
                "betsson-pba",
                "f-1",
                "boca river",
                "Ganador del partido",
                "Boca",
                2.0,
                timestamp=1000.0,
            )
        )
        push_snap = _snap(
            "betsson-pba",
            "f-1",
            "boca river",
            "Total de goles 2",
            "más de 2",
            2.00,
            timestamp=1001.0,
        )
        result = await c.canonicalize(push_snap)
        assert result is None

    async def test_betsson_btts_anchors_on_slug_regardless_of_market_order(self) -> None:
        """The Betsson slug carries both team names, so a BTTS/OU snapshot
        anchors its fixture on the slug — it does NOT need a 1X2 snapshot to
        arrive first (the outcome label `Si`/`No` isn't a team name, but the
        slug is enough). Out-of-order ingestion resolves immediately."""
        c = Canonicalizer(fixture_resolver=FixtureResolver())
        # BetWarrior anchor exists, but Betsson event_id isn't linked yet.
        await c.canonicalize(
            _snap(
                "betwarrior-pba",
                "k-1",
                "Boca - River",
                "Resultado Final",
                "1",
                2.0,
                timestamp=1000.0,
            )
        )
        # Betsson BTTS arrives WITHOUT a prior 1X2 anchor on f-1 — still resolves.
        btts = _snap(
            "betsson-pba", "f-1", "boca river", "Ambos equipos anotan", "Si", 1.85, timestamp=1001.0
        )
        cq = await c.canonicalize(btts)
        assert cq is not None
        assert cq.odds_quote.market_id.endswith("|btts")


# ---- Defensive: non-v1 markets drop, unresolvable fixtures drop ----


class TestNonV1MarketsDropped:
    @pytest.mark.parametrize(
        "raw_market_name",
        [
            # DNB and AH are still out of scope.
            "Handicap 1-2 -1.5",
            "1-2",
            # Bplay quarter-line — Asian OU, not in scope.
            "Más de / Menos de 2.25",
        ],
    )
    async def test_non_v1_market_returns_none(self, raw_market_name: str) -> None:
        c = Canonicalizer(fixture_resolver=FixtureResolver())
        snap = _snap("bplay-pba", "b-1", "A vs B", raw_market_name, "A", timestamp=1000.0)
        assert await c.canonicalize(snap) is None


class TestUnresolvableFixtureDrops:
    async def test_betsson_with_no_anchor_drops(self) -> None:
        """Without a prior BetWarrior/Bplay anchor, a Betsson
        snapshot has nowhere to attach. The canonicalizer drops it
        cleanly — no exception."""
        c = Canonicalizer(fixture_resolver=FixtureResolver())
        snap = _snap(
            "betsson-pba",
            "f-bet",
            "boca river",
            "Ganador del partido",
            "Boca",
            timestamp=1000.0,
        )
        assert await c.canonicalize(snap) is None

    async def test_unparseable_event_name_drops(self) -> None:
        c = Canonicalizer(fixture_resolver=FixtureResolver())
        # No separator anywhere
        snap = _snap(
            "betwarrior-pba",
            "k-1",
            "MalformedNoSep",
            "Resultado Final",
            "1",
            timestamp=1000.0,
        )
        assert await c.canonicalize(snap) is None


# ---- canonical_market_id helper ----


class TestCanonicalMarketId:
    def test_no_line(self) -> None:
        from src.semantic.canonical import CanonicalMarket

        m = CanonicalMarket(code=CanonicalMarketCode.H2H_3WAY)
        assert canonical_market_id("fx-1", m) == "fx-1|1x2"

    def test_with_line(self) -> None:
        """Forward-compat check: when OU/AH eventually ship, the
        line value participates in the canonical market_id so
        different line markets don't collide."""
        from src.semantic.canonical import CanonicalMarket

        m = CanonicalMarket(code=CanonicalMarketCode.H2H_3WAY, line=2.5)
        assert canonical_market_id("fx-1", m) == "fx-1|1x2|2.5"
