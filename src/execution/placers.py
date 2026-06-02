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

from typing import Any

from src.execution.executor import PlacementResult


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
        return PlacementResult(accepted=False, detail="betano: not accepted")
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
