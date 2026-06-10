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
    *,
    outcome_id: int,
    odds_x100: int,
    stake_thousandths: int,
    allow_odds_change: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Kambi `/coupon.json`. Odds are ×100, stake ×1000 (Kambi minor units).

    `allow_odds_change` "NO" (default) rejects the bet if the line moved since we read
    it ("Invalid odds specified") — correct for an arb, where the exact odds are the
    edge. "YES" accepts the book's current odds; used for the mechanics trial (and the
    seam for a future live-re-verify path) on fast-moving lines."""
    flag = "YES" if allow_odds_change else "NO"
    return {
        "couponRows": [{"index": 0, "odds": odds_x100, "outcomeId": outcome_id, "type": "SIMPLE"}],
        "allowOddsChange": flag,
        "allowOddsChangeLive": flag,
        "allowOddsChangePreMatch": flag,
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


def build_bplay_togglebet(outcome_id: int, csrf_token: str) -> dict[str, Any]:
    """Bplay `/bettingslip/togglebet` — adds the selection to the server-side
    slip. `csrf_token` is the current (bootstrap) token; the response returns a
    fresh one under ``header.csrf_token`` for the next call."""
    return {"context": _bplay_context("/"), "data": {"id": outcome_id, "csrf_token": csrf_token}}


def bplay_csrf_from_response(resp: dict[str, Any]) -> str:
    """The rotated CSRF a slip response hands forward (``header.csrf_token``)."""
    return str(_d(resp.get("header")).get("csrf_token", "") or "")


def build_bplay_request(
    *, url_key: str, outcome_id: int, stake_ars: float, csrf_token: str, date_ms: int
) -> dict[str, Any]:
    """Bplay SportNCO `/bettingslip` (place). `csrf_token` is the latest rotated
    token (from the togglebet response). The per-outcome ``stake`` map is in
    thousandths of ARS (capture: total ``"1.00"`` ↔ map ``1000``); ``date_ms`` is
    the current epoch-ms."""
    return {
        "context": _bplay_context(url_key),
        "data": {
            "data": {
                "date": date_ms,
                "betslip": {
                    "nb_bettingslip_totalStake": f"{stake_ars:.2f}",
                    "freebet": None,
                    "formule": "single",
                    "combiboost_id": None,
                    "accept": True,
                    "stake": {str(outcome_id): round(stake_ars * 1000)},
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


def parse_betwarrior(resp: dict[str, Any]) -> PlacementResult:
    """``{"status":"SUCCESS","couponRef":N,"coupon":{"bets":[{"betOdds","stake"}]}}``
    Kambi units: odds are ×100, stake is ×1000."""
    if resp.get("status") != "SUCCESS":
        return PlacementResult(accepted=False, detail=f"betwarrior: status={resp.get('status')}")
    bet = _first(_d(resp.get("coupon")).get("bets"))
    return PlacementResult(
        accepted=True,
        stake_filled=_f(bet.get("stake")) / 1000.0,
        odds_filled=_f(bet.get("betOdds")) / 100.0,
        ref=str(resp.get("couponRef", "")),
    )


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
