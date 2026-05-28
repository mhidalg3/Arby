"""Unit tests for team-name normalization and similarity.

The tests are anchored on REAL team-name strings observed during
the 30s 3-platform Redis ingestion run on 2026-05-26. When this file
needs new cases, prefer pulling them from the live snapshot stream
rather than inventing strings.
"""

from __future__ import annotations

import pytest

from src.semantic.team_normalize import normalize_team_name, team_similarity


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", ""),
            ("LDU Quito", "ldu quito"),
            ("Always Ready", "always ready"),
            ("Mirassol-SP", "mirassol sp"),
            ("Mirassol FC SP", "mirassol fc sp"),
            ("Atlético Mineiro-MG", "atletico mineiro mg"),
            ("América de Cali", "america de cali"),
            ("Belgrano", "belgrano"),
            ("Defensa Y Justicia Reserve", "defensa y justicia reserve"),
            ("Paris SG", "paris sg"),
            ("Paris Saint-Germain", "paris saint germain"),
            ("  trailing  whitespace  ", "trailing whitespace"),
            ("Argentinos Jrs Reserves", "argentinos jrs reserves"),
            # Suffixes are NOT stripped — they distinguish real teams.
            ("CA San Miguel Reserves", "ca san miguel reserves"),
        ],
    )
    def test_normalize_examples(self, raw: str, expected: str) -> None:
        assert normalize_team_name(raw) == expected

    def test_idempotent(self) -> None:
        once = normalize_team_name("Atlético Mineiro-MG")
        twice = normalize_team_name(once)
        assert once == twice


class TestSimilarity:
    def test_exact_match_after_normalize_is_one(self) -> None:
        assert team_similarity("Mirassol", "mirassol") == 1.0
        assert team_similarity("Atlético Mineiro", "atletico mineiro") == 1.0

    def test_empty_returns_zero(self) -> None:
        assert team_similarity("", "anything") == 0.0
        assert team_similarity("anything", "") == 0.0

    def test_kambi_vs_sportnco_close_variants_match(self) -> None:
        """Real recon case: Kambi `"Mirassol-SP"` vs SportNCO
        `"Mirassol FC SP"`. After normalization the gap is the `fc`
        token — similarity should clear the 0.85 strict threshold."""
        score = team_similarity("Mirassol-SP", "Mirassol FC SP")
        assert score >= 0.85

    def test_reserves_vs_first_team_does_not_match(self) -> None:
        """The discriminator test: `Belgrano` and `Belgrano Reserves`
        are different teams. Similarity must NOT clear 0.85."""
        score = team_similarity("Belgrano", "Belgrano Reserves")
        assert score < 0.85

    def test_paris_sg_vs_paris_saint_germain_is_partial(self) -> None:
        """The Paris case is borderline — similarity is moderate but
        below the strict threshold. This is the kind of case the
        fixture resolver should escalate to LLM."""
        score = team_similarity("Paris SG", "Paris Saint-Germain")
        # Should be in the ambiguous band, not strict-pass and not zero.
        assert 0.3 < score < 0.85

    def test_completely_different_teams_low_score(self) -> None:
        assert team_similarity("Boca Juniors", "River Plate") < 0.5
