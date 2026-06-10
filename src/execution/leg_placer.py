"""Per-platform LegPlacer implementations: build → send in-session → parse.

Wraps the request builders + confirmation parsers (:mod:`placers`) around a
`Transport` (:mod:`session`), implementing the executor's `LegPlacer` protocol
so the two-leg state machine can drive real placement.

Covers all four. The **stateless** platforms (Betsson, BetWarrior) are single
POSTs. The **stateful** ones run a slip-build sequence first: Betano does
plain-leg → updatebets (which refreshes the ``hash``) → place; Bplay does
togglebet → place, threading the rotated ``header.csrf_token`` (the first,
bootstrap, token is read off the live page via ``bootstrap_csrf``).

A place failure (HTTP ≥ 400, transport error, or a non-success confirmation)
returns ``accepted=False`` so the executor handles it deterministically
(abort if Leg A, naked-exposure if Leg B) — it does not raise. The
build→send→parse wiring is unit-tested with a fake Transport; the real
in-session send is validated in the Phase-1 trial.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol

import structlog

from src.execution import placers
from src.execution.executor import Leg, PlacementResult
from src.execution.session import Transport, TransportError

log = structlog.get_logger(__name__)

_BETSSON_PLACE_URL = "https://pba.betsson.bet.ar/api/sb/v2/coupons"
# Headers the coupons POST always needs; merged UNDER the captured live context so
# the placer never depends on which request we captured the context from.
_BETSSON_REQUIRED_HEADERS = {
    "brandid": "238cb63a-3dcc-4fdf-b241-23a12cb71aa7",
    "marketcode": "ag",
    "x-sb-type": "b2b",
    "x-sb-jurisdiction": "Iplyc",
}
_BETWARRIOR_PLACE_URL = "https://cf-al-auth-api.kambicdn.com/player/api/v2019/bwargbap/coupon.json"
_BETANO_BASE = "https://www.betano.bet.ar/api/betslip/v3"
_BPLAY_BASE = "https://ws-deportespba.bplay.bet.ar"


class BetssonTransport(Protocol):
    """What BetssonLegPlacer needs: the generic fetch + the authenticated-context
    read (`InSessionTransport.prepare_betsson_context`)."""

    async def prepare_betsson_context(self) -> dict[str, str] | None: ...
    async def fetch(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]: ...


class BetssonLegPlacer:
    """Betsson OBG — POST to ``/api/sb/v2/coupons``.

    Placement needs the *authenticated* session context (sessiontoken + the
    ``ctx-`` user-context + x-sb-* headers). That context lives in the SPA's
    in-memory state and is established by client-side in-app navigation after
    login (a hard reload destroys it), so the transport does NOT navigate — the
    live session must already be in the placeable state (operator-driven for now,
    automated in-app nav later). We read the live header set the app is using,
    add the coupon-submit headers, and POST the `updateSources`-carrying body.
    """

    platform = "betsson-pba"

    def __init__(self, transport: BetssonTransport) -> None:
        self._t = transport
        self._log = log.bind(component="leg_placer", platform=self.platform)

    async def place(self, leg: Leg) -> PlacementResult:
        try:
            ctx_headers = await self._t.prepare_betsson_context()
        except TransportError as exc:
            self._log.warning("leg_placer.transport_error", error=str(exc))
            return PlacementResult(accepted=False, detail=f"transport: {exc!s}")
        if not ctx_headers:
            return PlacementResult(
                accepted=False,
                detail="betsson: authenticated context not resolved (login + in-app nav?)",
            )
        headers = {
            **_BETSSON_REQUIRED_HEADERS,
            **ctx_headers,
            "content-type": "application/json",
            "x-sb-identifier": "BETSLIP_SUBMIT_COUPONS_REQUEST",
        }
        request = placers.build_betsson_request(
            [(leg.platform_outcome_id, f"{leg.odds:.2f}")], leg.stake_ars
        )
        try:
            status, resp = await self._t.fetch(
                "POST", _BETSSON_PLACE_URL, json_body=request, headers=headers
            )
        except TransportError as exc:
            self._log.warning("leg_placer.transport_error", error=str(exc))
            return PlacementResult(accepted=False, detail=f"transport: {exc!s}")
        if status >= 400:
            return PlacementResult(accepted=False, detail=f"HTTP {status}: {str(resp)[:200]}")
        result = placers.parse_betsson(resp)
        if result.accepted and result.stake_filled == 0.0:
            # Betsson's coupon response doesn't echo stake/odds — fall back to the
            # requested values so the executor records exposure correctly.
            return replace(result, stake_filled=leg.stake_ars, odds_filled=leg.odds)
        return result


class BetWarriorTransport(Protocol):
    """What BetWarriorLegPlacer needs: the generic fetch + the live Kambi session
    bearer read (`InSessionTransport.prepare_betwarrior_auth`)."""

    async def prepare_betwarrior_auth(self) -> str | None: ...
    async def fetch(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]: ...


class BetWarriorLegPlacer:
    """BetWarrior (Kambi) — stateless single POST to ``coupon.json``.

    Reads the live Kambi session bearer from the warm transport at place-time
    (captured passively from the SPA's authenticated calls — analogous to
    Betsson's context), so a refreshed token is always used and a not-logged-in
    session fails closed rather than placing with a stale/absent token."""

    platform = "betwarrior-pba"

    def __init__(self, transport: BetWarriorTransport) -> None:
        self._t = transport
        self._log = log.bind(component="leg_placer", platform=self.platform)

    async def place(self, leg: Leg) -> PlacementResult:
        try:
            bearer = await self._t.prepare_betwarrior_auth()
        except TransportError as exc:
            self._log.warning("leg_placer.transport_error", error=str(exc))
            return PlacementResult(accepted=False, detail=f"transport: {exc!s}")
        if not bearer:
            return PlacementResult(
                accepted=False, detail="betwarrior: session bearer not captured (logged in?)"
            )
        # leg.odds must be the live-re-verified current odds (allowOddsChange is NO) —
        # Kambi rejects "Invalid odds specified" if it doesn't match the book's current.
        request = placers.build_betwarrior_request(
            outcome_id=int(leg.platform_outcome_id),
            odds_x1000=round(leg.odds * 1000),
            stake_thousandths=round(leg.stake_ars * 1000),
        )
        headers = {"content-type": "application/json", "authorization": f"Bearer {bearer}"}
        try:
            status, resp = await self._t.fetch(
                "POST", _BETWARRIOR_PLACE_URL, json_body=request, headers=headers
            )
        except TransportError as exc:
            self._log.warning("leg_placer.transport_error", error=str(exc))
            return PlacementResult(accepted=False, detail=f"transport: {exc!s}")
        if status >= 400:
            # Surface the Kambi error body — it names the reason (odds change, suspended
            # outcome, validation) so a rejection is diagnosable, not an opaque 400.
            self._log.warning("leg_placer.http_error", status=status, body=str(resp)[:300])
            return PlacementResult(accepted=False, detail=f"HTTP {status}: {str(resp)[:200]}")
        return placers.parse_betwarrior(resp)


def _data_envelope(resp: dict[str, object]) -> dict[str, object]:
    """Betano wraps its slip payload in ``{"data": {...}}``."""
    inner = resp.get("data")
    return inner if isinstance(inner, dict) else {}


class BetanoLegPlacer:
    """Betano (Kaizen) — stateful: plain-leg → updatebets → place.

    eventId = ``leg.platform_event_ref`` (falling back to ``leg.match_id`` so
    `match_id` can carry a canonical cross-platform id for exposure tracking);
    selectionId = ``leg.platform_outcome_id``. updatebets refreshes the ``hash``
    the place call must use, so the slip is rebuilt from each response.
    """

    platform = "betano-pba"

    def __init__(self, transport: Transport) -> None:
        self._t = transport
        self._log = log.bind(component="leg_placer", platform=self.platform)

    async def place(self, leg: Leg) -> PlacementResult:
        event_id = leg.platform_event_ref or leg.match_id
        try:
            status, resp = await self._t.fetch(
                "POST",
                f"{_BETANO_BASE}/plain-leg/",
                json_body=placers.build_betano_plain_leg(leg.platform_outcome_id, event_id),
            )
            if status >= 400:
                return PlacementResult(accepted=False, detail=f"HTTP {status} (plain-leg)")
            slip = placers.betano_slip_from_response(_data_envelope(resp))
            if not slip.get("bets"):
                return PlacementResult(accepted=False, detail="betano: plain-leg added no bet")

            status, resp = await self._t.fetch(
                "PATCH",
                f"{_BETANO_BASE}/updatebets",
                json_body=placers.build_betano_updatebets(slip, leg.stake_ars),
            )
            if status >= 400:
                return PlacementResult(accepted=False, detail=f"HTTP {status} (updatebets)")
            slip = placers.betano_slip_from_response(_data_envelope(resp))
            if not slip.get("bets"):
                # updatebets rejected the body (e.g. 400 → empty data); don't place a naked slip.
                return PlacementResult(
                    accepted=False,
                    detail=f"betano: updatebets returned no slip "
                    f"(errorCode={resp.get('errorCode')}, errors={resp.get('errors')})",
                )
            self._log.info("betano.placing", stake=leg.stake_ars, odds=leg.odds)
            status, resp = await self._t.fetch(
                "POST",
                f"{_BETANO_BASE}/place",
                json_body=placers.build_betano_request(slip, leg.stake_ars),
            )
        except TransportError as exc:
            self._log.warning("leg_placer.transport_error", error=str(exc))
            return PlacementResult(accepted=False, detail=f"transport: {exc!s}")
        if status >= 400:
            return PlacementResult(accepted=False, detail=f"HTTP {status} (place)")
        return placers.parse_betano(resp)


class BplayLegPlacer:
    """Bplay (SportNCO) — stateful: togglebet → place.

    Each slip response returns a fresh ``header.csrf_token`` threaded into the
    next call. The first (bootstrap) token lives in the page's JS state, not in
    the slip flow, so it's supplied by ``bootstrap_csrf`` — an async callable
    that reads it off the live page (the one piece confirmed in the trial).
    ``event_url_key`` is the match page path the place call is keyed to.
    """

    platform = "bplay-pba"

    def __init__(
        self,
        transport: Transport,
        *,
        event_url_key: str,
        bootstrap_csrf: Callable[[], Awaitable[str]],
    ) -> None:
        self._t = transport
        self._url_key = event_url_key
        self._bootstrap = bootstrap_csrf
        self._log = log.bind(component="leg_placer", platform=self.platform)

    async def place(self, leg: Leg) -> PlacementResult:
        outcome_id = int(leg.platform_outcome_id)
        try:
            csrf = await self._bootstrap()
            status, resp = await self._t.fetch(
                "POST",
                f"{_BPLAY_BASE}/bettingslip/togglebet",
                json_body=placers.build_bplay_togglebet(outcome_id, csrf),
            )
            if status >= 400:
                return PlacementResult(accepted=False, detail=f"HTTP {status} (togglebet)")
            csrf = placers.bplay_csrf_from_response(resp) or csrf

            status, resp = await self._t.fetch(
                "POST",
                f"{_BPLAY_BASE}/bettingslip",
                json_body=placers.build_bplay_request(
                    url_key=self._url_key,
                    outcome_id=outcome_id,
                    stake_ars=leg.stake_ars,
                    csrf_token=csrf,
                    date_ms=int(time.time() * 1000),
                ),
            )
        except TransportError as exc:
            self._log.warning("leg_placer.transport_error", error=str(exc))
            return PlacementResult(accepted=False, detail=f"transport: {exc!s}")
        if status >= 400:
            return PlacementResult(accepted=False, detail=f"HTTP {status} (place)")
        return placers.parse_bplay(resp)
