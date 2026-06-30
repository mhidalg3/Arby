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
        self.prepared = 0

    async def prepare_betsson_context(self) -> dict[str, str] | None:
        self.prepared += 1
        return _FAKE_CTX

    async def fetch(self, method, url, *, json_body=None, headers=None):  # type: ignore[no-untyped-def]
        self.calls.append({"method": method, "url": url, "json": json_body, "headers": headers})
        return self._responses[len(self.calls) - 1]


class _DelayTransport:
    """BetWarrior fake: the POST returns a LIVE_DELAY_PENDING place body (with a
    couponRef), and each GET poll returns the next queued (status, body). Exercises
    BetWarriorLegPlacer._poll_live_delay without real network or multi-second sleeps."""

    def __init__(
        self,
        *,
        place_body: dict[str, Any],
        poll_responses: list[tuple[int, dict[str, Any]]],
        bearer: str | None = "TOK",
    ) -> None:
        self.bearer = bearer
        self._place_body = place_body
        self._poll_responses = list(poll_responses)
        self.calls: list[dict[str, Any]] = []

    async def prepare_betwarrior_auth(self) -> str | None:
        return self.bearer

    async def fetch(self, method, url, *, json_body=None, headers=None):  # type: ignore[no-untyped-def]
        self.calls.append({"method": method, "url": url})
        if method == "POST":
            return 200, self._place_body
        # GET poll — drain the queue; default to coupon-not-yet-propagated once
        # exhausted (match_betwarrior_coupon → UNKNOWN → keep polling → timeout).
        if self._poll_responses:
            return self._poll_responses.pop(0)
        return 200, {"historyCoupons": []}


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


def _betsson_odds_invalid(
    valid_odds: str,
    tag: str = "s-m-f-EVT-MW3W-away",
    coupon_id: str = "",
) -> dict[str, Any]:
    """The verbatim live E_BETTING_ODDS_INVALID reject shape (events.jsonl:17)."""
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


def _betsson_success(coupon_id: str = "C1") -> dict[str, Any]:
    return {
        "couponStatus": {
            "couponStatusPollingResult": "Success",
            "couponId": coupon_id,
            "couponPlacementErrors": [],
        }
    }


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
        {
            "couponStatus": {
                "couponStatusPollingResult": "Success",
                "couponId": "C1",
                "couponPlacementErrors": [],
            }
        },
    )
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-x", 50.0, 2.62))
    assert res.accepted and res.stake_filled == 50.0 and res.odds_filled == 2.62


async def test_betsson_fails_closed_when_context_not_resolved() -> None:
    # prepare_betsson_context returns None → not logged in → no place attempt.
    t = FakeTransport(ctx_headers=None)
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-x", 50.0, 2.62))
    assert not res.accepted and "context not resolved" in res.detail
    assert not t.calls  # never POSTed


async def test_betsson_favorable_odds_resubmit_accepts_at_valid_odds() -> None:
    # Reproduces fx-d11b22fcc7f2: first POST rejected at submitted 4.35 with
    # validOdds 4.45 (a favorable price-confirmation); the bounded re-submit at 4.45
    # is accepted. The new-behavior proof.
    t = SeqTransport([(200, _betsson_odds_invalid("4.45")), (200, _betsson_success())])
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-m-f-EVT-MW3W-away", 500.0, 4.35))
    assert res.accepted
    assert res.odds_filled == 4.45  # the corrected price, not the stale 4.35
    assert res.stake_filled == 500.0  # stake unchanged (exposure recorded correctly)
    assert len(t.calls) == 2  # original + exactly one re-submit
    # the re-submit body carries the exact returned validOdds string
    assert t.calls[1]["json"]["bets"][0]["betSelections"][0]["odds"] == "4.45"
    assert (
        t.calls[1]["json"]["bets"][0]["betSelections"][0]["marketSelectionId"]
        == "s-m-f-EVT-MW3W-away"
    )


async def test_betsson_unfavorable_odds_no_resubmit() -> None:
    # validOdds BELOW the submitted price → worse for us → no re-submit (keep reject).
    t = FakeTransport(200, _betsson_odds_invalid("4.25"))
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-m-f-EVT-MW3W-away", 500.0, 4.35))
    assert not res.accepted
    assert len(t.calls) == 1
    assert "E_BETTING_ODDS_INVALID" in res.detail


async def test_betsson_favorable_beyond_cap_no_resubmit() -> None:
    # validOdds 10.0 is ≈+130% over a 4.35 submit — past the 20% anomaly cap → fail
    # closed (a multiple-of-price "valid" odds is a data/selection anomaly).
    t = FakeTransport(200, _betsson_odds_invalid("10.0"))
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-m-f-EVT-MW3W-away", 500.0, 4.35))
    assert not res.accepted
    assert len(t.calls) == 1


