"""Unit tests for market_resolver.

Anchored on the actual raw_market_name strings observed in the
30s Redis ingestion run on 2026-05-26.
"""

from __future__ import annotations

import pytest

from src.semantic.canonical import CanonicalMarket, CanonicalMarketCode
from src.semantic.market_resolver import resolve_market


class TestH2H3Way:
    @pytest.mark.parametrize(
        ("platform", "raw_market_name"),
        [
            ("betsson-pba", "Ganador del partido"),
            # Real recon case: Betsson's capitalization varies across snapshots.
            ("betsson-pba", "Ganador del Partido"),
            # And the case-insensitivity should be robust to all-lower.
            ("betsson-pba", "ganador del partido"),
            # Defensive whitespace tolerance.
            ("betsson-pba", "  Ganador  del  partido  "),
            ("bplay-pba", "1-X-2"),
            ("bplay-pba", "1-x-2"),
            ("betwarrior-pba", "Resultado Final"),
            ("betwarrior-pba", "resultado final"),
            ("betano", "Resultado del partido"),
            ("betano", "resultado del partido"),
        ],
    )
    def test_known_h2h_3way_resolves(self, platform: str, raw_market_name: str) -> None:
        result = resolve_market(platform, raw_market_name)
        assert result == CanonicalMarket(code=CanonicalMarketCode.H2H_3WAY, line=None)


class TestNonV1Markets:
    @pytest.mark.parametrize(
        ("platform", "raw_market_name"),
        [
            # DNB and AH are not in v1 scope yet.
            ("bplay-pba", "1-2"),
            ("bplay-pba", "Handicap 1-2 -1.5"),
            # Unknown platform.
            ("imaginary-platform", "Ganador del partido"),
            # Empty.
            ("betsson-pba", ""),
            ("bplay-pba", "1-X-3"),  # invented near-miss
        ],
    )
    def test_non_v1_returns_none(self, platform: str, raw_market_name: str) -> None:
        assert resolve_market(platform, raw_market_name) is None

    def test_cross_platform_label_does_not_match(self) -> None:
        """The label is real for Bplay; on Betsson it must NOT match
        (otherwise the resolver is too permissive)."""
        assert resolve_market("betsson-pba", "1-X-2") is None
        assert resolve_market("bplay-pba", "Ganador del partido") is None
        assert resolve_market("betwarrior-pba", "1-X-2") is None


class TestBTTS:
    @pytest.mark.parametrize(
        ("platform", "raw_market_name"),
        [
            ("betsson-pba", "Ambos equipos anotan"),
            ("betsson-pba", "AMBOS EQUIPOS ANOTAN"),
            ("betsson-pba", "  ambos  equipos  anotan  "),
            # Bplay: scraper accepts any "Ambos" prefix; until we have
            # live samples, accept the same prefix here.
            ("bplay-pba", "Ambos equipos anotan"),
            ("bplay-pba", "Ambos equipos marcan"),
            ("bplay-pba", "ambos equipos anotan 90"),
        ],
    )
    def test_btts_resolves(self, platform: str, raw_market_name: str) -> None:
        result = resolve_market(platform, raw_market_name)
        assert result == CanonicalMarket(code=CanonicalMarketCode.BTTS, line=None)

    def test_betsson_btts_is_exact_not_prefix(self) -> None:
        """Betsson uses exact match for BTTS (the scraper emits the
        full canonical name). A different `Ambos *` string must NOT
        accidentally match on Betsson."""
        assert resolve_market("betsson-pba", "Ambos equipos algo distinto") is None

    def test_betwarrior_btts_not_supported_yet(self) -> None:
        """BetWarrior's list-view scraper doesn't emit BTTS. Even if a
        BTTS-shaped string showed up, the resolver should reject it
        until BetWarrior's per-event scraper ships AND the Kambi
        BTTS label is observed."""
        assert resolve_market("betwarrior-pba", "Ambos equipos anotan") is None


class TestOuGoals:
    @pytest.mark.parametrize(
        ("platform", "raw_market_name", "expected_line"),
        [
            ("betsson-pba", "Total de goles 0.5", 0.5),
            ("betsson-pba", "Total de goles 1.5", 1.5),
            ("betsson-pba", "Total de goles 2.5", 2.5),
            ("betsson-pba", "Total de goles 3.5", 3.5),
            ("betsson-pba", "Total de goles 4.5", 4.5),
            ("bplay-pba", "Más de / Menos de 0.5", 0.5),
            ("bplay-pba", "Más de / Menos de 2.5", 2.5),
            ("bplay-pba", "Más de / Menos de 7.5", 7.5),
            # Case-insensitivity
            ("bplay-pba", "MÁS DE / MENOS DE 2.5", 2.5),
        ],
    )
    def test_half_lines_resolve(
        self, platform: str, raw_market_name: str, expected_line: float
    ) -> None:
        result = resolve_market(platform, raw_market_name)
        assert result == CanonicalMarket(code=CanonicalMarketCode.OU_GOALS, line=expected_line)

    @pytest.mark.parametrize(
        ("platform", "raw_market_name"),
        [
            # Push lines — observed Betsson integer lines must be rejected.
            ("betsson-pba", "Total de goles 1"),
            ("betsson-pba", "Total de goles 2"),
            ("betsson-pba", "Total de goles 3"),
            ("betsson-pba", "Total de goles 4"),
            # Hypothetical zero line
            ("bplay-pba", "Más de / Menos de 0"),
        ],
    )
    def test_integer_lines_rejected_push_lines(self, platform: str, raw_market_name: str) -> None:
        """Integer lines are PUSH lines — total goals = N refunds both
        sides. {OVER, UNDER} on a push line isn't a clean partition;
        treating it as one would let the detector emit false arbs."""
        assert resolve_market(platform, raw_market_name) is None

    def test_quarter_lines_rejected(self) -> None:
        """Quarter lines (e.g. 2.25, 2.75) would be Asian OU, not OU
        goals. Reject — half-lines only."""
        assert resolve_market("bplay-pba", "Más de / Menos de 2.25") is None

    def test_unknown_ou_format_returns_none(self) -> None:
        assert resolve_market("betsson-pba", "Total de goles") is None
        assert resolve_market("betsson-pba", "Total de goles 2.5 extra") is None
