"""Per-platform LegPlacer implementations: build → send in-session → parse.

Wraps the request builders + confirmation parsers (:mod:`placers`) around a
`Transport` (:mod:`session`), implementing the executor's `LegPlacer` protocol
so the two-leg state machine can drive real placement.

This turn covers the **stateless** platforms (Betsson, BetWarrior) — single
POSTs. The **stateful** ones (Betano, Bplay) need a slip-build sequence first
(to obtain the ``hash`` / ``csrf_token``) and are added with the trial wiring.

A place failure (HTTP ≥ 400, transport error, or a non-success confirmation)
returns ``accepted=False`` so the executor handles it deterministically
(abort if Leg A, naked-exposure if Leg B) — it does not raise. The
build→send→parse wiring is unit-tested with a fake Transport; the real
in-session send is validated in the Phase-1 trial.
"""

from __future__ import annotations

import structlog

from src.execution import placers
from src.execution.executor import Leg, PlacementResult
from src.execution.session import Transport, TransportError

log = structlog.get_logger(__name__)

_BETSSON_PLACE_URL = "https://pba.betsson.bet.ar/api/sb/v2/coupons"
_BETWARRIOR_PLACE_URL = "https://cf-al-auth-api.kambicdn.com/player/api/v2019/bwargbap/coupon.json"

# Betsson OBG headers required on every sportsbook call (from recon).
_BETSSON_HEADERS = {
    "content-type": "application/json",
    "brandid": "238cb63a-3dcc-4fdf-b241-23a12cb71aa7",
    "marketcode": "ag",
    "x-sb-type": "b2b",
    "x-sb-jurisdiction": "Iplyc",
}


class BetssonLegPlacer:
    """Betsson OBG — stateless single POST to ``/api/sb/v2/coupons``."""

    platform = "betsson-pba"

    def __init__(self, transport: Transport) -> None:
        self._t = transport
        self._log = log.bind(component="leg_placer", platform=self.platform)

    async def place(self, leg: Leg) -> PlacementResult:
        request = placers.build_betsson_request(
            [(leg.platform_outcome_id, f"{leg.odds:.2f}")], leg.stake_ars
        )
        try:
            status, resp = await self._t.fetch(
                "POST", _BETSSON_PLACE_URL, json_body=request, headers=_BETSSON_HEADERS
            )
        except TransportError as exc:
            self._log.warning("leg_placer.transport_error", error=str(exc))
            return PlacementResult(accepted=False, detail=f"transport: {exc!s}")
        if status >= 400:
            return PlacementResult(accepted=False, detail=f"HTTP {status}")
        return placers.parse_betsson(resp)


class BetWarriorLegPlacer:
    """BetWarrior (Kambi) — stateless single POST to ``coupon.json``.
    Needs the Kambi session bearer token (``auth_token``)."""

    platform = "betwarrior-pba"

    def __init__(self, transport: Transport, auth_token: str) -> None:
        self._t = transport
        self._auth = auth_token
        self._log = log.bind(component="leg_placer", platform=self.platform)

    async def place(self, leg: Leg) -> PlacementResult:
        request = placers.build_betwarrior_request(
            outcome_id=int(leg.platform_outcome_id),
            odds_x100=round(leg.odds * 100),
            stake_thousandths=round(leg.stake_ars * 1000),
        )
        headers = {"content-type": "application/json", "authorization": f"Bearer {self._auth}"}
        try:
            status, resp = await self._t.fetch(
                "POST", _BETWARRIOR_PLACE_URL, json_body=request, headers=headers
            )
        except TransportError as exc:
            self._log.warning("leg_placer.transport_error", error=str(exc))
            return PlacementResult(accepted=False, detail=f"transport: {exc!s}")
        if status >= 400:
            return PlacementResult(accepted=False, detail=f"HTTP {status}")
        return placers.parse_betwarrior(resp)