async def test_betsson_resubmit_is_bounded_single_retry() -> None:
    # A second odds move on the re-submit (4.45 → 4.55) is NOT retried again: the
    # retry path never calls betsson_odds_correction, so a second reject returns the
    # reject — no loop, no third POST.
    t = SeqTransport([(200, _betsson_odds_invalid("4.45")), (200, _betsson_odds_invalid("4.55"))])
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-m-f-EVT-MW3W-away", 500.0, 4.35))
    assert not res.accepted
    assert len(t.calls) == 2  # no third POST
    assert "E_BETTING_ODDS_INVALID" in res.detail  # the second error


async def test_betsson_non_odds_error_no_resubmit() -> None:
    # A non-odds reject code is not a price-confirmation handshake → no re-submit.
    t = FakeTransport(
        200,
        {
            "couponStatus": {
                "couponStatusPollingResult": "Failure",
                "couponPlacementErrors": [{"code": "E_BETTING_COUPON_GENERAL"}],
            }
        },
    )
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-m-f-EVT-MW3W-away", 500.0, 4.35))
    assert not res.accepted
    assert len(t.calls) == 1


async def test_betsson_coupon_id_present_no_resubmit() -> None:
    # An odds-invalid error that ALSO carries a couponId → a coupon may have been
    # created → never re-POST over it. The parser fails closed.
    t = FakeTransport(200, _betsson_odds_invalid("4.45", coupon_id="C9"))
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-m-f-EVT-MW3W-away", 500.0, 4.35))
    assert not res.accepted
    assert len(t.calls) == 1


async def test_betsson_favorable_selection_tag_mismatch_no_resubmit() -> None:
    # The correction's tag is for a DIFFERENT selection than our single-leg coupon →
    # the market/line changed underneath us → identity gate fails closed.
    t = FakeTransport(200, _betsson_odds_invalid("4.45", tag="s-m-f-OTHER-MW3W-home"))
    res = await BetssonLegPlacer(t).place(_leg("betsson-pba", "s-m-f-EVT-MW3W-away", 500.0, 4.35))
    assert not res.accepted
    assert len(t.calls) == 1


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
    assert call["json"]["couponRows"][0]["odds"] == 1410  # 1.41 ×1000 (captured app contract)
    assert call["json"]["bets"][0]["stake"] == 500000  # 500.0 ×1000
    assert (
        call["json"]["allowOddsChange"] == "NO"
    )  # exact odds = the edge (live re-verify supplies)


async def test_betwarrior_fails_closed_when_bearer_not_captured() -> None:
    t = FakeTransport(bearer=None)  # not logged in → no session bearer
    res = await BetWarriorLegPlacer(t).place(_leg("betwarrior-pba", "42", 500.0, 1.41))
    assert not res.accepted and "bearer not captured" in res.detail
    assert res.auth_failed is True  # no bearer ⇒ eligible for one auto re-auth + retry
    assert not t.calls  # never POSTed


async def test_betwarrior_401_sets_auth_failed() -> None:
    # A placement HTTP 401 (server-side session death) is flagged auth_failed so the
    # executor's bounded re-auth + retry can rescue it; the body is surfaced for diagnosis.
    t = FakeTransport(status=401, body={"reason": "Unauthorized"})
    res = await BetWarriorLegPlacer(t).place(_leg("betwarrior-pba", "42", 500.0, 1.41))
    assert not res.accepted and res.auth_failed is True
    assert "HTTP 401" in res.detail


async def test_betwarrior_non_401_error_is_not_auth_failed() -> None:
    # A non-auth HTTP ≥400 (odds invalid, suspended outcome, validation) stays
    # auth_failed=False — today's abort/naked, never a re-auth.
    t = FakeTransport(status=400, body={"reason": "Invalid odds specified"})
    res = await BetWarriorLegPlacer(t).place(_leg("betwarrior-pba", "42", 500.0, 1.41))
    assert not res.accepted and res.auth_failed is False


async def test_betwarrior_live_delay_poll_resolves_to_open() -> None:
    # POST → LIVE_DELAY_PENDING (couponRef 777); the GET poll finds the coupon in
    # coupon/history.json with betStatus OPEN + echoed stake/odds → accepted.
    t = _DelayTransport(
        place_body={"status": "LIVE_DELAY_PENDING", "couponRef": 777},
        poll_responses=[
            (
                200,
                {
                    "historyCoupons": [
                        {
                            "couponRef": 777,
                            "bets": [{"betStatus": "OPEN", "betOdds": 1410, "stake": 500000}],
                        }
                    ]
                },
            ),
        ],
    )
    res = await BetWarriorLegPlacer(t, live_delay_timeout_s=1.0, live_delay_interval_s=0.0).place(
        _leg("betwarrior-pba", "42", 500.0, 1.41)
    )
    assert res.accepted and res.ref == "777" and res.odds_filled == 1.41
    assert not res.pending_unknown
    # Placed via POST, then GET-polled the coupon/history.json endpoint.
    assert t.calls[0]["method"] == "POST" and t.calls[0]["url"].endswith("/coupon.json")
    assert t.calls[1]["method"] == "GET" and "/coupon/history.json" in t.calls[1]["url"]


