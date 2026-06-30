"""Per-platform placement contracts: confirmation parsers.

Captured from real placements on 2026-06-02 (see ``scripts/recon/RECON_LOG.md``):

  betano     POST www.betano.bet.ar/api/betslip/v3/place
  bplay      POST ws-deportespba.bplay.bet.ar/bettingslip
  betwarrior POST cf-al-auth-api.kambicdn.com/.../coupon.json   (Kambi)
  betsson    POST pba.betsson.bet.ar/api/sb/v2/coupons          (OBG)

This module is the **pure** parse half of each platform's LegPlacer: it turns a
place response into a `PlacementResult`. The request-building + in-session
transport (which also acquires slip-state tokens for Betano/Bplay) is wired
separately for the trial.

``accepted`` is the safety-critical field — the executor's naked-exposure logic
keys on it — so each parser derives it strictly from the platform's documented
success markers and **fails closed** on anything unrecognised. ``stake_filled`` /
``odds_filled`` are populated only when the response echoes them (Betano,
BetWarrior); otherwise the caller falls back to the requested values.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from src.execution.executor import PlacementResult

# ---- request builders ----
# Pure constructors for each platform's place body. Stateless platforms
# (Betsson, BetWarrior) build from the selection + stake directly; stateful
# ones (Betano, Bplay) take slip-state tokens (hash / csrf) the transport
# obtains from a prior slip-build call. Verified against the captured request
# bodies (2026-06-02); analytics-only sub-objects (Betsson `updateSources`,
# BetWarrior `trackingData`) are omitted and validated live in the trial.


def build_betsson_request(selections: list[tuple[str, str]], stake_ars: float) -> dict[str, Any]:
    """Betsson OBG `/api/sb/v2/coupons`. `selections` = [(marketSelectionId,
    odds_str), …] — one entry for a single 1X2 leg.

    `updateSources` pins the coupon to a price/status feed version; its absence
    (or wrong shape) → `E_BETTING_COUPON_GENERAL`. Validated live 2026-06-03:
    with `acceptOddsChanges: true` the server accepts FABRICATED `rt:`/`api:`
    uuids as long as the structure matches, so we generate them (no need to lift
    the real values off the rtf feed). The market id is the selection id minus
    the `s-` prefix and the outcome suffix; one `api:` uuid is shared across the
    batch, one `rt:` uuid per selection."""
    odds_selections: dict[str, str] = {}
    latest_rt: dict[str, str] = {}
    status_selections: dict[str, str] = {}
    status_markets: dict[str, str] = {}
    api_tag = f"api: {uuid.uuid4()}"
    for sid, _odds in selections:
        rt_tag = f"rt: {uuid.uuid4()}"
        odds_selections[sid] = rt_tag
        latest_rt[sid] = rt_tag
        status_selections[sid] = api_tag
        status_markets[sid[2:].rsplit("-", 1)[0]] = api_tag
    return {
        "bets": [
            {
                "stake": stake_ars,
                "stakeForReview": 0,
                "taxAmount": 0,
                "taxAmountForReview": 0,
                "oddsFormat": 1,
                "hasInconsistentBetSelectionPriceFormats": False,
                "currencyCode": "ARS",
                "betSelections": [
                    {"marketSelectionId": sid, "odds": odds} for sid, odds in selections
                ],
            }
        ],
        "acceptOddsChanges": True,
        "updateSources": {
            "odds": {"selections": odds_selections, "latestRt": latest_rt},
            "statuses": {"selections": status_selections, "markets": status_markets},
        },
        "betslipOddChangeBehaviour": "CanAcceptOddChanges",
    }


def build_betwarrior_request(
    *, outcome_id: int, odds_x1000: int, stake_thousandths: int, request_id: str | None = None
) -> dict[str, Any]:
    """Kambi `/coupon.json`. Odds AND stake are Kambi minor units ×1000 (confirmed by
    capturing the app's real request: a 1.13 line places as ``odds: 1130``, 50 ARS as
    ``stake: 50000``). ``allowOddsChange: "NO"`` matches the app — it places with the
    EXACT current odds (a live re-verify must supply them), so a moved line is rejected
    ("Invalid odds specified") rather than silently placed at a worse price."""
    return {
        "couponRows": [{"index": 0, "odds": odds_x1000, "outcomeId": outcome_id, "type": "SIMPLE"}],
        "allowOddsChange": "NO",
        "allowOddsChangeLive": "NO",
        "allowOddsChangePreMatch": "NO",
        "bets": [{"couponRowIndexes": [0], "eachWay": False, "stake": stake_thousandths}],
        "requestId": request_id or str(uuid.uuid4()),
        "channel": "WEB",
    }


def _bplay_context(url_key: str) -> dict[str, Any]:
    return {
        "url_key": url_key,
        "version": "1.0.1",
        "device": "web_vuejs_desktop",
        "lang": "ag",
        "timezone": "America/Buenos_Aires",
        "url_params": {},
    }


def build_bplay_togglebet(outcome_id: int, csrf_token: str, url_key: str = "/") -> dict[str, Any]:
    """Bplay `/bettingslip/togglebet` — adds the selection to the server-side slip.
    The live app keys the context to the EVENT url_key (not "/"); pass it. The CSRF
    does NOT rotate within a slip (confirmed by capture) — the response echoes the
    same token under ``header.csrf_token``."""
    return {
        "context": _bplay_context(url_key),
        "data": {"id": outcome_id, "csrf_token": csrf_token},
    }


def bplay_csrf_from_response(resp: dict[str, Any]) -> str:
    """The CSRF a slip response echoes (``header.csrf_token``) — same token, threaded
    forward so a future rotation would be picked up transparently."""
    return str(_d(resp.get("header")).get("csrf_token", "") or "")


def build_bplay_request(
    *,
    url_key: str,
    outcome_id: int,
    stake_ars: float,
    csrf_token: str,
    date_ms: int,
    accept_odds_change: bool = True,
) -> dict[str, Any]:
    """Bplay SportNCO `/bettingslip` (place). Confirmed by a live placement capture:
    the per-outcome ``stake`` map is WHOLE ARS (×1 — a 500-peso bet → ``{id: 500}``);
    ``nb_bettingslip_totalStake`` is the line count for a single bet (constant
    ``"1.00"``), NOT the amount; no odds are sent (Bplay places at its current odds,
    gated by ``accept``). ``accept`` True = take the book's current odds (we re-verify
    first); ``date_ms`` is the current epoch-ms."""
    return {
        "context": _bplay_context(url_key),
        "data": {
            "data": {
                "date": date_ms,
                "betslip": {
                    "nb_bettingslip_totalStake": "1.00",
                    "freebet": None,
                    "formule": "single",
                    "combiboost_id": None,
                    "accept": accept_odds_change,
                    "stake": {str(outcome_id): round(stake_ars)},
                },
            },
            "csrf_token": csrf_token,
        },
    }


def build_betano_plain_leg(selection_id: str, event_id: str) -> dict[str, Any]:
    """Betano `/api/betslip/v3/plain-leg/` (first slip-build call): adds a
    selection to an empty slip. The response ``data`` carries the
    hash/slipData/legs/bets/betslipTrackId the next calls thread forward."""
    return {
        "selectionIds": [selection_id],
        "betslip": {
            "hash": "",
            "slipData": "",
            "legs": [],
            "bets": [],
            "betslipTabId": 1,
            "betslipTrackId": "",
        },
        "eventId": event_id,
        "triggerPoint": {"origin": 6, "parentOrigin": 1},
    }


def betano_slip_from_response(data: dict[str, Any]) -> dict[str, Any]:
    """Pick the betslip subset the next call needs out of a plain-leg/updatebets
    ``data`` envelope (drops echo-only fields like errors/taxDetails)."""
    return {
        "hash": data.get("hash", ""),
        "slipData": data.get("slipData", ""),
        "legs": data.get("legs", []),
        "bets": data.get("bets", []),
        "betslipTabId": data.get("betSlipTabId", 1),
        "betslipTrackId": data.get("betslipTrackId", ""),
    }


def _betano_fill(slip: dict[str, Any], stake_ars: float) -> dict[str, Any]:
    """Fill the stake (amount + returns) into each bet of a betslip subset."""
    betslip = copy.deepcopy(slip)
    for bet in betslip.get("bets", []):
        if not isinstance(bet, dict):
            continue
        odds = bet.get("odds") or 0
        bet["amount"] = stake_ars
        bet["returns"] = round(stake_ars * float(odds), 2) if odds else 0
    return betslip


def build_betano_updatebets(slip: dict[str, Any], stake_ars: float) -> dict[str, Any]:
    """Betano `PATCH /api/betslip/v3/updatebets`. The body carries a TOP-LEVEL
    ``bets`` array — the bets with the desired ``amount`` set and ``returns`` left
    0 (the server computes it) — ALONGSIDE the current (unfilled, amount=0)
    ``betslip``. The response returns the refreshed ``hash`` + the filled,
    limit-checked slip the place call uses. (Sending only ``betslip`` → 400.)"""
    top_bets: list[dict[str, Any]] = []
    for bet in slip.get("bets", []):
        if not isinstance(bet, dict):
            continue
        b = copy.deepcopy(bet)
        b["amount"] = stake_ars
        b["returns"] = 0
        top_bets.append(b)
    return {"bets": top_bets, "betslip": copy.deepcopy(slip)}


def build_betano_request(slip_state: dict[str, Any], stake_ars: float) -> dict[str, Any]:
    """Betano `/api/betslip/v3/place`. `slip_state` is the betslip subset from
    the *updatebets* response (carries the refreshed hash/slipData/legs/bets/
    betslipTrackId); this fills the stake into each bet and returns the place body."""
    betslip = _betano_fill(slip_state, stake_ars)
    betslip["oddschanges"] = "0"
    return {"betslip": betslip}


def build_betano_limits(slip: dict[str, Any], tag: str) -> dict[str, Any]:
    """Betano ``POST /api/betslipcombo/limits`` — read the per-bet stake ceiling
    for the slip's single selection pre-place. Threads the slip subset + the
    selection's server-generated ``tag`` forward; ``type: "SGL"`` marks a single.
    Captured 2026-06-21: the response is ``{"data": {"min": …, "max": …}}`` — NOT
    the ``data.bets[].maxAmount`` shape ``updatebets`` echoes (that one is always
    0). Deep-copies the slip so the caller's state is untouched."""
    return {"betslip": copy.deepcopy(slip), "tag": tag, "type": "SGL"}


