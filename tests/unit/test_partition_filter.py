"""Unit tests for the rule-based partition pre-filter.

These exercise the normalization helpers and the individual rules in
isolation. The fixture-driven evaluation in test_partition_filter_eval.py
is the integration-style counterpart that locks in the safety property
across the whole labeled set.
"""

from __future__ import annotations

import pytest

from src.semantic.partition_filter import (
    MatchContext,
    Verdict,
    _normalize,
    _rule_negation_complement,
    _rule_yes_no_complement,
    classify,
)

REGULAR_SEASON = MatchContext(
    competition="Liga Profesional", stage="regular_season", is_knockout=False
)
KNOCKOUT_R16 = MatchContext(competition="Copa Libertadores", stage="round_of_16", is_knockout=True)


class TestNormalize:
    def test_strips_accents(self) -> None:
        assert _normalize("Sí") == "si"
        assert _normalize("Más de 2.5 goles") == "mas de 2.5 goles"

    def test_lowercases(self) -> None:
        assert _normalize("BTTS YES") == "btts yes"

    def test_preserves_dots_between_digits(self) -> None:
        assert _normalize("Más de 2.5 goles") == "mas de 2.5 goles"

    def test_strips_stray_dots(self) -> None:
        # Trailing dot not between digits.
        assert _normalize("Btts.") == "btts"

    def test_collapses_punctuation_and_whitespace(self) -> None:
        assert _normalize("Ambos equipos marcan:  Sí") == "ambos equipos marcan si"
        assert _normalize("Boca (eliminado)") == "boca eliminado"

    def test_decimal_comma_normalized_to_dot(self) -> None:
        assert _normalize("Más de 2,5 goles") == "mas de 2.5 goles"

    def test_empty_input(self) -> None:
        assert _normalize("") == ""
        assert _normalize("   ") == ""


class TestYesNoRule:
    def test_simple_si_no_suffix(self) -> None:
        result = _rule_yes_no_complement(
            "Ambos equipos marcan: Sí",
            "Ambos equipos marcan: No",
            REGULAR_SEASON,
        )
        assert result is not None
        verdict, _ = result
        assert verdict == Verdict.VALID

    def test_english_yes_no_suffix(self) -> None:
        result = _rule_yes_no_complement("BTTS Yes", "BTTS No", REGULAR_SEASON)
        assert result is not None
        assert result[0] == Verdict.VALID

    def test_yes_no_with_parenthetical_tail(self) -> None:
        """The yes/no token sits mid-phrase with a common tail on both sides."""
        result = _rule_yes_no_complement(
            "Ambos equipos marcan: Sí (todo el partido incluyendo tiempo extra)",
            "Ambos equipos marcan: No (todo el partido incluyendo tiempo extra)",
            REGULAR_SEASON,
        )
        assert result is not None
        assert result[0] == Verdict.VALID

    def test_order_swapped(self) -> None:
        """Rule is symmetric: 'No' on A and 'Sí' on B also fires."""
        result = _rule_yes_no_complement(
            "Ambos marcan: No",
            "Ambos marcan: Sí",
            REGULAR_SEASON,
        )
        assert result is not None
        assert result[0] == Verdict.VALID

    def test_different_prefixes_does_not_fire(self) -> None:
        """A different prefix on each side must not be classified VALID."""
        result = _rule_yes_no_complement(
            "BTTS Yes",
            "Boca gana No",  # different market entirely
            REGULAR_SEASON,
        )
        assert result is None

    def test_both_have_no_does_not_fire(self) -> None:
        result = _rule_yes_no_complement(
            "Boca no anota Sí",
            "Boca no anota No",
            REGULAR_SEASON,
        )
        # Both sides contain 'no'; ambiguous which 'no' the rule should strip.
        # Conservative: don't fire.
        assert result is None

    def test_no_yes_no_tokens_returns_none(self) -> None:
        result = _rule_yes_no_complement(
            "Gana Boca",
            "Gana River",
            REGULAR_SEASON,
        )
        assert result is None

    def test_does_not_fire_on_compound_with_y(self) -> None:
        """Regression: 'Boca gana y BTTS Sí' / '... y BTTS No' covers only
        Boca-wins outcomes, not the full space. The Sí/No scopes to the
        BTTS conjunct alone, so the pair is INVALID despite identical
        token shape. Pre-filter must defer."""
        result = _rule_yes_no_complement(
            "Boca gana y BTTS Sí",
            "Boca gana y BTTS No",
            REGULAR_SEASON,
        )
        assert result is None

    def test_does_not_fire_on_compound_with_and(self) -> None:
        result = _rule_yes_no_complement(
            "Boca wins and BTTS Yes",
            "Boca wins and BTTS No",
            REGULAR_SEASON,
        )
        assert result is None


