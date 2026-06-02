"""Tests for the stateless LegPlacers (build → send → parse) via a fake transport.

The real in-session send is validated in the trial; here we assert the wiring:
the placer builds the right request to the right endpoint, and turns the
platform's confirmation into the right `PlacementResult`.
"""

from __future__ import annotations

from typing import Any

from src.execution.executor import Leg
from src.execution.leg_placer import BetssonLegPlacer, BetWarriorLegPlacer


class FakeTransport:
    """Records the request it's handed and returns a canned (status, body)."""

    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body or {}
        self.calls: list[dict[str, Any]] = []

    async def fetch(self, method, url, *, json_body=None, headers=None):  # type: ignore[no-untyped-def]
        self.calls.append({"method": method, "url": url, "json": json_body, "headers": headers})
        return self.status, self.body


def _leg(platform: str, outcome_id: str, stake: float, odds: float) -> Leg:
    return Leg(
        platform=platform,
        match_id="m1",
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
    call = t.calls[0]
    assert call["url"].endswith("/api/sb/v2/coupons")
    assert call["headers"]["brandid"] and call["headers"]["x-sb-type"] == "b2b"
    sel = call["json"]["bets"][0]["betSelections"][0]
    assert sel == {"marketSelectionId": "s-m-f-EVT-MW3W-home", "odds": "1.41"}
    assert call["json"]["bets"][0]["stake"] == 500.0


async def test_betwarrior_placer_builds_request_and_parses_success() -> None:
    t = FakeTransport(
        200,
        {
            "status": "SUCCESS",
            "couponRef": 999,
            "coupon": {"bets": [{"betOdds": 141, "stake": 500000}]},
        },
    )
    res = await BetWarriorLegPlacer(t, auth_token="TOK").place(
        _leg("betwarrior-pba", "4206111729", 500.0, 1.41)
    )
    assert res.accepted and res.ref == "999"
    call = t.calls[0]
    assert call["url"].endswith("/coupon.json")
    assert call["headers"]["authorization"] == "Bearer TOK"
    assert call["json"]["couponRows"][0]["outcomeId"] == 4206111729
    assert call["json"]["couponRows"][0]["odds"] == 141  # 1.41 ×100
    assert call["json"]["bets"][0]["stake"] == 500000  # 500.0 ×1000


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
        async def fetch(self, *a: Any, **k: Any) -> tuple[int, dict[str, Any]]:
            from src.execution.session import TransportError

            raise TransportError("not armed")

    res = await BetssonLegPlacer(BoomTransport()).place(_leg("betsson-pba", "s-x", 10.0, 2.0))
    assert not res.accepted and "transport" in res.detail
