"""Tests for the per-platform stake-limit model (recon 2026-06-01)."""

from __future__ import annotations

import pytest

from src.risk.stake_limits import (
    DEFAULT_MAX_STAKE_PER_LEG_ARS,
    effective_max_stake_ars,
    is_dynamic,
)


def test_betsson_flat_cap_binds_at_low_odds() -> None:
    # min(20M, 100M/odds): at odds 2, payout/odds = 50M, so the 20M flat binds.
    assert effective_max_stake_ars("betsson", 2.0) == 20_000_000.0


def test_betsson_payout_cap_binds_at_high_odds() -> None:
    # at odds 10, payout/odds = 10M < 20M flat → payout cap binds.
    assert effective_max_stake_ars("betsson", 10.0) == pytest.approx(10_000_000.0)


def test_betsson_breakeven_at_odds_5() -> None:
    assert effective_max_stake_ars("betsson", 5.0) == pytest.approx(20_000_000.0)


def test_betano_is_dynamic_returns_none() -> None:
    assert is_dynamic("betano") is True
    assert effective_max_stake_ars("betano", 1.5) is None


def test_bplay_payout_cap_per_odds() -> None:
    assert effective_max_stake_ars("bplay", 1.09) == pytest.approx(999_999_999.0 / 1.09)
    assert effective_max_stake_ars("bplay", 19.0) == pytest.approx(999_999_999.0 / 19.0)


def test_betwarrior_no_cap_uses_fallback() -> None:
    assert effective_max_stake_ars("betwarrior", 3.0) == DEFAULT_MAX_STAKE_PER_LEG_ARS
    assert is_dynamic("betwarrior") is False


def test_suffixed_platform_names_match_base() -> None:
    assert effective_max_stake_ars("betsson-pba", 2.0) == 20_000_000.0
    assert is_dynamic("betano-live") is True


def test_unknown_platform_uses_fallback() -> None:
    assert effective_max_stake_ars("mystery", 2.0) == DEFAULT_MAX_STAKE_PER_LEG_ARS


def test_custom_fallback_respected() -> None:
    assert effective_max_stake_ars("betwarrior", 2.0, fallback=12_345.0) == 12_345.0