async def test_betwarrior_live_delay_poll_waiting_then_open() -> None:
    # First poll → WAITING_FOR_APPROVAL (keep polling); second poll → OPEN → accepted.
    t = _DelayTransport(
        place_body={"status": "LIVE_DELAY_PENDING", "couponRef": 777},
        poll_responses=[
            (
                200,
                {
                    "historyCoupons": [
                        {
                            "couponRef": 777,
                            "bets": [
                                {
                                    "betStatus": "WAITING_FOR_APPROVAL",
                                    "betOdds": 1410,
                                    "stake": 500000,
                                }
                            ],
                        }
                    ]
                },
            ),
            (
                200,
                {
                    "historyCoupons": [
                        {
                            "couponRef": 777,
                            "bets": [{"betStatus": "OPEN", "betOdds": 1410, "stake": 500000}],
                        }
                    ]
                },
            ),
        ],
    )
    res = await BetWarriorLegPlacer(t, live_delay_timeout_s=1.0, live_delay_interval_s=0.0).place(
        _leg("betwarrior-pba", "42", 500.0, 1.41)
    )
    assert res.accepted and res.ref == "777"
    assert len(t.calls) == 3  # POST + 2 polls


async def test_betwarrior_live_delay_poll_rejected_is_clean_reject() -> None:
    # betStatus REFUSED → a definitive reject → clean reject, NOT pending_unknown.
    t = _DelayTransport(
        place_body={"status": "LIVE_DELAY_PENDING", "couponRef": 777},
        poll_responses=[
            (
                200,
                {
                    "historyCoupons": [
                        {
                            "couponRef": 777,
                            "bets": [{"betStatus": "REFUSED", "betOdds": 1410, "stake": 500000}],
                        }
                    ]
                },
            ),
        ],
    )
    res = await BetWarriorLegPlacer(t, live_delay_timeout_s=1.0, live_delay_interval_s=0.0).place(
        _leg("betwarrior-pba", "42", 500.0, 1.41)
    )
    assert not res.accepted and not res.pending_unknown
    assert "rejected" in res.detail and "REFUSED" in res.detail


async def test_betwarrior_live_delay_poll_times_out_pending_unknown() -> None:
    # Coupon never appears in history (still WAITING / not propagated) → deadline
    # trips → pending_unknown (bet was submitted; may still be placed), not a
    # clean reject.
    t = _DelayTransport(
        place_body={"status": "LIVE_DELAY_PENDING", "couponRef": 777},
        poll_responses=[],  # defaults to coupon-not-propagated
    )
    res = await BetWarriorLegPlacer(
        t, live_delay_timeout_s=0.02, live_delay_interval_s=0.005
    ).place(_leg("betwarrior-pba", "42", 500.0, 1.41))
    assert not res.accepted and res.pending_unknown
    assert "LIVE_DELAY_PENDING unresolved" in res.detail and "777" in res.detail
    assert len(t.calls) >= 2  # POSTed + polled at least once


async def test_betwarrior_live_delay_without_coupon_ref_is_pending_unknown() -> None:
    # No couponRef on the pending body, but the bet WAS submitted (LIVE_DELAY_PENDING) →
    # cannot poll, and it may still settle → pending_unknown (NOT a clean reject).
    t = _DelayTransport(
        place_body={"status": "LIVE_DELAY_PENDING"},  # no couponRef
        poll_responses=[(200, {"historyCoupons": []})],
    )
    res = await BetWarriorLegPlacer(t, live_delay_timeout_s=1.0, live_delay_interval_s=0.0).place(
        _leg("betwarrior-pba", "42", 500.0, 1.41)
    )
    assert not res.accepted and res.pending_unknown
    assert "without couponRef" in res.detail
    assert len(t.calls) == 1  # POSTed only — never polled


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


async def test_bplay_threads_csrf_and_uses_whole_ars_stake() -> None:
    togglebet = {"header": {"csrf_token": "C1"}, "body": {}, "footer": {}}
    place = {"return": "OK", "message": {"type": "success", "message": "Apuesta colocada"}}
    t = SeqTransport([(200, togglebet), (200, place)])

    async def bootstrap() -> str:
        return "C0"

    res = await BplayLegPlacer(
        t, event_url_key="/eventos/10595536-francia-senegal", bootstrap_csrf=bootstrap
    ).place(_leg("bplay-pba", "6621121460", 500.0, 1.5))
    assert res.accepted
    toggle_body = t.calls[0]["json"]
    assert toggle_body["data"] == {"id": 6621121460, "csrf_token": "C0"}  # bootstrap token
    assert toggle_body["context"]["url_key"] == "/eventos/10595536-francia-senegal"  # event-keyed
    place_body = t.calls[1]["json"]
    assert place_body["data"]["csrf_token"] == "C1"  # threaded from togglebet response
    slip = place_body["data"]["data"]["betslip"]
    assert slip["stake"] == {"6621121460": 500}  # ×1 (whole ARS — confirmed by capture)
    assert slip["nb_bettingslip_totalStake"] == "1.00"  # line count, not the amount
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
