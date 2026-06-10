"""Tests for the stateless LegPlacers (build → send → parse) via a fake transport.

The real in-session send is validated in the trial; here we assert the wiring:
the placer builds the right request to the right endpoint, and turns the
platform's confirmation into the right `PlacementResult`.
"""

from __future__ import annotations

from typing import Any

from src.execution.executor import Leg
from src.execution.leg_placer import (
    BetanoLegPlacer,
    BetssonLegPlacer,
    BetWarriorLegPlacer,
    BplayLegPlacer,
)

_FAKE_CTX = {"x-sb-user-context-id": "ctx-1", "sessiontoken": "JWT", "brandid": "B"}


class FakeTransport:
    """Records the request it's handed and returns a canned (status, body).
    For Betsson, `ctx_headers` is what prepare_betsson_context returns (None to
    simulate a not-logged-in / unresolved context)."""

    def __init__(
        self,
        status: int = 200,
        body: dict[str, Any] | None = None,
        ctx_headers: dict[str, str] | None = _FAKE_CTX,
        bearer: str | None = "TOK",
    ) -> None:
        self.status = status
        self.body = body or {}
        self.ctx_headers = ctx_headers
        self.bearer = bearer
        self.calls: list[dict[str, Any]] = []
        self.prepared = 0

    async def prepare_betsson_context(self) -> dict[str, str] | None:
        self.prepared += 1
        return self.ctx_headers

    async def prepare_betwarrior_auth(self) -> str | None:
        self.prepared += 1
        return self.bearer

    async def fetch(self, method, url, *, json_body=None, headers=None):  # type: ignore[no-untyped-def]
        self.calls.append({"method": method, "url": url, "json": json_body, "headers": headers})
        return self.status, self.body


