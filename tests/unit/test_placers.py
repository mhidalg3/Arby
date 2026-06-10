"""Tests for per-platform placement confirmation parsers.

Responses mirror the real captured place/confirm bodies (2026-06-02). The
critical assertions are on `accepted` — the executor's naked-exposure logic
depends on it being right and fail-closed.
"""

from __future__ import annotations

import uuid

from src.execution.placers import (
    build_betano_request,
    build_betsson_request,
    build_betwarrior_request,
    build_bplay_request,
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


# ---- request builders (verified against captured place bodies) ----


def test_build_betsson_single_leg() -> None:
    req = build_betsson_request([("s-m-f-EVT-MW3W-home", "1.41")], 500.0)
    bet = req["bets"][0]
    assert bet["stake"] == 500.0 and bet["currencyCode"] == "ARS" and bet["oddsFormat"] == 1
    assert bet["betSelections"] == [{"marketSelectionId": "s-m-f-EVT-MW3W-home", "odds": "1.41"}]
    assert req["acceptOddsChanges"] is True


def test_build_betwarrior_units_and_generated_request_id() -> None:
    req = build_betwarrior_request(
        outcome_id=4206111729, odds_x1000=14000, stake_thousandths=1_000_000
    )
    assert req["couponRows"][0] == {
        "index": 0,
        "odds": 14000,
        "outcomeId": 4206111729,
        "type": "SIMPLE",
    }
    assert req["bets"][0]["stake"] == 1_000_000 and req["channel"] == "WEB"
    assert req["allowOddsChange"] == "NO"
    uuid.UUID(req["requestId"])  # a valid uuid was generated


def test_build_betwarrior_explicit_request_id() -> None:
    assert (
        build_betwarrior_request(
            outcome_id=1, odds_x1000=2000, stake_thousandths=1000, request_id="fixed"
        )["requestId"]
        == "fixed"
    )


def test_build_bplay_stake_keyed_by_outcome() -> None:
    req = build_bplay_request(
        url_key="/eventos/123-a-b",
        outcome_id=6621121460,
        stake_ars=1.0,
        csrf_token="CSRF",
        date_ms=1780434223302,
    )
    bs = req["data"]["data"]["betslip"]
    assert bs["stake"] == {"6621121460": 1000}  # thousandths of ARS (capture: 1.00 ↔ 1000)
    assert bs["nb_bettingslip_totalStake"] == "1.00" and bs["accept"] is True
    assert req["data"]["csrf_token"] == "CSRF"
    assert req["context"]["url_key"] == "/eventos/123-a-b"


def test_build_betano_fills_stake_and_returns_preserving_slip_state() -> None:
    slip = {
        "hash": "H==.1|#x",
        "slipData": "H==.1|#x",
        "betslipTrackId": "track-1",
        "legs": [{"eventId": "86489358", "tag": "9698869897", "odds": 3.65}],
        "bets": [
            {"id": "1:SGL:9698869897", "tag": "9698869897", "odds": 3.65, "amount": 0, "returns": 0}
        ],
    }
    req = build_betano_request(slip, 1000.0)
    bet = req["betslip"]["bets"][0]
    assert bet["amount"] == 1000.0 and bet["returns"] == 3650.0
    assert req["betslip"]["oddschanges"] == "0"
    assert req["betslip"]["hash"] == "H==.1|#x"  # slip state preserved
    # input slip not mutated (deep-copied)
    assert slip["bets"][0]["amount"] == 0