def betano_leg_tag(data: dict[str, Any]) -> str | None:
    """The server-generated selection tag (``data.legs[0].tag``, mirrored in
    ``data.bets[0].tag``) that ``plain-leg``/``updatebets`` return and
    ``betslipcombo/limits`` threads forward. Falls back across legs→bets→None."""
    for key in ("legs", "bets"):
        items = data.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            tag = items[0].get("tag")
            if isinstance(tag, str) and tag:
                return tag
    return None


def betano_limits_max(resp: dict[str, Any]) -> float | None:
    """Parse ``{"data": {"min": …, "max": …}}`` → the per-bet max stake, or None
    when the field is absent / not a number (fail-soft)."""
    data = resp.get("data")
    if not isinstance(data, dict):
        return None
    max_val = data.get("max")
    if max_val is None:
        return None
    try:
        return float(max_val)
    except (TypeError, ValueError):
        return None


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _d(value: Any) -> dict[str, Any]:
    """Narrow an untrusted value to a dict (empty if it isn't one)."""
    return value if isinstance(value, dict) else {}


def _first(value: Any) -> dict[str, Any]:
    """First element of a list as a dict (empty if missing / not a dict)."""
    return _d(value[0]) if isinstance(value, list) and value else {}


def parse_betano(resp: dict[str, Any]) -> PlacementResult:
    """``{"data":{"accepted":true,"receipts":[{"betId","totalAmount","totalOdds"}]}}``"""
    data = _d(resp.get("data"))
    if data.get("accepted") is not True:
        # Surface WHY: Betano puts rejection reasons in data.errors (and the place
        # body sends oddschanges:"0", so odds drift is a common cause).
        errors = data.get("errors") or resp.get("errors") or data.get("errorMessages")
        return PlacementResult(
            accepted=False,
            detail=f"betano: not accepted (errors={errors!r}; "
            f"data_keys={sorted(data.keys())}; top_keys={sorted(resp.keys())})",
        )
    receipt = _first(data.get("receipts"))
    return PlacementResult(
        accepted=True,
        stake_filled=_f(receipt.get("totalAmount")),
        odds_filled=_f(receipt.get("totalOdds")),
        ref=str(receipt.get("betId", "")),
    )


