"""Betsson scraper for the three Argentine provincial sportsbooks.

`pba.betsson.bet.ar`, `caba.betsson.bet.ar`, and `cba.betsson.bet.ar`
all run the same OBG-platform backend (channel strings carry
`?obg/sportsbook/transient/...`) with the same `/api/sb/v1/widgets/...`
surface. They differ only in jurisdiction (regulator), and possibly in
the odds returned per province (separate licensing). This module is
parameterized by `subdomain` so one instance covers one province; run
three for cross-provincial coverage.

API contract (frozen from recon, see `scripts/recon/RECON_LOG.md`
session 20260525-210248):

    GET /api/sb/v1/widgets/categories/v2
        → 2.8MB sport / country / league / match tree (cached, refresh
          on a multi-minute TTL — too heavy to re-fetch every poll).

    GET /api/sb/v1/widgets/accordion/v1
        ?eventId=f-<id>
        &marketTemplateIds=MW3W,BTTS,MTG2W
        → markets + selections for the three target market types in
          one round trip.

Market codes consumed (the canonical OBG `marketTemplateId`s):
    MW3W   — 1X2 / "Ganador del partido"  (selectionTemplateId: HOME / DRAW / AWAY)
    BTTS   — both teams to score          (YES / NO)
    MTG2W  — match total goals O/U        (OVER / UNDER, one market per line)

The scraper is intentionally dumb: when the response shape doesn't
match the recon-frozen contract it raises `BetssonContractError`. Per
`docs/architecture.md`, we don't try to reason about contract drift at
runtime — we fix the scraper manually.

Stake limits (`max_stake`, `min_stake`, `stake_increment`) are NOT in
the public response; they appear only on the logged-in bet slip. Until
a logged-in recon pass captures them, snapshots carry `max_stake=None`
and downstream code falls back to platform-wide policy defaults.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from src.ingestion.rate_limit import CircuitOpenError, RateLimitGuard
from src.ingestion.scrapers.base import BaseScraper, RawOddsSnapshot

log = structlog.get_logger(__name__)


# ---- Frozen contract (see RECON_LOG.md sessions 20260525-205613 / 210248) ----

# Subdomain → x-sb-jurisdiction header value.
#
# The OBG backend dispatches per jurisdiction via the `x-sb-jurisdiction`
# header — without it (or with the wrong value), every odds endpoint
# returns HTTP 400. The recon-frozen value for PBA is "Iplyc" (Instituto
# Provincial de Lotería y Casinos, the Buenos Aires Province regulator).
#
# CABA and CBA use different provincial regulators (LOTBA and LCBA
# respectively in real life), but their exact x-sb-jurisdiction strings
# have NOT yet been confirmed via recon. They are excluded from the map
# until a separate recon session captures them; passing them to the
# constructor raises until that recon happens.
SUBDOMAIN_JURISDICTION = {
    "pba": "Iplyc",
}
VALID_SUBDOMAINS = frozenset(SUBDOMAIN_JURISDICTION.keys())
DEFAULT_SUBDOMAIN = "pba"

# Brand UUID captured from PBA request headers. The OBG backend
# REQUIRES this header on every API call (HTTP 400 with code
# `E_VALIDATION_INVALIDHEADER` if missing or wrong). Only PBA-confirmed;
# CABA/CBA may use different brand IDs.
PLATFORM_BRAND_ID = "238cb63a-3dcc-4fdf-b241-23a12cb71aa7"

# Country / market code. Also REQUIRED — same 400 if missing. "ag" =
# Argentina across all three provincial sites (their brand is per-AR,
# the jurisdiction split is at a different layer).
MARKET_CODE = "ag"

# Market template IDs we pull today.
MARKET_MW3W = "MW3W"
MARKET_BTTS = "BTTS"
MARKET_MTG2W = "MTG2W"
TARGET_MARKETS: tuple[str, ...] = (MARKET_MW3W, MARKET_BTTS, MARKET_MTG2W)

# Per-event accordion calls (one HTTP round-trip each) dominate the scrape; run
# them with bounded concurrency to cut wall time. Kept modest so the burst stays
# under the WAF's radar — the RateLimitGuard circuit-breaker is the backstop.
DEFAULT_MAX_CONCURRENT_EVENTS = 8

# Slug prefixes consumed from the categories tree. Each entry is a
# top-level Betsson grouping (`futbol/<group>/`); the scraper picks
# fixtures (depth-4 slugs `futbol/<group>/<league>/<match>`) under
# any of these prefixes.
#
# Selection rationale: include the competitions that also appear on
# BetWarrior and Bplay so cross-platform overlap is maximized. Local
# European leagues (alemania, espana, etc.) excluded for v1 to keep
# per-fixture polling cost bounded; revisit when those become a
# strategic priority.
#
# Confirmed live in the Betsson PBA categories tree on 2026-05-26.
DEFAULT_SOCCER_SLUG_PREFIXES: tuple[str, ...] = (
    "futbol/argentina/",
    "futbol/copa-libertadores/",
    "futbol/copa-sudamericana/",
    "futbol/champions-league/",
    "futbol/conference-league/",
    "futbol/mundial/",  # Copa del Mundo 2026
    "futbol/internacionales/",  # International friendlies + WC qualifiers
    "futbol/brasil/",
)
SLUG_MATCH_LEVEL_SEGMENTS = 4  # sport/country/league/match

# Fixture discovery is expensive; cache and refresh on a multi-minute TTL.
DEFAULT_FIXTURE_TTL_SEC = 300.0

# Conservative per-request HTTP timeout. The accordion endpoint typically
# answers in <500ms; this leaves headroom for AWS WAF challenges.
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# The odds (accordion) endpoint's WAF returns HTTP 403 (an HTML block page)
# to the default `python-httpx` User-Agent — confirmed live 2026-05-29. A
# browser UA gets a 200. (categories/v2 tolerates the python UA, which long
# masked this: discovery worked, every odds call 403'd → zero snapshots.)
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class BetssonContractError(RuntimeError):
    """The Betsson API returned a payload that doesn't match the recon-frozen contract.

    This is the alertable "the platform changed something" error. Raise
    it, log it, fix the scraper. Do not catch-and-continue at the
    framework level — a sustained schema break should stop ingestion,
    not silently drop data.
    """


@dataclass(frozen=True)
class _Fixture:
    """Internal record: one fixture we discovered via the categories tree."""

    event_id: str  # e.g. "f-1BTUpOr2SEi33O_h-WHKyg"
    competition_id: str  # e.g. "5292" (Copa Argentina)
    slug: str  # e.g. "futbol/argentina/copa-argentina/gimnasia-jujuy-belgrano"


class BetssonScraper(BaseScraper):
    """One provincial Betsson sportsbook (PBA, CABA, or CBA)."""

    poll_interval_sec = 5.0
    backoff_seconds_on_error = 30.0

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        subdomain: str = DEFAULT_SUBDOMAIN,
        fixture_cache_ttl_sec: float = DEFAULT_FIXTURE_TTL_SEC,
        soccer_slug_prefixes: tuple[str, ...] = DEFAULT_SOCCER_SLUG_PREFIXES,
        guard: RateLimitGuard | None = None,
        max_concurrent_events: int = DEFAULT_MAX_CONCURRENT_EVENTS,
    ) -> None:
        if subdomain not in VALID_SUBDOMAINS:
            raise ValueError(
                f"unknown Betsson subdomain {subdomain!r}; "
                f"expected one of {sorted(VALID_SUBDOMAINS)} "
                f"(CABA/CBA need recon to discover their jurisdiction header value)"
            )
        self.subdomain = subdomain
        self.platform_name = f"betsson-{subdomain}"
        self.base_url = f"https://{subdomain}.betsson.bet.ar"
        self._client = http_client
        self._fixture_cache_ttl_sec = fixture_cache_ttl_sec
        self._soccer_slug_prefixes = soccer_slug_prefixes
        self._max_concurrent_events = max(1, max_concurrent_events)
        self._fixtures_cache: list[_Fixture] = []
        self._fixtures_cached_at: float = 0.0
        self._guard = guard or RateLimitGuard(platform=self.platform_name)
        self._log = log.bind(platform=self.platform_name)
        # Three headers are empirically REQUIRED (derived by deletion-
        # test against live API on 2026-05-25; see RECON_LOG.md):
        #   brandid       — HTTP 400 `E_VALIDATION_INVALIDHEADER` if missing
        #   marketcode    — HTTP 400 `E_VALIDATION_INVALIDHEADER` if missing
        #   x-sb-type     — HTTP 500 `E_UNHANDLED` if missing (the request
        #                   handler dispatches on this; "b2b" is OBG's
        #                   business-to-business integration context)
        # `x-sb-jurisdiction` isn't required for a 200 but kept so the
        # response is actually scoped to this province's offering rather
        # than some default. Every other `x-sb-*` / `x-obg-*` header we
        # saw the browser send is NOT required and is deliberately
        # omitted — fewer headers, fewer surfaces for a future OBG
        # infrastructure change to break us.
        self._platform_headers: dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            # Required by the odds-endpoint WAF — a non-browser UA gets a
            # 403 HTML block page (see _BROWSER_USER_AGENT above).
            "User-Agent": _BROWSER_USER_AGENT,
            "brandid": PLATFORM_BRAND_ID,
            "marketcode": MARKET_CODE,
            "x-sb-type": "b2b",
            "x-sb-jurisdiction": SUBDOMAIN_JURISDICTION[subdomain],
            "Referer": f"{self.base_url}/apuestas-deportivas",
        }

    async def list_fixture_refs(self) -> list[tuple[str, str]]:
        """`(event_id, slug)` for current soccer fixtures — one cheap call, NO
        per-event odds. Lets a caller pre-select which events to fetch odds for
        (overlap-only fetching: skip events the other book doesn't cover)."""
        return [(fx.event_id, fx.slug) for fx in await self._discover_soccer_fixtures()]

    async def fetch_event_quotes(self, event_id: str) -> list[RawOddsSnapshot]:
        """Surgical per-event refetch — bypasses discovery + polling.

        Used by the pre-execution Tier-2 verifier to get the freshest
        available odds for a known event. Returns a list of snapshots
        for that single event's markets in v1 scope (MW3W + BTTS +
        MTG2W). Raises `BetssonContractError` on transport / schema
        failure; the caller decides whether to fall back.
        """
        # Build a minimal `_Fixture` — only `event_id` is used by
        # `_fetch_event_odds`. `slug` would normally seed the
        # `raw_event_name`; for verification we don't need it
        # (verifier matches on `platform_outcome_id`, not name).
        fx = _Fixture(event_id=event_id, competition_id="", slug="")
        return [snap async for snap in self._fetch_event_odds(fx)]

    async def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]:
        fixtures = await self._discover_soccer_fixtures()
        self._log.debug("scraper.fixtures", n=len(fixtures))

        # Run the per-event accordion calls with bounded concurrency: ~Nx faster
        # wall time than the old sequential loop. A single bad fixture (event
        # finished mid-poll, 404, or a tripped circuit) is logged and skipped, not
        # fatal — a platform-wide schema break recurs on every fixture in the logs.
        semaphore = asyncio.Semaphore(self._max_concurrent_events)

        async def _collect(fx: _Fixture) -> list[RawOddsSnapshot]:
            async with semaphore:
                try:
                    return [snap async for snap in self._fetch_event_odds(fx)]
                except BetssonContractError as exc:
                    self._log.warning(
                        "scraper.fixture_skipped", event_id=fx.event_id, error=str(exc)
                    )
                    return []

        for snaps in await asyncio.gather(*(_collect(fx) for fx in fixtures)):
            for snapshot in snaps:
                yield snapshot

    # ---- internals ----

    async def _discover_soccer_fixtures(self) -> list[_Fixture]:
        """Return soccer fixtures across the configured slug prefixes.
        Cached for `fixture_cache_ttl_sec`."""
        now = time.time()
        if self._fixtures_cache and (now - self._fixtures_cached_at) < self._fixture_cache_ttl_sec:
            return self._fixtures_cache

        url = f"{self.base_url}/api/sb/v1/widgets/categories/v2"
        data = await self._get_json(url)
        try:
            index = data["data"]["items"]["indexBySlug"]
        except (KeyError, TypeError) as exc:
            raise BetssonContractError(
                f"categories/v2 missing 'data.items.indexBySlug': {exc}"
            ) from exc
        if not isinstance(index, dict):
            raise BetssonContractError(
                f"categories/v2 'indexBySlug' has unexpected type {type(index).__name__}"
            )

        fixtures: list[_Fixture] = []
        for slug, id_chain in index.items():
            if not isinstance(slug, str):
                continue
            if not any(slug.startswith(p) for p in self._soccer_slug_prefixes):
                continue
            if slug.count("/") != SLUG_MATCH_LEVEL_SEGMENTS - 1:
                continue
            if not (isinstance(id_chain, list) and len(id_chain) >= SLUG_MATCH_LEVEL_SEGMENTS):
                continue
            event_id = id_chain[SLUG_MATCH_LEVEL_SEGMENTS - 1]
            competition_id = str(id_chain[SLUG_MATCH_LEVEL_SEGMENTS - 2])
            if not (isinstance(event_id, str) and event_id.startswith("f-")):
                continue
            fixtures.append(_Fixture(event_id=event_id, competition_id=competition_id, slug=slug))

        self._fixtures_cache = fixtures
        self._fixtures_cached_at = now
        return fixtures

    async def _fetch_event_odds(self, fx: _Fixture) -> AsyncIterator[RawOddsSnapshot]:
        """One accordion call → snapshots for MW3W + BTTS + MTG2W."""
        url = f"{self.base_url}/api/sb/v1/widgets/accordion/v1"
        params = {"eventId": fx.event_id, "marketTemplateIds": ",".join(TARGET_MARKETS)}
        data = await self._get_json(url, params=params)
        try:
            accordions = data["data"]["accordions"]
        except (KeyError, TypeError) as exc:
            raise BetssonContractError(
                f"accordion/v1 missing 'data.accordions' for {fx.event_id}: {exc}"
            ) from exc
        if not isinstance(accordions, dict):
            raise BetssonContractError(
                f"accordion/v1 'accordions' has unexpected type {type(accordions).__name__}"
            )

        observed_at = time.time()
        raw_event_name = self._event_name_from_slug(fx.slug)

        for market_code, group in accordions.items():
            if not isinstance(group, dict):
                continue
            markets = group.get("markets", [])
            selections = group.get("selections", [])
            if not isinstance(markets, list) or not isinstance(selections, list):
                continue
            # MTG2W returns multiple markets (one per O/U line); each
            # selection's `marketId` ties it back to its line.
            sel_by_market: dict[str, list[dict[str, Any]]] = {}
            for sel in selections:
                if isinstance(sel, dict):
                    sel_by_market.setdefault(str(sel.get("marketId", "")), []).append(sel)

            for mkt in markets:
                if not isinstance(mkt, dict):
                    continue
                if mkt.get("status") != "Open":
                    continue
                market_id = str(mkt.get("id", ""))
                if not market_id:
                    continue
                line_value = str(mkt.get("lineValue", "") or "")
                friendly = mkt.get("marketFriendlyName") or mkt.get("label") or market_code
                # Include the O/U line in the human-readable market name so
                # the semantic layer doesn't have to look it up separately.
                raw_market_name = f"{friendly} {line_value}".strip() if line_value else friendly

                for sel in sel_by_market.get(market_id, []):
                    if sel.get("status") != "Open":
                        continue
                    odds = sel.get("odds")
                    if not isinstance(odds, int | float) or odds <= 1.0:
                        continue
                    raw_outcome_name = sel.get("label") or sel.get("participantLabel") or ""
                    yield RawOddsSnapshot(
                        platform=self.platform_name,
                        platform_event_id=fx.event_id,
                        platform_market_id=market_id,
                        platform_outcome_id=str(sel.get("id", "")),
                        raw_event_name=raw_event_name,
                        raw_market_name=str(raw_market_name),
                        raw_outcome_name=str(raw_outcome_name),
                        decimal_odds=float(odds),
                        max_stake=None,  # not in public response; bet-slip only
                        timestamp=observed_at,
                    )

    @staticmethod
    def _event_name_from_slug(slug: str) -> str:
        """Best-effort human-readable event name from a fixture URL slug.

        The slug's final segment is `<home>-<away>` with hyphens both
        between teams and within team names; we can't reliably split it
        here. The semantic layer matches on this string against the
        canonical match record, so a hint is enough.
        """
        parts = slug.split("/")
        if len(parts) < SLUG_MATCH_LEVEL_SEGMENTS:
            return slug
        return parts[SLUG_MATCH_LEVEL_SEGMENTS - 1].replace("-", " ")

    async def _get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        """GET <url> as JSON. Wraps network/HTTP/parse errors uniformly."""
        try:
            resp = await self._guard.get(
                lambda: self._client.get(
                    url,
                    params=params,
                    headers=self._platform_headers,
                    timeout=DEFAULT_HTTP_TIMEOUT,
                )
            )
        except CircuitOpenError as exc:
            raise BetssonContractError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise BetssonContractError(f"GET {url} failed: {exc!s}") from exc
        if resp.status_code >= 400:
            raise BetssonContractError(f"GET {url} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise BetssonContractError(f"GET {url} returned non-JSON body: {exc!s}") from exc
