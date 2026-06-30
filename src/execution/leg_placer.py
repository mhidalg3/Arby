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

import asyncio
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
# A favorable Betsson odds correction is re-submitted only up to this much above the
# submitted price. Real arb drift is ~1-5%; a multiple-of-the-price "valid" odds is a
# data/selection anomaly, so we fail closed past the cap. Deliberately NOT the 1.0%
# `odds_tolerance_pct` (config.py:39) — that governs UNFAVORABLE drift and would
# re-reject the observed +2.30% favorable move this change exists to capture.
_BETSSON_RESUBMIT_MAX_UPLIFT_PCT = 20.0
# Headers the coupons POST always needs; merged UNDER the captured live context so
# the placer never depends on which request we captured the context from.
_BETSSON_REQUIRED_HEADERS = {
    "brandid": "238cb63a-3dcc-4fdf-b241-23a12cb71aa7",
    "marketcode": "ag",
    "x-sb-type": "b2b",
    "x-sb-jurisdiction": "Iplyc",
}
_BETWARRIOR_PLACE_URL = "https://cf-al-auth-api.kambicdn.com/player/api/v2019/bwargbap/coupon.json"
# The SPA's own authenticated coupon-history GET (same host as the placement POST).
# Proven reachable on cf-al-auth-api.kambicdn.com by the auth-liveness probe and the
# 2026-06-01 recon. The previous inferred per-coupon URL (``.../coupon/{ref}.json``)
# 404'd on the 2026-06-23 LIVE_DELAY_PENDING capture (couponRef 12796224824) — this is
# the corrected endpoint. NO ``status=`` filter: a bet accepted during the live delay
# leaves the PENDING bucket (betStatus → OPEN) and would vanish from a ``status=PENDING``
# query, so only an unfiltered query observes the accepted state.
_BETWARRIOR_COUPON_HISTORY_URL = (
    "https://cf-al-auth-api.kambicdn.com/player/api/v2019/bwargbap"
    "/coupon/history.json?lang=es_AR&market=AR&client_id=200&channel_id=1"
    "&range_size=100&range_start=0"
)
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
        if result.accepted:
            if result.stake_filled == 0.0:
                # Betsson's coupon response doesn't echo stake/odds — fall back to the
                # requested values so the executor records exposure correctly.
                return replace(result, stake_filled=leg.stake_ars, odds_filled=leg.odds)
            return result
        # Not accepted. E_BETTING_ODDS_INVALID + validOdds is a price-confirmation
        # handshake: when the new price is FAVORABLE (higher odds at the SAME stake →
        # worst-case payout strictly non-decreasing, hedge can only improve) and within
        # the anomaly cap, re-submit ONCE at the exact returned odds. Unfavorable /
        # over-cap / non-odds / ambiguous rejects keep the reject (executor aborts when
        # nothing's placed; flags naked once a leg is live).
        correction = placers.betsson_odds_correction(resp)
        if correction is None:
            return result
        # Selection identity: the single-selection coupon's correction must be for OUR
        # selection. marketSelectionTag == platform_outcome_id (both the
        # `s-m-f-<hash>-<MARKET>-<outcome>` form: builder posts platform_outcome_id as
        # marketSelectionId, placers.py:73; live reject tag, events.jsonl:17). A
        # non-empty tag that differs ⇒ the market/line changed underneath us → fail
        # closed.
        if correction.selection_tag not in ("", leg.platform_outcome_id):
            self._log.warning(
                "leg_placer.betsson_odds_tag_mismatch",
                submitted_selection=leg.platform_outcome_id,
                correction_tag=correction.selection_tag,
            )
            return result
        uplift_ceiling = leg.odds * (1.0 + _BETSSON_RESUBMIT_MAX_UPLIFT_PCT / 100.0)
        if not (leg.odds < correction.valid_odds <= uplift_ceiling):
            self._log.info(
                "leg_placer.betsson_odds_unfavorable",
                submitted=leg.odds,
                valid_odds=correction.valid_odds,
                selection_tag=correction.selection_tag,
            )
            return result
        self._log.info(
            "leg_placer.betsson_odds_resubmit",
            submitted=leg.odds,
            valid_odds=correction.valid_odds,
            selection_tag=correction.selection_tag,
        )
        retry_request = placers.build_betsson_request(
            [(leg.platform_outcome_id, correction.valid_odds_str)], leg.stake_ars
        )
        try:
            status, resp = await self._t.fetch(
                "POST", _BETSSON_PLACE_URL, json_body=retry_request, headers=headers
            )
        except TransportError as exc:
            self._log.warning("leg_placer.transport_error", error=str(exc))
            return PlacementResult(accepted=False, detail=f"transport: {exc!s}")
        if status >= 400:
            return PlacementResult(accepted=False, detail=f"HTTP {status}: {str(resp)[:200]}")
        retry = placers.parse_betsson(resp)
        if retry.accepted and retry.stake_filled == 0.0:
            return replace(retry, stake_filled=leg.stake_ars, odds_filled=correction.valid_odds)
        return retry


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

    def __init__(
        self,
        transport: BetWarriorTransport,
        *,
        live_delay_timeout_s: float = 16.0,
        live_delay_interval_s: float = 2.0,
    ) -> None:
        self._t = transport
        # Kambi's live-bet delay window is a few seconds (sport-dependent); 16s covers it
        # with margin while bounding how long a pending bet stalls the executor. Tightened
        # in unit tests via the constructor.
        self._live_delay_timeout_s = live_delay_timeout_s
        self._live_delay_interval_s = live_delay_interval_s
        self._log = log.bind(component="leg_placer", platform=self.platform)

    async def place(self, leg: Leg) -> PlacementResult:
        try:
            bearer = await self._t.prepare_betwarrior_auth()
        except TransportError as exc:
            self._log.warning("leg_placer.transport_error", error=str(exc))
            return PlacementResult(accepted=False, detail=f"transport: {exc!s}")
        if not bearer:
            return PlacementResult(
                accepted=False,
                detail="betwarrior: session bearer not captured (logged in?)",
                auth_failed=True,
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
            return PlacementResult(
                accepted=False,
                detail=f"HTTP {status}: {str(resp)[:200]}",
                auth_failed=status == 401,
            )
        # Log the body on ANY non-SUCCESS HTTP-200 response — previously only HTTP ≥ 400 was
        # logged, so a LIVE_DELAY_PENDING (the transient hold the poll below resolves) went
        # blind. This also captures any future unrecognized status literal for diagnosis.
        if resp.get("status") != "SUCCESS":
            self._log.warning(
                "leg_placer.betwarrior_non_success",
                status=resp.get("status"),
                coupon_ref=resp.get("couponRef"),
                body=str(resp)[:2000],
            )
        if resp.get("status") == placers.BETWARRIOR_LIVE_DELAY_PENDING:
            return await self._poll_live_delay(resp, bearer)
        return placers.parse_betwarrior(resp)

    async def _poll_live_delay(self, place_resp: dict[str, Any], bearer: str) -> PlacementResult:
        """Resolve a Kambi ``LIVE_DELAY_PENDING`` placement by polling the player-API
        ``coupon/history.json`` until the bet settles, bounded by ``live_delay_timeout_s``.

        The ONLY way to ACCEPT is a history coupon whose ``betStatus == OPEN`` with an
        echoed ``stake``+``betOdds`` (``match_betwarrior_coupon`` — the same strict gate as
        a synchronous place), so the poll can never accept more loosely. A known REJECT
        literal is a CLEAN reject (we KNOW the bet is dead). Anything else (still
        ``WAITING_FOR_APPROVAL``, coupon not yet propagated, unrecognized status, transport
        error, HTTP ≥ 400) keeps polling; if the deadline elapses without a definitive
        ACCEPT/REJECT the bet was SUBMITTED (we hold a couponRef) and may still be
        pending/placed → ``pending_unknown=True`` (NOT a clean reject: the executor must not
        report "nothing placed" and hide a live position). The poll is GET-only: it never
        re-POSTs, so it cannot double-place."""
        ref = place_resp.get("couponRef")
        if ref is None:
            # The bet was SUBMITTED (LIVE_DELAY_PENDING = received) but the body gave no
            # coupon handle to poll → acceptance cannot be confirmed. NOT a clean reject:
            # it may still settle on the book. Same pending_unknown treatment as a timeout
            # so the executor never reports "nothing placed" for a submitted bet.
            return PlacementResult(
                accepted=False,
                pending_unknown=True,
                detail=(
                    "betwarrior: LIVE_DELAY_PENDING without couponRef (submitted, cannot "
                    "poll — verify on the book)"
                ),
            )
        # betRef for fallback matching (some history variants re-key by bet, not coupon).
        _coupon = place_resp.get("coupon")
        _coupon_bets = _coupon.get("bets") if isinstance(_coupon, dict) else None
        bet_ref = (
            _coupon_bets[0].get("betRef")
            if isinstance(_coupon_bets, list) and _coupon_bets and isinstance(_coupon_bets[0], dict)
            else None
        )
        url = _BETWARRIOR_COUPON_HISTORY_URL
        headers = {"authorization": f"Bearer {bearer}"}
        deadline = time.monotonic() + self._live_delay_timeout_s
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Sleep at most the remaining window — never oversleep past the deadline. A poll
            # that returns OPEN marginally past it is still ACCEPTED: the bet is genuinely
            # placed, and discarding it would hide a real position (blind naked exposure).
            await asyncio.sleep(min(self._live_delay_interval_s, remaining))
            attempt += 1
            try:
                status, resp = await self._t.fetch("GET", url, headers=headers)
            except TransportError as exc:
                self._log.warning(
                    "leg_placer.betwarrior_poll_error", attempt=attempt, error=str(exc)[:500]
                )
                continue  # keep polling to the deadline; pending_unknown if it never resolves
            if status >= 400:
                self._log.warning(
                    "leg_placer.betwarrior_poll_http_error",
                    attempt=attempt,
                    status=status,
                    body=str(resp)[:1500],
                )
                continue
            match, bet = placers.match_betwarrior_coupon(resp, coupon_ref=ref, bet_ref=bet_ref)
            if match is placers.BetHistoryMatch.ACCEPTED:
                self._log.info(
                    "leg_placer.betwarrior_delay_resolved",
                    attempt=attempt,
                    bet_status=placers.BETWARRIOR_BET_OPEN,
                    coupon_ref=ref,
                )
                return placers.betwarrior_fill(bet, ref)
            if match is placers.BetHistoryMatch.REJECTED:
                # A definitive reject literal — the bet is NOT placed. Clean reject.
                self._log.warning(
                    "leg_placer.betwarrior_delay_rejected",
                    attempt=attempt,
                    coupon_ref=ref,
                    bet_status=str(bet.get("betStatus"))[:80],
                )
                return PlacementResult(
                    accepted=False,
                    detail=f"betwarrior: bet rejected ({bet.get('betStatus')}, couponRef {ref})",
                )
            # WAITING / UNKNOWN / not-yet-propagated — keep polling to the deadline.
        self._log.warning(
            "leg_placer.betwarrior_delay_timeout",
            coupon_ref=ref,
            bet_ref=bet_ref,
            attempts=attempt,
            timeout_s=self._live_delay_timeout_s,
        )
        return PlacementResult(
            accepted=False,
            pending_unknown=True,
            detail=(
                f"betwarrior: LIVE_DELAY_PENDING unresolved after "
                f"{self._live_delay_timeout_s}s ({attempt} polls, couponRef {ref}) — "
                f"the bet may still be pending/placed on BetWarrior; verify couponRef {ref}"
            ),
        )


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
                json_body=placers.build_bplay_togglebet(outcome_id, csrf, self._url_key),
            )
            if status >= 400:
                return PlacementResult(
                    accepted=False, detail=f"HTTP {status} (togglebet): {str(resp)[:160]}"
                )
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
            return PlacementResult(
                accepted=False, detail=f"HTTP {status} (place): {str(resp)[:160]}"
            )
        return placers.parse_bplay(resp)