def parse_bplay(resp: dict[str, Any]) -> PlacementResult:
    """``{"return":"OK","message":{"type":"success","message":"Apuesta colocada"}}``"""
    message = _d(resp.get("message"))
    accepted = resp.get("return") == "OK" and message.get("type") == "success"
    return PlacementResult(
        accepted=bool(accepted),
        ref=str(resp.get("betslip_id", "") or ""),
        detail="" if accepted else str(message.get("message", "")),
    )


# Kambi returns this TRANSIENT placement status when a bet is held during the live-bet
# delay window: the bet was RECEIVED and is pending (resolves to SUCCESS/ACCEPTED or a
# reject after a few seconds) — NOT a rejection. ``parse_betwarrior`` stays strict
# (status == SUCCESS and an echoed coupon.bets); the placer owns the bounded poll that resolves it (see
# ``BetWarriorLegPlacer._poll_live_delay``). Shared literal so both modules agree.
BETWARRIOR_LIVE_DELAY_PENDING = "LIVE_DELAY_PENDING"


def parse_betwarrior(resp: dict[str, Any]) -> PlacementResult:
    """``{"status":"SUCCESS","couponRef":N,"coupon":{"bets":[{"betOdds","stake"}]}}``
    Kambi units: BOTH odds and stake are ×1000 (confirmed by a live placement — a 1.14
    bet echoes ``betOdds: 1140``, 50 ARS ``stake: 50000``)."""
    if resp.get("status") != "SUCCESS":
        return PlacementResult(accepted=False, detail=f"betwarrior: status={resp.get('status')}")
    bet = _first(_d(resp.get("coupon")).get("bets"))
    # Require an ECHOED bet (stake + odds). A bare status == SUCCESS with no coupon.bets
    # is NOT a placement — the live-delay poll (``match_betwarrior_coupon``) applies the
    # same echoed-bet gate, so accepting on status alone could mark an unplaced bet as
    # placed and leave the executor building a naked position. The documented SUCCESS
    # shape always echoes coupon.bets[0], so this never rejects a genuine placement —
    # it only closes the status-only hole.
    if "stake" not in bet or "betOdds" not in bet:
        return PlacementResult(
            accepted=False,
            detail="betwarrior: SUCCESS without echoed coupon.bets (status ok, no bet)",
        )
    return betwarrior_fill(bet, resp.get("couponRef", ""))