class SeqTransport:
    """Returns a queued (status, body) per call — for the multi-step stateful flows."""

    def __init__(self, responses: list[tuple[int, dict[str, Any]]]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def fetch(self, method, url, *, json_body=None, headers=None):  # type: ignore[no-untyped-def]
        self.calls.append({"method": method, "url": url, "json": json_body, "headers": headers})
        return self._responses[len(self.calls) - 1]


def _leg(
    platform: str, outcome_id: str, stake: float, odds: float, event_ref: str = "futbol/x-vs-y"
) -> Leg:
    return Leg(
        platform=platform,
        match_id="m1",
        market="1X2",
        outcome="home",
        stake_ars=stake,
        odds=odds,
        platform_outcome_id=outcome_id,
        platform_event_ref=event_ref,
    )


def _leg_betano(outcome_id: str, event_id: str, stake: float, odds: float) -> Leg:
    """Betano maps match_id -> eventId, so set it explicitly."""
    return Leg(
        platform="betano-pba",
        match_id=event_id,
        market="1X2",
        outcome="home",
        stake_ars=stake,
        odds=odds,
        platform_outcome_id=outcome_id,
    )


async def test_betsson_placer_builds_request_and_parses_success() -> None:
    t = FakeTransport(
        200,
        {
            "couponStatus": {
                "couponStatusPollingResult": "Success",
                "couponId": "C1",
                "couponPlacementErrors": [],
            }
        },
    )
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-m-f-EVT-MW3W-home", 500.0, 1.41))
    assert res.accepted and res.ref == "C1"
    assert t.prepared == 1  # resolved the authenticated context (no navigation/reload)
    call = t.calls[0]
    assert call["url"].endswith("/api/sb/v2/coupons")
    # the live authenticated context headers are replayed onto the place call
    assert call["headers"]["x-sb-user-context-id"] == "ctx-1"
    assert call["headers"]["sessiontoken"] == "JWT"
    assert call["headers"]["x-sb-identifier"] == "BETSLIP_SUBMIT_COUPONS_REQUEST"
    sel = call["json"]["bets"][0]["betSelections"][0]
    assert sel == {"marketSelectionId": "s-m-f-EVT-MW3W-home", "odds": "1.41"}
    assert call["json"]["bets"][0]["stake"] == 500.0
    # updateSources is present and correctly shaped (the E_BETTING_COUPON_GENERAL fix)
    us = call["json"]["updateSources"]
    assert "s-m-f-EVT-MW3W-home" in us["odds"]["selections"]
    assert "m-f-EVT-MW3W" in us["statuses"]["markets"]


async def test_betsson_fills_exposure_from_requested_when_not_echoed() -> None:
    # Betsson's response echoes no stake/odds → fall back to requested (for exposure).
    t = FakeTransport(
        200,
        {"couponStatus": {"couponStatusPollingResult": "Success", "couponId": "C1",
                          "couponPlacementErrors": []}},
    )
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-x", 50.0, 2.62))
    assert res.accepted and res.stake_filled == 50.0 and res.odds_filled == 2.62


async def test_betsson_fails_closed_when_context_not_resolved() -> None:
    # prepare_betsson_context returns None → not logged in → no place attempt.
    t = FakeTransport(ctx_headers=None)
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-x", 50.0, 2.62))
    assert not res.accepted and "context not resolved" in res.detail
    assert not t.calls  # never POSTed


async def test_betwarrior_placer_builds_request_and_parses_success() -> None:
    t = FakeTransport(
        200,
        {
            "status": "SUCCESS",
            "couponRef": 999,
            "coupon": {"bets": [{"betOdds": 141, "stake": 500000}]},
        },
    )
    res = await BetWarriorLegPlacer(t).place(_leg("betwarrior-pba", "4206111729", 500.0, 1.41))
    assert res.accepted and res.ref == "999"
    call = t.calls[0]
    assert call["url"].endswith("/coupon.json")
    assert call["headers"]["authorization"] == "Bearer TOK"  # bearer read from the transport
    assert call["json"]["couponRows"][0]["outcomeId"] == 4206111729
    assert call["json"]["couponRows"][0]["odds"] == 141  # 1.41 ×100
    assert call["json"]["bets"][0]["stake"] == 500000  # 500.0 ×1000


async def test_betwarrior_fails_closed_when_bearer_not_captured() -> None:
    t = FakeTransport(bearer=None)  # not logged in → no session bearer
    res = await BetWarriorLegPlacer(t).place(_leg("betwarrior-pba", "42", 500.0, 1.41))
    assert not res.accepted and "bearer not captured" in res.detail
    assert not t.calls  # never POSTed


async def test_betano_runs_slip_sequence_and_places_with_refreshed_hash() -> None:
    plain_leg = {
        "data": {
            "hash": "H1",
            "slipData": "H1",
            "betslipTrackId": "T",
            "legs": [{"id": "9698869897"}],
            "bets": [{"id": "1:SGL:9698869897", "odds": 3.65, "amount": 0}],
        }
    }
    updatebets = {
        "data": {
            "hash": "H2",
            "slipData": "H2",
            "betslipTrackId": "T",
            "legs": [{"id": "9698869897"}],
            "bets": [{"id": "1:SGL:9698869897", "odds": 3.65, "amount": 0}],
        }
    }
    place = {
        "data": {
            "accepted": True,
            "receipts": [{"betId": "B9", "totalAmount": 1000, "totalOdds": 3.65}],
        }
    }
    t = SeqTransport([(200, plain_leg), (200, updatebets), (200, place)])
    res = await BetanoLegPlacer(t).place(_leg_betano("9698869897", "86489358", 1000.0, 3.65))
    assert res.accepted and res.ref == "B9" and res.stake_filled == 1000
    methods = [
        (c["method"], c["url"].rsplit("/", 1)[-1] or c["url"].rsplit("/", 2)[-2]) for c in t.calls
    ]
    assert methods == [("POST", "plain-leg"), ("PATCH", "updatebets"), ("POST", "place")]
    # updatebets carries a TOP-LEVEL bets array with the amount set (else 400)
    ub_body = t.calls[1]["json"]
    assert ub_body["bets"][0]["amount"] == 1000.0 and ub_body["bets"][0]["returns"] == 0
    assert "betslip" in ub_body
    place_body = t.calls[2]["json"]["betslip"]
    assert place_body["hash"] == "H2"  # the refreshed hash from updatebets, not plain-leg's H1
    assert place_body["bets"][0]["amount"] == 1000.0
    assert place_body["bets"][0]["returns"] == 3650.0
    assert place_body["oddschanges"] == "0"


async def test_bplay_threads_rotated_csrf_and_scales_stake() -> None:
    togglebet = {"header": {"csrf_token": "C1"}, "body": {}, "footer": {}}
    place = {"return": "OK", "message": {"type": "success", "message": "Apuesta colocada"}}
    t = SeqTransport([(200, togglebet), (200, place)])

    async def bootstrap() -> str:
        return "C0"

    res = await BplayLegPlacer(
        t, event_url_key="/eventos/10595536-francia-senegal", bootstrap_csrf=bootstrap
    ).place(_leg("bplay-pba", "6621121460", 1.0, 1.5))
    assert res.accepted
    toggle_body = t.calls[0]["json"]
    assert toggle_body["data"] == {"id": 6621121460, "csrf_token": "C0"}  # bootstrap token
    place_body = t.calls[1]["json"]
    assert place_body["data"]["csrf_token"] == "C1"  # rotated forward from togglebet response
    assert place_body["data"]["data"]["betslip"]["stake"] == {"6621121460": 1000}  # ×1000
    assert place_body["context"]["url_key"] == "/eventos/10595536-francia-senegal"


async def test_http_error_is_not_accepted() -> None:
    res = await BetssonLegPlacer(FakeTransport(403, {})).place(
        _leg("betsson-pba", "s-x", 10.0, 2.0)
    )
    assert not res.accepted and "403" in res.detail


async def test_rejection_confirmation_is_not_accepted() -> None:
    t = FakeTransport(
        200,
        {"couponStatus": {"couponStatusPollingResult": "Failure", "couponPlacementErrors": ["X"]}},
    )
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-x", 10.0, 2.0))
    assert not res.accepted


async def test_transport_error_returns_not_accepted_not_raise() -> None:
    class BoomTransport:
        async def prepare_betsson_context(self) -> dict[str, str] | None:
            return _FAKE_CTX

        async def fetch(self, *a: Any, **k: Any) -> tuple[int, dict[str, Any]]:
            from src.execution.session import TransportError

            raise TransportError("not armed")

    res = await BetssonLegPlacer(BoomTransport()).place(_leg("betsson-pba", "s-x", 10.0, 2.0))
    assert not res.accepted and "transport" in res.detail
