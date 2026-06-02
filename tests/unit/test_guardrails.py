"""Tests for the execution guardrails (financial safety layer)."""

from __future__ import annotations

from src.execution.guardrails import Guardrails


def _g(**over: float) -> Guardrails:
    base = {
        "max_position_per_match_ars": 1000.0,
        "max_total_exposure_ars": 2500.0,
        "max_daily_loss_ars": 500.0,
        "odds_tolerance_pct": 1.0,
    }
    base.update(over)
    return Guardrails(**base)  # type: ignore[arg-type]


def test_allows_within_all_caps() -> None:
    g = _g()
    r = g.check_leg(platform="betsson", match_id="m1", stake_ars=500.0, decimal_odds=2.0)
    assert r.allowed and r.reason == ""


def test_rejects_non_positive_stake() -> None:
    assert (
        not _g()
        .check_leg(platform="betsson", match_id="m1", stake_ars=0.0, decimal_odds=2.0)
        .allowed
    )


def test_kill_switch_blocks_everything() -> None:
    g = _g()
    g.trip_kill_switch("manual")
    r = g.check_leg(platform="betsson", match_id="m1", stake_ars=10.0, decimal_odds=2.0)
    assert not r.allowed and "kill switch" in r.reason


def test_per_match_cap_enforced_with_accumulation() -> None:
    g = _g()
    g.record_exposure("m1", 800.0)
    r = g.check_leg(platform="betsson", match_id="m1", stake_ars=300.0, decimal_odds=2.0)
    assert not r.allowed and "per-match" in r.reason  # 800+300 > 1000


def test_total_exposure_cap_enforced() -> None:
    g = _g()
    g.record_exposure("m1", 900.0)
    g.record_exposure("m2", 900.0)
    g.record_exposure("m3", 700.0)  # total 2500 (at cap)
    r = g.check_leg(platform="betsson", match_id="m4", stake_ars=1.0, decimal_odds=2.0)
    assert not r.allowed and "total exposure" in r.reason


def test_dynamic_platform_denied_without_live_cap() -> None:
    # Betano is dynamic — without a live cap the leg is fail-closed.
    r = _g().check_leg(platform="betano", match_id="m1", stake_ars=10.0, decimal_odds=2.0)
    assert not r.allowed and "dynamic" in r.reason


def test_dynamic_platform_allowed_with_live_cap() -> None:
    g = _g()
    r = g.check_leg(
        platform="betano",
        match_id="m1",
        stake_ars=100.0,
        decimal_odds=2.0,
        live_max_stake_ars=500.0,
    )
    assert r.allowed


def test_live_cap_tightens_static_cap() -> None:
    g = _g()
    # Betsson static cap is huge; a live cap below the stake denies.
    r = g.check_leg(
        platform="betsson",
        match_id="m1",
        stake_ars=300.0,
        decimal_odds=2.0,
        live_max_stake_ars=200.0,
    )
    assert not r.allowed and "cap" in r.reason


def test_odds_tolerance_directional() -> None:
    g = _g(odds_tolerance_pct=1.0)
    assert g.odds_still_acceptable(2.00, 2.00)
    assert g.odds_still_acceptable(2.00, 2.10)  # rose → fine
    assert g.odds_still_acceptable(2.00, 1.99)  # -0.5% within 1%
    assert not g.odds_still_acceptable(2.00, 1.95)  # -2.5% drop → abort


def test_daily_loss_auto_trips_kill_switch() -> None:
    g = _g(max_daily_loss_ars=500.0)
    g.record_settlement(-300.0)
    assert not g.kill_switch_tripped
    g.record_settlement(-250.0)  # cumulative -550 >= 500
    assert g.kill_switch_tripped


def test_release_exposure_frees_room() -> None:
    g = _g()
    g.record_exposure("m1", 1000.0)
    g.release_exposure("m1", 1000.0)
    assert g.total_exposure_ars == 0.0
    assert g.check_leg(platform="betsson", match_id="m1", stake_ars=900.0, decimal_odds=2.0).allowed
