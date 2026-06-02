"""Tests for per-platform placement confirmation parsers.

Responses mirror the real captured place/confirm bodies (2026-06-02). The
critical assertions are on `accepted` — the executor's naked-exposure logic
depends on it being right and fail-closed.
"""

from __future__ import annotations

from src.execution.placers import (
    parse_betano,
    parse_betsson,
    parse_betwarrior,
    parse_bplay,
    parse_confirmation,
)

# ---- accepted (success) ----


def test_betano_accepted() -> None:
    r = parse_betano(
        {
            "data": {
                "accepted": True,
                "receipts": [{"betId": "20016095047", "totalAmount": 1000.0, "totalOdds": 3.65}],
            }
        }
    )
    assert r.accepted
    assert r.ref == "20016095047" and r.stake_filled == 1000.0 and r.odds_filled == 3.65


def test_bplay_accepted() -> None:
    r = parse_bplay({"return": "OK", "message": {"type": "success", "message": "Apuesta colocada"}})
    assert r.accepted


def test_betwarrior_accepted_normalizes_kambi_units() -> None:
    r = parse_betwarrior(
        {
            "status": "SUCCESS",
            "couponRef": 12713234454,
            "coupon": {"bets": [{"betOdds": 1400, "stake": 1000000}]},
        }
    )
    assert r.accepted
    assert r.ref == "12713234454"
    assert r.odds_filled == 14.0  # ×100
    assert r.stake_filled == 1000.0  # ×1000


def test_betsson_accepted() -> None:
    r = parse_betsson(
        {
            "couponStatus": {
                "couponStatusPollingResult": "Success",
                "couponId": "179282050977941504",
                "couponPlacementErrors": [],
            }
        }
    )
    assert r.accepted and r.ref == "179282050977941504"


# ---- rejected / fail-closed ----


def test_betano_not_accepted() -> None:
    assert not parse_betano({"data": {"accepted": False}}).accepted


def test_bplay_non_ok_rejected() -> None:
    assert not parse_bplay(
        {"return": "ERROR", "message": {"type": "error", "message": "saldo"}}
    ).accepted


def test_betwarrior_non_success_rejected() -> None:
    assert not parse_betwarrior({"status": "REJECTED"}).accepted


def test_betsson_placement_errors_rejected() -> None:
    r = parse_betsson(
        {
            "couponStatus": {
                "couponStatusPollingResult": "Success",
                "couponPlacementErrors": ["ODDS_CHANGED"],
            }
        }
    )
    assert not r.accepted  # errors present → fail closed even if polling "Success"


def test_unrecognized_shapes_fail_closed() -> None:
    # Empty / garbage responses must never read as accepted.
    for resp in ({}, {"data": {}}, {"foo": "bar"}, {"couponStatus": {}}):
        assert not parse_betano(resp).accepted
        assert not parse_betsson(resp).accepted
    assert not parse_bplay({}).accepted
    assert not parse_betwarrior({}).accepted


# ---- dispatcher ----


def test_dispatcher_matches_suffixed_platform() -> None:
    r = parse_confirmation(
        "betsson-pba",
        {
            "couponStatus": {
                "couponStatusPollingResult": "Success",
                "couponId": "x",
                "couponPlacementErrors": [],
            }
        },
    )
    assert r.accepted and r.ref == "x"


def test_dispatcher_unknown_platform_fails_closed() -> None:
    assert not parse_confirmation("mystery", {"anything": True}).accepted