def betwarrior_fill(bet: dict[str, Any], ref: Any) -> PlacementResult:
    """Build the ACCEPTED result from a Kambi bet echo. Both ``stake`` and
    ``betOdds`` are minor units ×1000 (÷1000 → ARS / decimal odds). Shared by the
    synchronous SUCCESS parse and the live-delay history poll so the unit
    conversion lives in exactly one place."""
    return PlacementResult(
        accepted=True,
        stake_filled=_f(bet.get("stake")) / 1000.0,
        odds_filled=_f(bet.get("betOdds")) / 1000.0,
        ref=str(ref),
    )


# ---- BetWarrior (Kambi) coupon/history.json poll classification ----
# A LIVE_DELAY_PENDING placement is resolved by polling the player-API
# ``coupon/history.json`` — the SAME authenticated GET the SPA and the auth-liveness
# probe use (proven 200 on cf-al-auth-api.kambicdn.com). The response wraps coupons in
# ``historyCoupons``; we match the placed ``couponRef`` and classify ``bets[0].betStatus``.
# NO ``status=`` query filter: a bet accepted during the live delay leaves the PENDING
# bucket (betStatus → OPEN) and would VANISH from a ``status=PENDING`` query, so only an
# unfiltered query can observe the accepted state. Grounded from the 2026-06-23 capture
# (couponRef 12796224824, betStatus WAITING_FOR_APPROVAL, stake 234720, betOdds 25000)
# and the 2026-06-01 recon (coupon/history.json → 200, empty historyCoupons).
BETWARRIOR_BET_OPEN = "OPEN"  # accepted — the bet is live
BETWARRIOR_BET_WAITING = "WAITING_FOR_APPROVAL"  # pending the live-delay window
# Known REJECTION literals (bet NOT accepted). Matched case-insensitively so a Kambi
# casing change can't turn a reject into a pending-unknown false alarm.
_BETWARRIOR_BET_REJECTED = frozenset(
    {"REFUSED", "REJECTED", "CANCELLED", "DECLINED", "VOID", "NOT_ACCEPTED"}
)


class BetHistoryMatch(StrEnum):
    ACCEPTED = "accepted"  # betStatus OPEN + echoed stake/odds
    WAITING = "waiting"  # betStatus WAITING_FOR_APPROVAL — keep polling
    REJECTED = "rejected"  # a known reject literal — clean reject
    UNKNOWN = "unknown"  # not found / unrecognized status — keep polling


