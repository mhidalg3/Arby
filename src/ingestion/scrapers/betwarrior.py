"""BetWarrior PBA scraper.

BetWarrior PBA runs on the Kambi white-label platform (Swedish B2B
sportsbook tech). Brand ID `bwargbap` = BetWarrior Argentina BA
Province. The on-prem SPA at `pba.betwarrior.bet.ar` is a Shapegames-
orchestrated wrapper around the Kambi client; the actual odds API
lives at `eu.offering-api.kambicdn.com`.

The Kambi schema is stable across every Kambi tenant globally and
returns clean JSON. The "list view" endpoint
(`listView/football/<slug>/all/all/matches.json`) returns one HTTP
per competition per poll cycle carrying all that competition's
matches with their PRIMARY 1X2 betoffer attached. That's the MVP
target — single endpoint per competition, 1X2 across all events,
~31 KB response. Deeper markets (Asian Handicap, Over/Under,
BTTS) live in the per-event endpoint
(`betoffer/event/<id>.json`, ~434 KB) and are intentionally NOT
consumed here — adding them is a one-method addition once the
semantic layer is in flight.

API contract (frozen from recon, see `scripts/recon/RECON_LOG.md`
session 20260526-173113):

    GET https://eu.offering-api.kambicdn.com/offering/v2018/bwargbap/
        listView/football/<slug>/all/all/matches.json
        ?lang=es_AR&market=AR&client_id=2&channel_id=1
        Anonymous, plain httpx. Origin + Referer headers required to
        match the SPA's request shape.
        → 200 + application/json with the schema below.
        → 400 if `lang` is missing (the SPA always sends it).
        → other 4xx/5xx are treated as a per-competition skip (logged,
          cycle continues with the next competition).

Response schema (the parts we consume):

    {"events": [
        {"event": {"id": 1027027525, "name": "LDU Quito - Always Ready",
                   "state": "NOT_STARTED", "homeName": "LDU Quito",
                   "awayName": "Always Ready", ...},
         "betOffers": [
             {"id": 2649147284,
              "criterion": {"label": "Resultado Final"},
              "betOfferType": {"englishName": "Match"},
              "outcomes": [
                  {"id": ..., "label": "1", "odds": 1290, "status": "OPEN"},
                  {"id": ..., "label": "X", "odds": 5600, "status": "OPEN"},
                  {"id": ..., "label": "2", "odds": 9000, "status": "OPEN"}
              ]}
         ]}
    ]}

Two Kambi-specific gotchas:

1. **Odds are integer-scaled by 1000.** `odds: 1290` means decimal
   1.29. Divide by 1000 on emit.
2. **Outcome `status`** filters live offers. Only `"OPEN"` outcomes
   are emitted; suspended outcomes carry a different status.

Stake limits (`max_stake`, `min_stake`, `stake_increment`) live on
the logged-in bet-slip and are not in the public list-view payload.
Snapshots emit `max_stake=None`; the risk layer falls back to
platform-wide policy defaults.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from typing import Any, Final

import httpx
import structlog

from src.ingestion.rate_limit import CircuitOpenError, RateLimitGuard
from src.ingestion.scrapers.base import BaseScraper, RawOddsSnapshot

log = structlog.get_logger(__name__)


# ---- Frozen contract (see RECON_LOG.md session 20260526-173113) ----

BASE_URL: Final[str] = "https://eu.offering-api.kambicdn.com"
BRAND_ID: Final[str] = "bwargbap"  # BetWarrior Argentina BA Province
LIST_VIEW_PATH: Final[str] = "/offering/v2018/{brand}/listView/football/{slug}/all/all/matches.json"

# Required query params. `lang` is enforced by Kambi (bare requests
# return 400); the others are sent by the SPA and we mirror its shape
# for stability.
DEFAULT_QUERY_PARAMS: Final[dict[str, str]] = {
    "lang": "es_AR",
    "market": "AR",
    "client_id": "2",
    "channel_id": "1",
}

# Required cosmetic headers. Origin and Referer match what the SPA
# sends; without them the offering-api still answers, but mirroring
# the SPA shape is the recon-frozen contract.
SPA_ORIGIN: Final[str] = "https://pba.betwarrior.bet.ar"

# Competition slugs (Kambi `path[*].termKey`). Selected for direct
# overlap with what Betsson and Bplay cover, so cross-platform
# arb candidates surface as widely as possible.
#
# Confirmed live in `group.json` on 2026-05-26 with event counts:
#   brazil                          426
#   argentina                       150
#   world_cup_2026                  139
#   international_friendly_matches  101
#   copa_libertadores                28
#   copa_sudamericana                27
#   conference_league                 2 (varies)
#   champions_league                  5 (varies)
#
# `world_cup_2026` + `international_friendly_matches` together match
# Bplay's Copa Mundial XML feed (international qualifiers + friendlies).
TARGET_COMPETITIONS: Final[dict[str, str]] = {
    "argentina": "Argentina (domestic)",
    "brazil": "Brasil",
    "copa_libertadores": "Copa Libertadores",
    "copa_sudamericana": "Copa Sudamericana",
    "champions_league": "UEFA Champions League",
    "conference_league": "UEFA Conference League",
    "world_cup_2026": "Copa del Mundo 2026",
    "international_friendly_matches": "Amistosos Internacionales",
}

# The list-view endpoint carries ONLY the primary 1X2 betoffer per
# event. Filter defensively in case Kambi adds more in the future —
# we want this scraper to keep emitting clean 1X2 only until a
# deliberate widening.
TARGET_BET_OFFER_TYPE: Final[str] = "Match"

# Kambi serves odds as integers scaled by this factor.
KAMBI_ODDS_SCALE: Final[float] = 1000.0

# Conservative per-request timeout. List-view responses are small
# (~31 KB) and typically sub-300ms; 15s leaves headroom for a
# transient Cloudflare slowness without stalling the poll loop.
DEFAULT_HTTP_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(15.0, connect=5.0)


class BetWarriorContractError(RuntimeError):
    """Kambi returned a payload that doesn't match the recon-frozen contract.

    Raised on HTTP failures, non-JSON bodies, and missing/wrong-typed
    keys in the response. The per-competition loop catches and skips
    so one bad competition doesn't kill the cycle; a sustained
    schema break recurs across all target competitions and surfaces
    clearly in logs.
    """


class BetWarriorPbaScraper(BaseScraper):
    """Polls Kambi's per-competition list-view feeds for soccer 1X2 odds.

    One HTTP per competition per poll cycle gets all matches in that
    competition with their primary 1X2 betoffer. Deeper markets (AH,
    O/U, BTTS) require the per-event endpoint and are intentionally
    out of scope for this scraper.
    """

    poll_interval_sec = 5.0
    backoff_seconds_on_error = 30.0
    platform_name = "betwarrior-pba"

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        competitions: dict[str, str] | None = None,
        guard: RateLimitGuard | None = None,
    ) -> None:
        self._client = http_client
        self._competitions = competitions if competitions is not None else dict(TARGET_COMPETITIONS)
        self._guard = guard or RateLimitGuard(platform=self.platform_name)
        self._log = log.bind(platform=self.platform_name)
        # Mirrors the SPA's request shape. The offering-api itself
        # does not enforce these headers, but anchoring on the SPA's
        # shape is the cheapest insurance against a future Kambi-side
        # header check.
        self._platform_headers: dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "Origin": SPA_ORIGIN,
            "Referer": f"{SPA_ORIGIN}/",
        }

    async def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]:
        for slug, label in self._competitions.items():
            try:
                async for snapshot in self._fetch_competition(slug, label):
                    yield snapshot
            except BetWarriorContractError as exc:
                self._log.warning(
                    "scraper.competition_skipped",
                    competition_slug=slug,
                    competition_label=label,
                    error=str(exc),
                )

    # ---- internals ----

    async def _fetch_competition(self, slug: str, label: str) -> AsyncIterator[RawOddsSnapshot]:
        url = BASE_URL + LIST_VIEW_PATH.format(brand=BRAND_ID, slug=slug)
        data = await self._get_json(url)

        events = data.get("events")
        if not isinstance(events, list):
            raise BetWarriorContractError(
                f"listView for {slug!r} missing 'events' list (got {type(events).__name__})"
            )

        observed_at = time.time()
        for block in events:
            if not isinstance(block, dict):
                continue
            event = block.get("event")
            bet_offers = block.get("betOffers")
            if not isinstance(event, dict) or not isinstance(bet_offers, list):
                continue
            for snapshot in self._snapshots_for_event(event, bet_offers, observed_at):
                yield snapshot

    def _snapshots_for_event(
        self,
        event: dict[str, Any],
        bet_offers: list[Any],
        observed_at: float,
    ) -> Iterator[RawOddsSnapshot]:
        event_id = event.get("id")
        if event_id is None:
            return
        event_id_str = str(event_id)
        raw_event_name = _event_name(event)

        for offer in bet_offers:
            if not isinstance(offer, dict):
                continue
            bo_type = offer.get("betOfferType")
            if not isinstance(bo_type, dict):
                continue
            if bo_type.get("englishName") != TARGET_BET_OFFER_TYPE:
                continue

            market_id = offer.get("id")
            if market_id is None:
                continue
            market_id_str = str(market_id)

            criterion = offer.get("criterion")
            criterion_label = criterion.get("label") if isinstance(criterion, dict) else None
            raw_market_name = (
                criterion_label if isinstance(criterion_label, str) else TARGET_BET_OFFER_TYPE
            )

            outcomes = offer.get("outcomes")
            if not isinstance(outcomes, list):
                continue

            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                if outcome.get("status") != "OPEN":
                    continue
                odds_raw = outcome.get("odds")
                if not isinstance(odds_raw, int | float):
                    continue
                decimal_odds = float(odds_raw) / KAMBI_ODDS_SCALE
                if decimal_odds <= 1.0:
                    # Below-unity decimal odds either suspended or
                    # nonsense — downstream OddsQuote would reject them.
                    continue

                outcome_id = outcome.get("id")
                if outcome_id is None:
                    continue
                outcome_label = outcome.get("label")
                if not isinstance(outcome_label, str):
                    continue

                yield RawOddsSnapshot(
                    platform=self.platform_name,
                    platform_event_id=event_id_str,
                    platform_market_id=market_id_str,
                    platform_outcome_id=str(outcome_id),
                    raw_event_name=raw_event_name,
                    raw_market_name=raw_market_name,
                    raw_outcome_name=outcome_label,
                    decimal_odds=decimal_odds,
                    max_stake=None,
                    timestamp=observed_at,
                )

    async def _get_json(self, url: str) -> Any:
        try:
            resp = await self._guard.get(
                lambda: self._client.get(
                    url,
                    params=DEFAULT_QUERY_PARAMS,
                    headers=self._platform_headers,
                    timeout=DEFAULT_HTTP_TIMEOUT,
                )
            )
        except CircuitOpenError as exc:
            raise BetWarriorContractError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise BetWarriorContractError(f"GET {url} failed: {exc!s}") from exc
        if resp.status_code >= 400:
            raise BetWarriorContractError(f"GET {url} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise BetWarriorContractError(f"GET {url} returned non-JSON body: {exc!s}") from exc


def _event_name(event: dict[str, Any]) -> str:
    """Prefer the canonical Kambi `name` field; fall back to
    `<homeName> - <awayName>` if absent."""
    name = event.get("name")
    if isinstance(name, str) and name:
        return name
    home = event.get("homeName")
    away = event.get("awayName")
    if isinstance(home, str) and isinstance(away, str) and home and away:
        return f"{home} - {away}"
    event_id = event.get("id")
    return f"Event {event_id}" if event_id is not None else ""


def _snapshots_for_kambi_match(
    offer: dict[str, Any],
    event_id: str,
    raw_event_name: str,
    observed_at: float,
    platform_name: str,
) -> list[RawOddsSnapshot]:
    """Extract Match (1X2) snapshots from a Kambi betoffer.

    Shared helper — used by both the list-view scraper (during
    normal polling) and the depth scraper's `fetch_event_quotes`
    (surgical refetch for the verifier). Mirrors the list-view's
    `_snapshots_for_event` logic but exposed at module level so the
    depth scraper can call it without subclassing.
    """
    market_id = offer.get("id")
    if market_id is None:
        return []
    market_id_str = str(market_id)
    criterion = offer.get("criterion")
    criterion_label = criterion.get("label") if isinstance(criterion, dict) else None
    raw_market_name = (
        criterion_label if isinstance(criterion_label, str) else TARGET_BET_OFFER_TYPE
    )
    outcomes = offer.get("outcomes")
    if not isinstance(outcomes, list):
        return []
    snapshots: list[RawOddsSnapshot] = []
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        if outcome.get("status") != "OPEN":
            continue
        odds_raw = outcome.get("odds")
        if not isinstance(odds_raw, int | float):
            continue
        decimal_odds = float(odds_raw) / KAMBI_ODDS_SCALE
        if decimal_odds <= 1.0:
            continue
        outcome_id = outcome.get("id")
        if outcome_id is None:
            continue
        outcome_label = outcome.get("label")
        if not isinstance(outcome_label, str):
            continue
        snapshots.append(
            RawOddsSnapshot(
                platform=platform_name,
                platform_event_id=event_id,
                platform_market_id=market_id_str,
                platform_outcome_id=str(outcome_id),
                raw_event_name=raw_event_name,
                raw_market_name=raw_market_name,
                raw_outcome_name=outcome_label,
                decimal_odds=decimal_odds,
                max_stake=None,
                timestamp=observed_at,
            )
        )
    return snapshots


# =============================================================================
# Depth scraper — per-event polling for BTTS + OU goals.
# =============================================================================
#
# The list-view scraper above gives us 1X2 across all events in a
# competition with one HTTP per competition. Deeper markets (BTTS,
# OU goals, Asian Handicap, player props) live in the per-event
# endpoint, which returns ~434 KB and ~470 betoffers per event.
#
# This scraper polls per-event for BTTS + OU goals only. AH and
# player props are deliberately out of scope for v1 — they need
# resolver crosswalks that haven't been built yet.
#
# Bandwidth math:
#   ~100 active events × 434 KB = ~43 MB per depth poll cycle
#   At poll_interval_sec = 30, sustained rate ≈ 1.4 MB/s
# This is acceptable for a single-machine deployment. Lower to
# 60s or restrict the event set if it ever becomes a concern.
#
# Same `platform_name = "betwarrior-pba"` as the list-view scraper.
# Snapshots from both scrapers flow into the same `odds:raw` stream
# and the canonicalizer treats them uniformly via the existing cache.

BET_OFFER_PATH: Final[str] = "/offering/v2018/{brand}/betoffer/event/{event_id}.json"

# Kambi criterion labels we consume in v1. Stable across all Kambi
# tenants we've recon'd; confirmed in the live stream on 2026-05-26.
CRITERION_BTTS: Final[str] = "Ambos Equipos Marcarán"
CRITERION_OU_GOALS: Final[str] = "Total de goles"

# Kambi betOfferType.englishName values for the markets we want.
BET_OFFER_TYPE_BTTS: Final[str] = "Yes/No"
BET_OFFER_TYPE_OU: Final[str] = "Over/Under"

# Outcome line is integer-scaled by the same factor as odds. line=2500
# means decimal 2.5.
KAMBI_LINE_SCALE: Final[float] = 1000.0


class BetWarriorPbaDepthScraper(BaseScraper):
    """Per-event depth poll for BetWarrior — BTTS + OU goals.

    Polls Kambi's `betoffer/event/<id>.json` endpoint for each event
    discovered from the list-view. Slower cadence than the list-view
    scraper because each per-event response is ~100× larger.
    """

    poll_interval_sec = 30.0
    backoff_seconds_on_error = 60.0
    platform_name = "betwarrior-pba"

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        competitions: dict[str, str] | None = None,
        guard: RateLimitGuard | None = None,
    ) -> None:
        self._client = http_client
        self._competitions = (
            competitions if competitions is not None else dict(TARGET_COMPETITIONS)
        )
        self._guard = guard or RateLimitGuard(platform=self.platform_name)
        self._log = log.bind(platform=self.platform_name, mode="depth")
        self._platform_headers: dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "Origin": SPA_ORIGIN,
            "Referer": f"{SPA_ORIGIN}/",
        }

    async def fetch_event_quotes(self, event_id: str) -> list[RawOddsSnapshot]:
        """Surgical per-event refetch — returns ALL v1 markets (1X2 +
        BTTS + OU goals) for the given event in one HTTP call.

        Used by the Tier-2 pre-execution verifier. Unlike the
        regular polling path which separates 1X2 (list-view) from
        BTTS/OU (depth), this single endpoint call returns every
        v1 market the event currently exposes — the same per-event
        endpoint already used by `_fetch_event_depth`, just with
        the Match (1X2) betoffer also passed through.
        """
        url = BASE_URL + BET_OFFER_PATH.format(brand=BRAND_ID, event_id=event_id)
        data = await self._get_json(url)
        bet_offers = data.get("betOffers")
        if not isinstance(bet_offers, list):
            raise BetWarriorContractError(
                f"betoffer/event/{event_id} missing 'betOffers' list"
            )
        observed_at = time.time()
        # `raw_event_name` populated when the response carries event
        # metadata; otherwise empty. The verifier doesn't use it
        # (it matches on `platform_outcome_id`).
        raw_event_name = ""
        snapshots: list[RawOddsSnapshot] = []
        for offer in bet_offers:
            if not isinstance(offer, dict):
                continue
            bo_type = offer.get("betOfferType")
            criterion = offer.get("criterion")
            if not isinstance(bo_type, dict) or not isinstance(criterion, dict):
                continue
            bo_type_name = bo_type.get("englishName")
            criterion_label = criterion.get("label")

            # 1X2 — Match betoffer
            if bo_type_name == TARGET_BET_OFFER_TYPE:
                snapshots.extend(
                    _snapshots_for_kambi_match(
                        offer, event_id, raw_event_name, observed_at,
                        platform_name=self.platform_name,
                    )
                )
            elif (
                bo_type_name == BET_OFFER_TYPE_BTTS
                and criterion_label == CRITERION_BTTS
            ):
                snapshots.extend(
                    self._snapshots_for_btts(
                        event_id, raw_event_name, offer, observed_at
                    )
                )
            elif (
                bo_type_name == BET_OFFER_TYPE_OU
                and criterion_label == CRITERION_OU_GOALS
            ):
                snapshots.extend(
                    self._snapshots_for_ou_goals(
                        event_id, raw_event_name, offer, observed_at
                    )
                )
        return snapshots

    async def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]:
        # First, enumerate active events from the list-view (cheap;
        # ~31 KB per competition). Then poll each event for depth.
        event_ids: list[tuple[str, str]] = []  # (event_id, raw_event_name)
        for slug, label in self._competitions.items():
            try:
                async for event in self._discover_events(slug, label):
                    event_ids.append(event)
            except BetWarriorContractError as exc:
                self._log.warning(
                    "depth.list_view_skipped",
                    competition_slug=slug,
                    error=str(exc),
                )

        self._log.debug("depth.events_discovered", count=len(event_ids))

        # Per-event depth poll. Sequential — per-event responses are
        # large (~434 KB) and concurrent calls would burst bandwidth
        # without changing the cycle time meaningfully (most time is
        # in JSON parsing, not network).
        for event_id, raw_event_name in event_ids:
            try:
                async for snapshot in self._fetch_event_depth(
                    event_id, raw_event_name
                ):
                    yield snapshot
            except BetWarriorContractError as exc:
                self._log.warning(
                    "depth.event_skipped",
                    event_id=event_id,
                    error=str(exc),
                )

    # ---- internals ----

    async def _discover_events(
        self, slug: str, label: str
    ) -> AsyncIterator[tuple[str, str]]:
        url = BASE_URL + LIST_VIEW_PATH.format(brand=BRAND_ID, slug=slug)
        data = await self._get_json(url)
        events = data.get("events")
        if not isinstance(events, list):
            raise BetWarriorContractError(
                f"listView for {slug!r} missing 'events' list"
            )
        for block in events:
            if not isinstance(block, dict):
                continue
            event = block.get("event")
            if not isinstance(event, dict):
                continue
            event_id = event.get("id")
            if event_id is None:
                continue
            yield str(event_id), _event_name(event)

    async def _fetch_event_depth(
        self, event_id: str, raw_event_name: str
    ) -> AsyncIterator[RawOddsSnapshot]:
        url = BASE_URL + BET_OFFER_PATH.format(brand=BRAND_ID, event_id=event_id)
        data = await self._get_json(url)
        bet_offers = data.get("betOffers")
        if not isinstance(bet_offers, list):
            raise BetWarriorContractError(
                f"betoffer/event/{event_id} missing 'betOffers' list"
            )

        observed_at = time.time()
        for offer in bet_offers:
            if not isinstance(offer, dict):
                continue
            bo_type = offer.get("betOfferType")
            criterion = offer.get("criterion")
            if not isinstance(bo_type, dict) or not isinstance(criterion, dict):
                continue

            bo_type_name = bo_type.get("englishName")
            criterion_label = criterion.get("label")

            if (
                bo_type_name == BET_OFFER_TYPE_BTTS
                and criterion_label == CRITERION_BTTS
            ):
                for snapshot in self._snapshots_for_btts(
                    event_id, raw_event_name, offer, observed_at
                ):
                    yield snapshot
            elif (
                bo_type_name == BET_OFFER_TYPE_OU
                and criterion_label == CRITERION_OU_GOALS
            ):
                for snapshot in self._snapshots_for_ou_goals(
                    event_id, raw_event_name, offer, observed_at
                ):
                    yield snapshot
            # All other markets (AH, player props, corners, cards, etc.)
            # are silently skipped — not in v1 scope.

    def _snapshots_for_btts(
        self,
        event_id: str,
        raw_event_name: str,
        offer: dict[str, Any],
        observed_at: float,
    ) -> Iterator[RawOddsSnapshot]:
        market_id = offer.get("id")
        if market_id is None:
            return
        market_id_str = str(market_id)
        outcomes = offer.get("outcomes")
        if not isinstance(outcomes, list):
            return

        for outcome in outcomes:
            snapshot = self._snapshot_from_outcome(
                outcome=outcome,
                event_id=event_id,
                raw_event_name=raw_event_name,
                raw_market_name=CRITERION_BTTS,
                market_id_str=market_id_str,
                observed_at=observed_at,
            )
            if snapshot is not None:
                yield snapshot

    def _snapshots_for_ou_goals(
        self,
        event_id: str,
        raw_event_name: str,
        offer: dict[str, Any],
        observed_at: float,
    ) -> Iterator[RawOddsSnapshot]:
        market_id = offer.get("id")
        if market_id is None:
            return
        outcomes = offer.get("outcomes")
        if not isinstance(outcomes, list):
            return

        # Every outcome in an OU betOffer carries the same line; read
        # it once from the first valid outcome. Each betOffer = one
        # line, so one canonical market_id per betOffer is correct.
        line_raw: int | float | None = None
        for outcome in outcomes:
            if isinstance(outcome, dict):
                candidate = outcome.get("line")
                if isinstance(candidate, int | float):
                    line_raw = candidate
                    break
        if line_raw is None:
            return
        line = float(line_raw) / KAMBI_LINE_SCALE
        # Half-lines only: integer goal totals are push lines and break
        # the {OVER, UNDER} partition.
        if abs((line * 2) - round(line * 2)) > 1e-9 or (round(line * 2)) % 2 == 0:
            return

        # raw_market_name follows the Betsson-style format `"Total de
        # goles 2.5"` so the existing market_resolver regex pattern
        # (registered for `betwarrior-pba` with the same regex as
        # Betsson) extracts the line cleanly.
        raw_market_name = f"{CRITERION_OU_GOALS} {line:g}"
        market_id_str = f"{market_id}-line{line:g}"

        for outcome in outcomes:
            snapshot = self._snapshot_from_outcome(
                outcome=outcome,
                event_id=event_id,
                raw_event_name=raw_event_name,
                raw_market_name=raw_market_name,
                market_id_str=market_id_str,
                observed_at=observed_at,
            )
            if snapshot is not None:
                yield snapshot

    def _snapshot_from_outcome(
        self,
        outcome: Any,
        event_id: str,
        raw_event_name: str,
        raw_market_name: str,
        market_id_str: str,
        observed_at: float,
    ) -> RawOddsSnapshot | None:
        if not isinstance(outcome, dict):
            return None
        if outcome.get("status") != "OPEN":
            return None
        odds_raw = outcome.get("odds")
        if not isinstance(odds_raw, int | float):
            return None
        decimal_odds = float(odds_raw) / KAMBI_ODDS_SCALE
        if decimal_odds <= 1.0:
            return None
        outcome_id = outcome.get("id")
        if outcome_id is None:
            return None
        outcome_label = outcome.get("label")
        if not isinstance(outcome_label, str):
            return None
        return RawOddsSnapshot(
            platform=self.platform_name,
            platform_event_id=event_id,
            platform_market_id=market_id_str,
            platform_outcome_id=str(outcome_id),
            raw_event_name=raw_event_name,
            raw_market_name=raw_market_name,
            raw_outcome_name=outcome_label,
            decimal_odds=decimal_odds,
            max_stake=None,
            timestamp=observed_at,
        )

    async def _get_json(self, url: str) -> Any:
        try:
            resp = await self._guard.get(
                lambda: self._client.get(
                    url,
                    params=DEFAULT_QUERY_PARAMS,
                    headers=self._platform_headers,
                    timeout=DEFAULT_HTTP_TIMEOUT,
                )
            )
        except CircuitOpenError as exc:
            raise BetWarriorContractError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise BetWarriorContractError(f"GET {url} failed: {exc!s}") from exc
        if resp.status_code >= 400:
            raise BetWarriorContractError(
                f"GET {url} returned HTTP {resp.status_code}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise BetWarriorContractError(
                f"GET {url} returned non-JSON body: {exc!s}"
            ) from exc