class TestNegationRule:
    def test_no_inserted_in_middle(self) -> None:
        result = _rule_negation_complement(
            "Boca gana al medio tiempo",
            "Boca no gana al medio tiempo",
            REGULAR_SEASON,
        )
        assert result is not None
        assert result[0] == Verdict.VALID

    def test_no_prepended(self) -> None:
        result = _rule_negation_complement(
            "Ambos marcan",
            "No ambos marcan",
            REGULAR_SEASON,
        )
        assert result is not None
        assert result[0] == Verdict.VALID

    def test_explicit_negation_wrapper(self) -> None:
        """'No (X)' pattern — parentheses are collapsed by normalization."""
        result = _rule_negation_complement(
            "Boca gana y BTTS Sí",
            "No (Boca gana y BTTS Sí)",
            REGULAR_SEASON,
        )
        assert result is not None
        assert result[0] == Verdict.VALID

    def test_word_order_change_does_not_fire(self) -> None:
        """Sequence-strict on purpose: 'Gana X' vs 'X no gana' has the same
        bag of tokens but the negation rule must not collapse word-order
        differences (risk of false positive)."""
        result = _rule_negation_complement(
            "Gana Newell's Old Boys",
            "Newell's Old Boys no gana",
            REGULAR_SEASON,
        )
        assert result is None

    def test_both_contain_no_does_not_fire(self) -> None:
        result = _rule_negation_complement(
            "Boca no gana",
            "Boca no empata",
            REGULAR_SEASON,
        )
        assert result is None


class TestClassifyEntryPoint:
    def test_unknown_when_no_rule_matches(self) -> None:
        verdict, reason = classify(
            "Gana Boca Juniors",
            "Empate o gana River Plate",
            REGULAR_SEASON,
        )
        assert verdict == Verdict.UNKNOWN
        assert reason == "no rule matched"

    def test_returns_reason_string_when_rule_fires(self) -> None:
        verdict, reason = classify(
            "BTTS Yes",
            "BTTS No",
            REGULAR_SEASON,
        )
        assert verdict == Verdict.VALID
        assert reason  # non-empty
        assert "yes/no" in reason or "complement" in reason

    @pytest.mark.parametrize(
        "desc_a,desc_b",
        [
            ("Ambos equipos marcan: Sí", "Ambos equipos marcan: No"),
            ("Boca gana al medio tiempo", "Boca no gana al medio tiempo"),
        ],
    )
    def test_simple_valid_pairs(self, desc_a: str, desc_b: str) -> None:
        verdict, _ = classify(desc_a, desc_b, REGULAR_SEASON)
        assert verdict == Verdict.VALID

    def test_context_passed_through(self) -> None:
        """Smoke check that context-dependent rules will see the context.

        Both rules implemented so far are context-independent, so we just
        verify the call doesn't blow up. Future rules will actually consume
        the knockout flag.
        """
        verdict_regular, _ = classify("BTTS Yes", "BTTS No", REGULAR_SEASON)
        verdict_knockout, _ = classify("BTTS Yes", "BTTS No", KNOCKOUT_R16)
        assert verdict_regular == verdict_knockout == Verdict.VALID
