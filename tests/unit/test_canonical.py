"""Unit tests for canonical types — shape, equality, and the
EXPECTED_CELLS contract.

These tests are intentionally light: the module is pure data with no
logic. The point is to lock the shape so resolver code can depend on
it without surprises.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from src.arbitrage.quotes import OddsQuote
from src.semantic.canonical import (
    CELL_AWAY,
    CELL_DRAW,
    CELL_HOME,
    EXPECTED_CELLS,
    CanonicalFixture,
    CanonicalMarket,
    CanonicalMarketCode,
    CanonicalOutcome,
    CanonicalQuote,
)


class TestMarketCode:
    def test_h2h_3way_string_value(self) -> None:
        assert CanonicalMarketCode.H2H_3WAY.value == "1x2"

    def test_expected_cells_for_h2h_3way(self) -> None:
        cells = EXPECTED_CELLS[CanonicalMarketCode.H2H_3WAY]
        assert cells == frozenset({CELL_HOME, CELL_DRAW, CELL_AWAY})


class TestDataclassShape:
    def test_canonical_market_equality(self) -> None:
        a = CanonicalMarket(CanonicalMarketCode.H2H_3WAY)
        b = CanonicalMarket(CanonicalMarketCode.H2H_3WAY, line=None)
        assert a == b
        assert hash(a) == hash(b)

    def test_canonical_market_frozen(self) -> None:
        m = CanonicalMarket(CanonicalMarketCode.H2H_3WAY)
        with pytest.raises(dataclasses.FrozenInstanceError):
            m.line = 2.5  # type: ignore[misc]

    def test_canonical_outcome_equality(self) -> None:
        market = CanonicalMarket(CanonicalMarketCode.H2H_3WAY)
        a = CanonicalOutcome(market=market, cell=CELL_HOME)
        b = CanonicalOutcome(market=market, cell=CELL_HOME)
        assert a == b
        assert hash(a) == hash(b)

    def test_canonical_fixture_equality_via_fields(self) -> None:
        kickoff = datetime(2026, 5, 26, 22, 0, tzinfo=UTC)
        a = CanonicalFixture(
            fixture_id="fx-abc",
            home_team="lanus",
            away_team="mirassol",
            kickoff_utc=kickoff,
            competition_slug="copa_libertadores",
        )
        b = CanonicalFixture(
            fixture_id="fx-abc",
            home_team="lanus",
            away_team="mirassol",
            kickoff_utc=kickoff,
            competition_slug="copa_libertadores",
        )
        assert a == b

    def test_canonical_fixture_optional_fields_default_none(self) -> None:
        """v1: kickoff_utc and competition_slug aren't carried in the
        snapshot yet, so the resolver leaves them None."""
        fx = CanonicalFixture(
            fixture_id="fx-1",
            home_team="lanus",
            away_team="mirassol",
        )
        assert fx.kickoff_utc is None
        assert fx.competition_slug is None

    def test_canonical_quote_carries_oddsquote(self) -> None:
        market = CanonicalMarket(CanonicalMarketCode.H2H_3WAY)
        outcome = CanonicalOutcome(market=market, cell=CELL_HOME)
        fixture = CanonicalFixture(
            fixture_id="fx-1",
            home_team="lanus",
            away_team="mirassol",
            kickoff_utc=datetime(2026, 5, 26, 22, 0, tzinfo=UTC),
            competition_slug="copa_libertadores",
        )
        odds = OddsQuote(
            platform="betwarrior-pba",
            market_id="1x2|fx-1",
            outcome=CELL_HOME,
            decimal_odds=1.29,
            max_stake=None,
            timestamp=1748287200.0,
        )
        quote = CanonicalQuote(fixture=fixture, outcome=outcome, odds_quote=odds)
        assert quote.odds_quote.decimal_odds == 1.29
        assert quote.outcome.cell == CELL_HOME
        assert quote.fixture.fixture_id == "fx-1"
