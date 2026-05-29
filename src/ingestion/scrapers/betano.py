"""Betano (Argentina) scraper — Kaizen Gaming "danae" backend.

Betano runs the Kaizen Gaming platform. Both the pre-match and in-play
odds feeds return the SAME normalized JSON shape — three id-keyed dicts,
`events` / `markets` / `selections` — so a single parser
(`_parse_danae_soccer_1x2`) serves both. The two feeds differ only in
endpoint, response nesting, and how fresh the data needs to be:

    live (in-play):
        GET /danae-webapi/api/live/overview/latest
            ?includeVirtuals=true&queryLanguageId=8&queryOperatorId=19
        → {events, markets, selections, ...}  (top-level)

    prematch:
        GET /api/home/top-events-v2/
        → {data: {topEventsV2: {events, markets, selections, ...}}}

In-play odds move every few seconds; pre-match odds drift over minutes.
Rather than fetch both in one pass at one cadence, this scraper is
parameterized by `mode` (like the Betsson scraper is by province): run
one `BetanoScraper(mode="live")` and one `BetanoScraper(mode="prematch")`,
each with its own poll interval. Both emit `platform="betano"` — they are
the same book, so downstream must not treat them as two platforms (that
would be a false self-arbitrage).

Contract frozen from recon session `20260529-150524`
(`recon/artifacts/betano/`). The canonical 1X2 market is **MRES** /
`typeId 1` / "Resultado del partido" (3 selections named 1 / X / 2 →
home / draw / away). The `MR12` "SuperCuotas" promo market is
deliberately ignored: enhanced-odds promos carry different stake limits
and terms and are unsafe for clean cross-platform arbitrage.

ANTI-BOT CAVEAT: Betano sits behind Cloudflare + Kaizen's own protection,
and the API was only ever confirmed reachable via a real browser during
recon. Whether a plain `httpx` client clears Cloudflare has NOT been
verified — that is exactly what the first live run will test. We send
browser-like headers to maximize the chance, but if the feed returns a
challenge (403 / HTML splash) the circuit breaker will open and the run
will surface a `BetanoContractError`. Do not "fix" that by hammering;
see `scripts/recon/README.md` and the recon memory.

Stake limits are not in the public feed (`max_stake=None`); downstream
falls back to platform policy defaults, as with the other scrapers.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from typing import Any, Literal

import httpx
import structlog

from src.ingestion.rate_limit import CircuitOpenError, RateLimitGuard
from src.ingestion.scrapers.base import BaseScraper, RawOddsSnapshot

log = structlog.get_logger(__name__)


# ---- Frozen contract (recon 20260529-150524) ----

BetanoMode = Literal["live", "prematch"]

BASE_URL = "https://www.betano.bet.ar"

# Per-mode endpoint + default cadence. Live is volatile (poll fast);
# pre-match drifts slowly (poll lazily, less traffic = less detection).
_MODE_PATH: dict[BetanoMode, str] = {
    "live": "/danae-webapi/api/live/overview/latest",
    "prematch": "/api/home/top-events-v2/",
}
_MODE_PARAMS: dict[BetanoMode, dict[str, str]] = {
    # Observed verbatim in recon. queryOperatorId=19 is Betano AR.
    "live": {
        "includeVirtuals": "true",
        "queryLanguageId": "8",
        "queryOperatorId": "19",
    },
    "prematch": {},
}
_MODE_POLL_INTERVAL_SEC: dict[BetanoMode, float] = {
    "live": 4.0,
    "prematch": 45.0,
}

# Canonical 1X2: MRES / typeId 1 ("Resultado del partido"). The MR12
# "SuperCuotas" promo (typeId 2850) is intentionally excluded.
MARKET_1X2_TYPE_ID = 1

# 1X2 selection short-name → our outcome role. The feed labels the three
# selections "1" / "X" / "2"; we resolve 1/2 to the participant names so
# the semantic layer has the team string, not a bare digit.
_SELECTION_HOME = "1"
_SELECTION_DRAW = "X"
_SELECTION_AWAY = "2"

# Conservative HTTP timeout. The feed answers in <1s; headroom for CF.
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Browser-like headers. Betano is Cloudflare-fronted; a bare httpx UA is
# more likely to draw a challenge. These mirror what the recon browser
# sent (reading public odds — no auth, no evasion of access controls).
_PLATFORM_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-AR,es;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/",
}


class BetanoContractError(RuntimeError):
    """Betano returned a payload that doesn't match the recon-frozen
    contract (or a Cloudflare challenge instead of JSON).

    The alertable "the platform changed something / blocked us" error.
    Raise it, log it, fix the scraper or back off — do not catch-and-retry
    at the framework level.
    """


class BetanoScraper(BaseScraper):
    """One Betano odds feed. `mode="live"` polls the in-play overview;
    `mode="prematch"` polls the top pre-match coupon. Run one instance per
    mode for full coverage; both report `platform="betano"`."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        mode: BetanoMode = "live",
        guard: RateLimitGuard | None = None,
    ) -> None:
        if mode not in _MODE_PATH:
            raise ValueError(f"unknown Betano mode {mode!r}; expected 'live' or 'prematch'")
        self.mode = mode
        self.platform_name = "betano"
        self.poll_interval_sec = _MODE_POLL_INTERVAL_SEC[mode]
        self._client = http_client
        self._url = f"{BASE_URL}{_MODE_PATH[mode]}"
        self._params = _MODE_PARAMS[mode]
        self._guard = guard or RateLimitGuard(platform=f"betano-{mode}")
        self._log = log.bind(platform=self.platform_name, mode=mode)

    async def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]:
        data = await self._get_json()
        block = self._extract_feed_block(data)
        observed_at = time.time()
        n = 0
        for snapshot in _parse_danae_soccer_1x2(block, observed_at=observed_at):
            n += 1
            yield snapshot
        self._log.debug("scraper.parsed", snapshots=n)

    # ---- internals ----

    def _extract_feed_block(self, data: Any) -> dict[str, Any]:
        """Pull the normalized `{events, markets, selections}` block out of
        the mode-specific response envelope."""
        block = data
        if self.mode == "prematch":
            try:
                block = data["data"]["topEventsV2"]
            except (KeyError, TypeError) as exc:
                raise BetanoContractError(
                    f"top-events-v2 missing 'data.topEventsV2': {exc}"
                ) from exc
        if not isinstance(block, dict) or not (
            isinstance(block.get("events"), dict)
            and isinstance(block.get("markets"), dict)
            and isinstance(block.get("selections"), dict)
        ):
            raise BetanoContractError(f"{self.mode} feed missing events/markets/selections dicts")
        return block

    async def _get_json(self) -> Any:
        """GET the feed as JSON. Wraps network/HTTP/parse errors uniformly."""
        try:
            resp = await self._guard.get(
                lambda: self._client.get(
                    self._url,
                    params=self._params,
                    headers=_PLATFORM_HEADERS,
                    timeout=DEFAULT_HTTP_TIMEOUT,
                )
            )
        except CircuitOpenError as exc:
            raise BetanoContractError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise BetanoContractError(f"GET {self._url} failed: {exc!s}") from exc
        if resp.status_code >= 400:
            raise BetanoContractError(f"GET {self._url} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            # A Cloudflare challenge serves HTML, not JSON — lands here.
            raise BetanoContractError(
                f"GET {self._url} returned non-JSON body (Cloudflare challenge?): {exc!s}"
            ) from exc


def _parse_danae_soccer_1x2(
    block: dict[str, Any], *, observed_at: float
) -> Iterator[RawOddsSnapshot]:
    """Yield 1X2 snapshots for real soccer matches from a danae feed block.

    Pure function over the normalized `{events, markets, selections}`
    shape shared by the live and pre-match endpoints. Skips outright
    markets, virtuals, and esports; emits only the standard MRES 1X2
    market (home / draw / away) for events with two participants.
    """
    events: dict[str, Any] = block["events"]
    markets: dict[str, Any] = block["markets"]
    selections: dict[str, Any] = block["selections"]

    for event in events.values():
        if not isinstance(event, dict) or event.get("sportId") != "FOOT":
            continue
        if event.get("isVirtual"):
            continue
        url = str(event.get("url", ""))
        if "esports" in url.lower() or url.startswith("/virtuals/"):
            continue
        participants = event.get("participants")
        if not (isinstance(participants, list) and len(participants) == 2):
            continue  # outright / non-head-to-head
        home = str(participants[0].get("name", "")).strip()
        away = str(participants[1].get("name", "")).strip()
        if not home or not away:
            continue
        raw_event_name = f"{home} vs {away}"

        market = _find_1x2_market(event.get("marketIdList", []), markets)
        if market is None:
            continue
        market_id = str(market.get("id", ""))
        raw_market_name = str(market.get("name", "Resultado del partido"))

        for sel_id in market.get("selectionIdList", []):
            sel = selections.get(str(sel_id))
            if not isinstance(sel, dict):
                continue
            role = str(sel.get("shortName") or sel.get("name") or "")
            outcome = _outcome_label(role, home, away)
            if outcome is None:
                continue
            price = sel.get("price")
            if not isinstance(price, int | float) or price <= 1.0:
                continue
            yield RawOddsSnapshot(
                platform="betano",
                platform_event_id=str(event.get("id", "")),
                platform_market_id=market_id,
                platform_outcome_id=str(sel.get("id", "")),
                raw_event_name=raw_event_name,
                raw_market_name=raw_market_name,
                raw_outcome_name=outcome,
                decimal_odds=float(price),
                max_stake=None,  # not in public feed; bet-slip only
                timestamp=observed_at,
            )


def _find_1x2_market(market_id_list: Any, markets: dict[str, Any]) -> dict[str, Any] | None:
    """Return the MRES (typeId 1) market among an event's markets, else None."""
    if not isinstance(market_id_list, list):
        return None
    for mid in market_id_list:
        market = markets.get(str(mid))
        if isinstance(market, dict) and market.get("typeId") == MARKET_1X2_TYPE_ID:
            return market
    return None


def _outcome_label(role: str, home: str, away: str) -> str | None:
    """Map a 1X2 selection's short name to a human outcome label, or None
    if it isn't one of the three expected 1 / X / 2 selections."""
    if role == _SELECTION_HOME:
        return home
    if role == _SELECTION_AWAY:
        return away
    if role == _SELECTION_DRAW:
        return "Empate"
    return None
