"""Unit tests for outcome_resolver.

Covers the two outcome-label shapes (position vs team-name) and
documents the team-similarity threshold behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.canonical import (
    CELL_AWAY,
    CELL_DRAW,
    CELL_HOME,
    CELL_NO,
    CELL_OVER,
    CELL_UNDER,
    CELL_YES,
    CanonicalFixture,
    CanonicalMarket,
    CanonicalMarketCode,
)
from src.semantic.outcome_resolver import resolve_outcome


def _snap(platform: str, raw_outcome_name: str) -> RawOddsSnapshot:
    return RawOddsSnapshot(
        platform=platform,
        platform_event_id="evt-1",
        platform_market_id="mkt-1",
        platform_outcome_id="out-1",
        raw_event_name="Home Team - Away Team",
        raw_market_name="ignored-by-this-test",
        raw_outcome_name=raw_outcome_name,
        decimal_odds=2.0,
        max_stake=None,
        timestamp=1748287200.0,
    )


def _fixture(home: str = "lanus", away: str = "mirassol sp") -> CanonicalFixture:
    return CanonicalFixture(
        fixture_id="fx-test",
        home_team=home,  # already-normalized form
        away_team=away,
        kickoff_utc=datetime(2026, 5, 26, 22, 0, tzinfo=UTC),
        competition_slug="copa_libertadores",
    )


_H2H = CanonicalMarket(code=CanonicalMarketCode.H2H_3WAY, line=None)


class TestPositionLabels:
    """BetWarrior emits position-label outcomes; map them directly."""

    @pytest.mark.parametrize(
        ("label", "expected_cell"),
        [("1", CELL_HOME), ("X", CELL_DRAW), ("2", CELL_AWAY)],
    )
    def test_position_label_resolves(self, label: str, expected_cell: str) -> None:
        snap = _snap("betwarrior-pba", label)
        result = resolve_outcome(snap, _H2H, _fixture())
        assert result is not None
        assert result.cell == expected_cell
        assert result.market == _H2H

    def test_unknown_position_label_returns_none(self) -> None:
        # Kambi uses "1"/"X"/"2"; "3" is invalid for H2H_3WAY.
        snap = _snap("betwarrior-pba", "3")
        assert resolve_outcome(snap, _H2H, _fixture()) is None


class TestTeamLabelDraw:
    @pytest.mark.parametrize(
        ("platform", "label"),
        [
            ("bplay-pba", "Empate"),
            ("betsson-pba", "Empate"),
            ("bplay-pba", "empate"),  # case-insensitive
        ],
    )
    def test_empate_resolves_to_draw(self, platform: str, label: str) -> None:
        result = resolve_outcome(_snap(platform, label), _H2H, _fixture())
        assert result is not None
        assert result.cell == CELL_DRAW


class TestTeamLabelHomeAway:
    def test_exact_home_match_resolves_home(self) -> None:
        snap = _snap("bplay-pba", "Lanús")
        result = resolve_outcome(snap, _H2H, _fixture(home="lanus", away="mirassol sp"))
        assert result is not None
        assert result.cell == CELL_HOME

    def test_exact_away_match_resolves_away(self) -> None:
        snap = _snap("bplay-pba", "Mirassol SP")
        result = resolve_outcome(snap, _H2H, _fixture(home="lanus", away="mirassol sp"))
        assert result is not None
        assert result.cell == CELL_AWAY

    def test_kambi_sportnco_variant_match_strict_threshold(self) -> None:
        """Real recon case: Bplay outcome `"Mirassol FC SP"` should
        resolve against canonical fixture `"mirassol sp"` (BetWarrior
        anchor). The similarity score must clear 0.85."""
        snap = _snap("bplay-pba", "Mirassol FC SP")
        result = resolve_outcome(snap, _H2H, _fixture(home="lanus", away="mirassol sp"))
        assert result is not None
        assert result.cell == CELL_AWAY

    def test_reserve_outcome_label_resolves_against_base_fixture(self) -> None:
        """Fixtures store BASE names, so a reserve outcome label ('Huracán II' /
        'Huracán Reserves') strips its marker to match — reserve-ness was already
        settled when the fixture was linked, so the cell mapping is unambiguous."""
        for label in ("Huracan II", "Huracán Reserves"):
            result = resolve_outcome(
                _snap("bplay-pba", label), _H2H, _fixture(home="huracan", away="estudiantes")
            )
            assert result is not None and result.cell == CELL_HOME

    def test_below_threshold_returns_none(self) -> None:
        """The discriminator: `Belgrano` against canonical
        `Belgrano Reserves` is below 0.85. The resolver returns None
        rather than risk conflating distinct teams."""
        snap = _snap("bplay-pba", "Belgrano")
        result = resolve_outcome(
            snap, _H2H, _fixture(home="belgrano reserves", away="quilmes reserves")
        )
        assert result is None

    def test_label_not_matching_either_team_returns_none(self) -> None:
        snap = _snap("bplay-pba", "Unrelated Team Name")
        result = resolve_outcome(snap, _H2H, _fixture(home="lanus", away="mirassol sp"))
        assert result is None


class TestUnsupportedInputs:
    def test_unsupported_market_returns_none(self) -> None:
        """Until DNB and AH ship, only H2H_3WAY / BTTS / OU_GOALS
        resolve. A market code outside that set returns None."""
        # No DNB/AH enum values yet — verify by mocking via a
        # market_code that doesn't exist in EXPECTED_CELLS would
        # require enum extension. Instead, exercise the "no cell"
        # branch by passing an OU market with an unknown outcome.
        snap = _snap("bplay-pba", "completely unknown label")
        ou = CanonicalMarket(code=CanonicalMarketCode.OU_GOALS, line=2.5)
        assert resolve_outcome(snap, ou, _fixture()) is None

    def test_empty_outcome_label_returns_none(self) -> None:
        snap = _snap("bplay-pba", "")
        assert resolve_outcome(snap, _H2H, _fixture()) is None
        snap = _snap("bplay-pba", "   ")
        assert resolve_outcome(snap, _H2H, _fixture()) is None


# ---- BTTS ----

_BTTS = CanonicalMarket(code=CanonicalMarketCode.BTTS, line=None)


class TestBTTSResolution:
    @pytest.mark.parametrize(
        ("platform", "label", "expected_cell"),
        [
            ("betsson-pba", "Si", CELL_YES),
            ("betsson-pba", "Sí", CELL_YES),  # accent variant
            ("betsson-pba", "SI", CELL_YES),
            ("betsson-pba", "No", CELL_NO),
            ("betsson-pba", "no", CELL_NO),
            # Bplay: anticipated identical convention; tests guard against
            # divergence when live samples appear.
            ("bplay-pba", "Si", CELL_YES),
            ("bplay-pba", "No", CELL_NO),
            # English fallback (if a tenant ever localizes to en).
            ("betsson-pba", "Yes", CELL_YES),
        ],
    )
    def test_btts_outcome_resolves(
        self, platform: str, label: str, expected_cell: str
    ) -> None:
        snap = _snap(platform, label)
        result = resolve_outcome(snap, _BTTS, _fixture())
        assert result is not None
        assert result.cell == expected_cell

    def test_unknown_btts_outcome_returns_none(self) -> None:
        snap = _snap("betsson-pba", "maybe")
        assert resolve_outcome(snap, _BTTS, _fixture()) is None

    def test_no_fixture_dependency_for_btts(self) -> None:
        """BTTS doesn't need the fixture context — verify by passing
        a clearly-unrelated fixture and confirming resolution still
        works."""
        snap = _snap("betsson-pba", "Si")
        unrelated = _fixture(home="unrelated", away="teams")
        result = resolve_outcome(snap, _BTTS, unrelated)
        assert result is not None
        assert result.cell == CELL_YES


# ---- OU goals ----

_OU_2_5 = CanonicalMarket(code=CanonicalMarketCode.OU_GOALS, line=2.5)


class TestOUResolution:
    @pytest.mark.parametrize(
        ("platform", "label", "expected_cell"),
        [
            # Bplay's bare labels
            ("bplay-pba", "Más", CELL_OVER),
            ("bplay-pba", "Menos", CELL_UNDER),
            ("bplay-pba", "más", CELL_OVER),
            ("bplay-pba", "MENOS", CELL_UNDER),
            # Betsson's line-in-label form
            ("betsson-pba", "más de 2.5", CELL_OVER),
            ("betsson-pba", "menos de 2.5", CELL_UNDER),
            ("betsson-pba", "Más de 2.5", CELL_OVER),
            ("betsson-pba", "MENOS DE 2.5", CELL_UNDER),
            # Betsson at other lines
            ("betsson-pba", "más de 0.5", CELL_OVER),
            ("betsson-pba", "menos de 4.5", CELL_UNDER),
            # English fallback
            ("betsson-pba", "Over", CELL_OVER),
            ("betsson-pba", "Under", CELL_UNDER),
        ],
    )
    def test_ou_outcome_resolves(
        self, platform: str, label: str, expected_cell: str
    ) -> None:
        snap = _snap(platform, label)
        result = resolve_outcome(snap, _OU_2_5, _fixture())
        assert result is not None
        assert result.cell == expected_cell

    def test_ou_unknown_label_returns_none(self) -> None:
        snap = _snap("bplay-pba", "draw")
        assert resolve_outcome(snap, _OU_2_5, _fixture()) is None
        snap = _snap("bplay-pba", "yes")
        assert resolve_outcome(snap, _OU_2_5, _fixture()) is None

    def test_ou_no_fixture_dependency(self) -> None:
        snap = _snap("bplay-pba", "Más")
        unrelated = _fixture(home="unrelated", away="teams")
        result = resolve_outcome(snap, _OU_2_5, unrelated)
        assert result is not None
        assert result.cell == CELL_OVER