def match_betwarrior_coupon(
    resp: dict[str, Any], *, coupon_ref: Any, bet_ref: Any = None
) -> tuple[BetHistoryMatch, dict[str, Any]]:
    """Classify a placed BetWarrior coupon inside a ``coupon/history.json`` body.

    Returns ``(classification, matched_bet)`` — ``matched_bet`` is empty when the
    coupon isn't present. Matches on the top-level ``couponRef`` the placement
    echoes, falling back to ``betRef`` so a history variant that re-keys by bet is
    still found. ``ACCEPTED`` requires OPEN status AND echoed ``stake``+``betOdds``
    (the same strict gate as ``parse_betwarrior``) so the poll can never accept more
    loosely than a synchronous place."""
    coupons = resp.get("historyCoupons")
    if not isinstance(coupons, list):
        return BetHistoryMatch.UNKNOWN, {}
    for c in coupons:
        if not isinstance(c, dict):
            continue
        bets = c.get("bets")
        # Match the coupon by couponRef (primary) or by a bet carrying our betRef (fallback).
        coupon_match = c.get("couponRef") == coupon_ref or (
            bet_ref is not None
            and isinstance(bets, list)
            and any(isinstance(b, dict) and b.get("betRef") == bet_ref for b in bets)
        )
        if not coupon_match:
            continue
        # Identify OUR bet within the matched coupon. When we have a betRef, find it
        # EXPLICITLY — never bets[0]: a multi-bet coupon could classify a different bet
        # (e.g. ACCEPT an OPEN sibling while our betRef is still WAITING_FOR_APPROVAL).
        if bet_ref is not None and isinstance(bets, list):
            bet = next((b for b in bets if isinstance(b, dict) and b.get("betRef") == bet_ref), {})
        else:
            bet = _first(bets)
        status = str(bet.get("betStatus", "")).upper()
        if status == BETWARRIOR_BET_OPEN and "stake" in bet and "betOdds" in bet:
            return BetHistoryMatch.ACCEPTED, bet
        if status == BETWARRIOR_BET_WAITING:
            return BetHistoryMatch.WAITING, bet
        if status in _BETWARRIOR_BET_REJECTED:
            return BetHistoryMatch.REJECTED, bet
        return BetHistoryMatch.UNKNOWN, bet
    # Coupon not in the list yet (not propagated) or already resolved+gone.
    # Either way: keep polling; unresolved at the deadline → pending_unknown.
    return BetHistoryMatch.UNKNOWN, {}


def parse_betsson(resp: dict[str, Any]) -> PlacementResult:
    """``{"couponStatus":{"couponStatusPollingResult":"Success","couponId":...,
    "couponPlacementErrors":[]}}`` — no stake/odds echoed (poll separately)."""
    status = _d(resp.get("couponStatus"))
    errors = status.get("couponPlacementErrors") or []
    accepted = status.get("couponStatusPollingResult") == "Success" and not errors
    return PlacementResult(
        accepted=bool(accepted),
        ref=str(status.get("couponId", "")),
        detail="" if accepted else f"betsson: {errors or status.get('couponStatusPollingResult')}",
    )


@dataclass(frozen=True)
class BetssonOddsCorrection:
    """A Betsson ``E_BETTING_ODDS_INVALID`` price-confirmation: the server's current
    valid odds for our selection. ``valid_odds_str`` is the EXACT string Betsson
    returned (re-submitted verbatim so a >2-dp price isn't lost to rounding)."""

    valid_odds: float
    valid_odds_str: str
    selection_tag: str


def betsson_odds_correction(resp: dict[str, Any]) -> BetssonOddsCorrection | None:
    """The valid-odds correction from an ``E_BETTING_ODDS_INVALID`` rejection, or None.

    Fails closed (None) on an accepted coupon, any present ``couponId`` (a coupon may
    exist → never re-POST), a non-odds error, or an unparseable ``validOdds``."""
    status = _d(resp.get("couponStatus"))
    if status.get("couponStatusPollingResult") == "Success":
        return None
    coupon_id = str(status.get("couponId", "") or "")
    if coupon_id and coupon_id != "0":
        return None  # a coupon may have been created — do not re-POST over it
    for err in status.get("couponPlacementErrors") or []:
        if not isinstance(err, dict) or err.get("code") != "E_BETTING_ODDS_INVALID":
            continue
        details = _d(err.get("details"))
        raw = details.get("validOdds")
        if raw is None:
            continue
        try:
            vo = float(raw)
        except (TypeError, ValueError):
            continue
        if vo > 1.0:
            return BetssonOddsCorrection(
                valid_odds=vo,
                valid_odds_str=str(raw),
                selection_tag=str(details.get("marketSelectionTag", "")),
            )
    return None


_PARSERS = {
    "betano": parse_betano,
    "bplay": parse_bplay,
    "betwarrior": parse_betwarrior,
    "betsson": parse_betsson,
}


def parse_confirmation(platform: str, resp: dict[str, Any]) -> PlacementResult:
    """Dispatch to the platform's parser (matches on the base name, so
    'betsson-pba' → 'betsson'). Unknown platform → fail closed."""
    parser = _PARSERS.get(platform.split("-", 1)[0].lower())
    if parser is None:
        return PlacementResult(accepted=False, detail=f"no confirmation parser for {platform!r}")
    return parser(resp)
