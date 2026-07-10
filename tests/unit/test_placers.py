"""Tests for per-platform placement confirmation parsers.

Responses mirror the real captured place/confirm bodies (2026-06-02). The
critical assertions are on `accepted` — the executor's naked-exposure logic
depends on it being right and fail-closed.
"""

from __future__ import annotations

import uuid
from typing import Any

from src.execution.placers import (
    BetHistoryMatch,
    betsson_odds_correction,
    betwarrior_odds_rejected,
    build_betano_request,
    build_betsson_request,
    build_betwarrior_request,
    build_bplay_request,
    match_betwarrior_coupon,
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
    assert r.odds_filled == 1.4  # ×1000 (confirmed by live placement)
    assert r.stake_filled == 1000.0  # ×1000


def test_betwarrior_success_without_echoed_bet_rejected() -> None:
    # A bare SUCCESS with no echoed coupon.bets is NOT a placement — the live-delay
    # poll reuses this strict gate, so accepting on status alone could mark an
    # unplaced bet as placed (blind naked exposure).
    r = parse_betwarrior({"status": "SUCCESS", "couponRef": 999, "coupon": {"bets": [{}]}})
    assert not r.accepted and "without echoed" in r.detail


def test_match_betwarrior_coupon_open_accepted() -> None:
    match, bet = match_betwarrior_coupon(
        {
            "historyCoupons": [
                {
                    "couponRef": 777,
                    "bets": [{"betStatus": "OPEN", "stake": 500000, "betOdds": 1410}],
                }
            ]
        },
        coupon_ref=777,
    )
    assert match is BetHistoryMatch.ACCEPTED
    assert bet["stake"] == 500000


def test_match_betwarrior_coupon_waiting() -> None:
    match, _ = match_betwarrior_coupon(
        {"historyCoupons": [{"couponRef": 777, "bets": [{"betStatus": "WAITING_FOR_APPROVAL"}]}]},
        coupon_ref=777,
    )
    assert match is BetHistoryMatch.WAITING


def test_match_betwarrior_coupon_rejected() -> None:
    match, _ = match_betwarrior_coupon(
        {"historyCoupons": [{"couponRef": 777, "bets": [{"betStatus": "REFUSED"}]}]},
        coupon_ref=777,
    )
    assert match is BetHistoryMatch.REJECTED


def test_match_betwarrior_coupon_not_found_is_unknown() -> None:
    # Coupon absent (not propagated / resolved+gone) → keep polling → pending_unknown.
    match, bet = match_betwarrior_coupon({"historyCoupons": []}, coupon_ref=777)
    assert match is BetHistoryMatch.UNKNOWN and bet == {}


def test_match_betwarrior_coupon_bet_ref_fallback() -> None:
    # couponRef doesn't match, but a bet's betRef does → still found.
    match, _ = match_betwarrior_coupon(
        {
            "historyCoupons": [
                {
                    "couponRef": 0,
                    "bets": [{"betStatus": "OPEN", "betRef": 42, "stake": 1, "betOdds": 1}],
                }
            ]
        },
        coupon_ref=777,
        bet_ref=42,
    )
    assert match is BetHistoryMatch.ACCEPTED


def test_match_betwarrior_coupon_bet_ref_classifies_matched_not_first() -> None:
    # Multi-bet coupon: bets[0] is OPEN but OUR betRef is on bets[1] which is still
    # WAITING. Must classify the matched bet (WAITING), NOT bets[0] (OPEN) — the
    # catastrophic case where a sibling bet's acceptance is mistaken for ours.
    match, bet = match_betwarrior_coupon(
        {
            "historyCoupons": [
                {
                    "couponRef": 777,
                    "bets": [
                        {"betStatus": "OPEN", "betRef": 1, "stake": 1, "betOdds": 1},
                        {"betStatus": "WAITING_FOR_APPROVAL", "betRef": 42},
                    ],
                }
            ]
        },
        coupon_ref=777,
        bet_ref=42,
    )
    assert match is BetHistoryMatch.WAITING
    assert bet.get("betRef") == 42


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


# ---- Betsson favorable odds correction (price-confirmation handshake) ----


def _odds_invalid_resp(
    valid_odds: object = "4.45",
    tag: str = "s-m-f-WB0IdcdAsEeygVTwHrJMtA-MW3W-away",
    coupon_id: str = "",
) -> dict[str, Any]:
    """The verbatim live reject shape (fx-d11b22fcc7f2, events.jsonl:17)."""
    return {
        "couponStatus": {
            "couponStatusPollingResult": "Failure",
            "couponId": coupon_id,
            "couponPlacementErrors": [
                {
                    "code": "E_BETTING_ODDS_INVALID",
                    "details": {
                        "marketSelectionTag": tag,
                        "validOdds": valid_odds,
                        "combinedMarketSelections": "",
                    },
                }
            ],
        }
    }


def test_betsson_odds_correction_present() -> None:
    # The live fx-d11b22fcc7f2 reject: validOdds 4.45 over a 4.35 submit.
    c = betsson_odds_correction(_odds_invalid_resp("4.45"))
    assert c is not None
    assert c.valid_odds == 4.45
    assert c.valid_odds_str == "4.45"  # exact string → re-submitted verbatim
    assert c.selection_tag == "s-m-f-WB0IdcdAsEeygVTwHrJMtA-MW3W-away"


def test_betsson_odds_correction_accepts_success_is_none() -> None:
    # An accepted coupon is never an odds correction → never re-POST.
    assert (
        betsson_odds_correction(
            {"couponStatus": {"couponStatusPollingResult": "Success", "couponPlacementErrors": []}}
        )
        is None
    )


def test_betsson_odds_correction_non_odds_error_is_none() -> None:
    # A non-odds code is not a price-confirmation handshake.
    assert (
        betsson_odds_correction(
            {
                "couponStatus": {
                    "couponStatusPollingResult": "Failure",
                    "couponPlacementErrors": [{"code": "E_BETTING_COUPON_GENERAL"}],
                }
            }
        )
        is None
    )


def test_betsson_odds_correction_legacy_string_error_is_none() -> None:
    # Legacy string errors (["X"], ["ODDS_CHANGED"]) are skipped → None (no behavior
    # change for those — the isinstance(err, dict) guard).
    assert (
        betsson_odds_correction(
            {
                "couponStatus": {
                    "couponStatusPollingResult": "Failure",
                    "couponPlacementErrors": ["X"],
                }
            }
        )
        is None
    )


def test_betsson_odds_correction_unparseable_is_none() -> None:
    # Missing / non-numeric validOdds → None.
    assert betsson_odds_correction(_odds_invalid_resp(valid_odds=None)) is None
    assert betsson_odds_correction(_odds_invalid_resp(valid_odds="abc")) is None


def test_betsson_odds_correction_coupon_id_present_is_none() -> None:
    # A coupon may have been created → never re-POST over it. The parser fails closed.
    assert betsson_odds_correction(_odds_invalid_resp("4.45", coupon_id="C9")) is None


def test_betsson_odds_correction_zero_coupon_id_still_corrects() -> None:
    # couponId == "0" (Betsson's "no coupon" sentinel) → still a correction.
    c = betsson_odds_correction(_odds_invalid_resp("4.45", coupon_id="0"))
    assert c is not None and c.valid_odds == 4.45


# ---- BetWarrior (Kambi) invalid-odds reject classification ----


def test_betwarrior_odds_rejected_message_key() -> None:
    # 2026-06-10 live capture shape: {"message": "Invalid odds specified"}.
    assert betwarrior_odds_rejected({"message": "Invalid odds specified"}) is True


def test_betwarrior_odds_rejected_reason_key() -> None:
    # Recon/test shape: the same literal under the "reason" key.
    assert betwarrior_odds_rejected({"reason": "Invalid odds specified"}) is True


def test_betwarrior_odds_rejected_case_and_whitespace_insensitive() -> None:
    assert betwarrior_odds_rejected({"message": "  invalid ODDS specified "}) is True


def test_betwarrior_odds_rejected_non_odds_error_is_false() -> None:
    # Unrelated 400s (funds, validation) must never classify as an odds move.
    assert betwarrior_odds_rejected({"reason": "Insufficient funds"}) is False
    assert betwarrior_odds_rejected({"message": "Stake below minimum"}) is False


def test_betwarrior_odds_rejected_empty_body_is_false() -> None:
    assert betwarrior_odds_rejected({}) is False


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
        stake_ars=500.0,
        csrf_token="CSRF",
        date_ms=1780434223302,
    )
    bs = req["data"]["data"]["betslip"]
    assert bs["stake"] == {"6621121460": 500}  # whole ARS ×1 (confirmed by live capture)
    assert bs["nb_bettingslip_totalStake"] == "1.00" and bs["accept"] is True  # line count
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
