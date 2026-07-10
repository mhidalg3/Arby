"""Tests for LiveOddsReverifier — the executor's live re-verify hook (fail-closed)."""

from __future__ import annotations

from src.arbitrage.quotes import OddsQuote
from src.execution.executor import Leg
from src.execution.reverify import BetanoCapRefresher, LiveOddsReverifier
from src.execution.session import TransportError
from src.risk.verifier import FreshQuote


class _FakeRefresher:
    platform_name = "betano"

    def __init__(self, odds: float | None, *, raise_exc: bool = False) -> None:
        self.odds = odds
        self.raise_exc = raise_exc

    async def refresh(self, leg: OddsQuote) -> FreshQuote:
        if self.raise_exc:
            raise RuntimeError("refetch boom")
        return FreshQuote(
            platform=leg.platform,
            platform_outcome_id=leg.platform_outcome_id,
            decimal_odds=self.odds,
            observed_at=0.0,
            tier=2,
        )


def _leg(platform: str = "betano") -> Leg:
    return Leg(
        platform=platform,
        match_id="m",
        market="1X2",
        outcome="home",
        stake_ars=50.0,
        odds=2.0,
        platform_outcome_id="o1",
        platform_event_ref="e1",
    )


async def test_returns_fresh_live_odds() -> None:
    rv = LiveOddsReverifier({"betano": _FakeRefresher(1.95)})  # type: ignore[dict-item]
    assert await rv(_leg()) == 1.95


async def test_no_refresher_for_platform_fails_closed() -> None:
    assert await LiveOddsReverifier({})(_leg("bplay-pba")) == 0.0


async def test_refresh_error_fails_closed() -> None:
    rv = LiveOddsReverifier({"betano": _FakeRefresher(0.0, raise_exc=True)})  # type: ignore[dict-item]
    assert await rv(_leg()) == 0.0


async def test_market_unavailable_fails_closed() -> None:
    rv = LiveOddsReverifier({"betano": _FakeRefresher(None)})  # type: ignore[dict-item]
    assert await rv(_leg()) == 0.0  # decimal_odds=None → unverified → abort


# ---- BetanoCapRefresher: live per-bet cap probe (fail-soft → None) ----


class _FakeTransport:
    """Minimal Transport: returns canned (status, body) pairs in call order, or
    raises when a response slot is an Exception. Records each fetch."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, object]] = []

    async def fetch(
        self, method: str, url: str, *, json_body: object = None, headers: object = None
    ) -> tuple[int, dict[str, object]]:
        self.calls.append((method, url, json_body))
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt  # type: ignore[return-value]


def _plain_leg_resp(tag: str = "9698869897") -> dict[str, object]:
    """A plain-leg response carrying the server-generated selection tag and a
    one-bet slip (mirrors the captured 2026-06-02 shape)."""
    return {
        "data": {
            "hash": "H==.1|#x",
            "slipData": "H==.1|#x",
            "betslipTrackId": "track-1",
            "legs": [{"eventId": "86489358", "tag": tag, "odds": 3.65}],
            "bets": [{"id": f"1:SGL:{tag}", "tag": tag, "odds": 3.65, "amount": 0, "returns": 0}],
        }
    }


async def test_cap_refresher_returns_live_max() -> None:
    # plain-leg → limits {data:{min,max}} → the per-bet max (captured 2026-06-21 shape)
    t = _FakeTransport(
        [(200, _plain_leg_resp()), (200, {"data": {"min": 58804.15, "max": 70004950.0}})]
    )
    cap = await BetanoCapRefresher(t)(_leg())  # type: ignore[arg-type]
    assert cap == 70004950.0
    # two calls: plain-leg then betslipcombo/limits
    assert len(t.calls) == 2
    assert t.calls[0][1].endswith("/plain-leg/")
    assert t.calls[1][1].endswith("/api/betslipcombo/limits")
    body = t.calls[1][2]
    assert isinstance(body, dict)
    assert body["tag"] == "9698869897"  # threaded from the plain-leg response
    assert body["type"] == "SGL"


async def test_cap_refresher_none_when_max_zero() -> None:
    t = _FakeTransport([(200, _plain_leg_resp()), (200, {"data": {"min": 0.0, "max": 0.0}})])
    assert await BetanoCapRefresher(t)(_leg()) is None  # type: ignore[arg-type]


async def test_cap_refresher_none_when_max_absent() -> None:
    t = _FakeTransport([(200, _plain_leg_resp()), (200, {"data": {"min": 58804.15}})])
    assert await BetanoCapRefresher(t)(_leg()) is None  # type: ignore[arg-type]


async def test_cap_refresher_none_on_plain_leg_http_error() -> None:
    t = _FakeTransport([(400, {"data": {}})])  # plain-leg 400 → no limits call
    assert await BetanoCapRefresher(t)(_leg()) is None  # type: ignore[arg-type]
    assert len(t.calls) == 1


async def test_cap_refresher_none_on_transport_error() -> None:
    t = _FakeTransport([TransportError("transport boom")])  # raises on plain-leg
    assert await BetanoCapRefresher(t)(_leg()) is None  # type: ignore[arg-type]


async def test_cap_refresher_none_for_non_betano() -> None:
    t = _FakeTransport([])  # nothing to fetch — non-betano is a static-cap platform
    assert await BetanoCapRefresher(t)(_leg("betsson")) is None  # type: ignore[arg-type]
    assert t.calls == []  # never probed


async def test_cap_refresher_none_when_limits_call_raises() -> None:
    """The SECOND hop (betslipcombo/limits) failing must also fail-soft → None, not
    just the first (plain-leg). Pins fail-soft across both network hops."""
    t = _FakeTransport([(200, _plain_leg_resp()), TransportError("limits boom")])
    assert await BetanoCapRefresher(t)(_leg()) is None  # type: ignore[arg-type]
    assert len(t.calls) == 2  # plain-leg succeeded, limits raised


async def test_cap_refresher_none_when_limits_http_error() -> None:
    """A non-2xx limits response (e.g. 500) also fail-softs → None."""
    t = _FakeTransport([(200, _plain_leg_resp()), (500, {"data": {}})])
    assert await BetanoCapRefresher(t)(_leg()) is None  # type: ignore[arg-type]
