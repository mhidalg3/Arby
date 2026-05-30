"""Bplay PBA SSE scraper — in-play / live odds via Server-Sent Events.

Companion to the existing `BplayPbaScraper` (HTTP XML feed). The XML
feed covers pre-match odds for tournament-style competitions (UCL,
Libertadores, Sudamericana, Conference League, Copa Mundial). This
SSE scraper covers **live in-play odds across all currently-live
matches** — including the Argentine domestic Reserves matches the
XML feed cannot reach.

Same `platform_name = "bplay-pba"` as the XML scraper. The
canonicalizer treats their snapshots uniformly; the
`(platform, platform_event_id)` cache key uses the Bplay matchId
(distinct from the XML feed's `<Match id="...">` value, but per-feed
unique by construction).

API contract (frozen from recon, see `scripts/recon/RECON_LOG.md`
session 2026-05-26 — bplay SSE protocol decoded):

    GET https://events-deportespba.bplay.bet.ar/live
        ?mode=v2
        &partner=1147           # Bplay's SportNCO partner ID
        &id=<A>|<B>|<C>|...     # pipe-separated matchIds (subscription set)
        &main=                  # empty for live-list subscriptions
        &lang=ag                # Argentine Spanish
        &odds_format=dec        # decimal odds
        Headers: Accept: text/event-stream, Origin/Referer matching the SPA
        No auth, no cookies.

The server pushes three event types (`event: <type>\\ndata: <json>`):

    match    — match metadata (teams, score, status, time)
    odds     — odds updates (the v1 target)
    status   — status changes (mirrors match.status; not consumed)

`odds` payload shape (abbreviated SportNCO keys):

    {"match_id": "<id>",
     "odds": [
       {"qt":   "<market-label, Spanish>",
        "qlid": <market-type-id>,
        "bets": [
          {"tch": {
            "c1": {"cid": "<outcome-code>", "ct": <decimal-odds>, "act": "<outcome-label>", ...},
            "c2": {...},
            ...
          }}
        ]}
     ]}

For v1 we consume only three markets:

    1X2     qt = "¿Quién ganará el partido?", qlid = 2133000
            outcome cids: SNC_ACTOR_HOME / SNC_ACTOR_DRAW / SNC_ACTOR_AWAY
    BTTS    qt = "Ambos equipos marcan",       qlid = 2133023
            outcome labels: "Sí" / "No"
    OU      qt = "Total de Goles",              qlid = 2133446
            outcome labels embed line: "Más de 2.5" / "Menos de 2.5"

Subscription discovery: GET /en-vivo HTML and grep `matchId:"<id>"`
patterns. SSE only pushes for IN-PLAY matches; pre-match scheduled
fixtures are NOT pushed.

Per-cycle lifecycle: every call to `fetch_live_soccer()`:
  1. Re-discover the current live-match set from /en-vivo.
  2. If non-empty, open the SSE stream with that subscription.
  3. Yield snapshots as `odds` events arrive, up to a max duration.
  4. Close cleanly and return so the base scraper's poll loop can
     restart with fresh discovery.

This is a streaming variant of the BaseScraper interface — the
generator can yield for tens of seconds before returning.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from functools import partial
from typing import Any, Final

import httpx
import structlog

from src.ingestion.rate_limit import CircuitOpenError, RateLimitGuard
from src.ingestion.scrapers.base import BaseScraper, RawOddsSnapshot
from src.ingestion.scrapers.bplay import BROWSER_USER_AGENT

log = structlog.get_logger(__name__)


# ---- Frozen contract ----

SSE_BASE_URL: Final[str] = "https://events-deportespba.bplay.bet.ar/live"
LIVE_PAGE_URL: Final[str] = "https://deportespba.bplay.bet.ar/en-vivo"

SSE_QUERY_PARAMS: Final[dict[str, str]] = {
    "mode": "v2",
    "partner": "1147",
    "main": "",
    "lang": "ag",
    "odds_format": "dec",
}

SPA_ORIGIN: Final[str] = "https://deportespba.bplay.bet.ar"

# v1 market filter: only emit snapshots for these `qt` values.
QT_H2H_3WAY: Final[str] = "¿Quién ganará el partido?"
QT_BTTS: Final[str] = "Ambos equipos marcan"
QT_OU_GOALS: Final[str] = "Total de Goles"
TARGET_QTS: Final[frozenset[str]] = frozenset({QT_H2H_3WAY, QT_BTTS, QT_OU_GOALS})

# Regex for extracting OU line from outcome's `act` field. Captures
# the half-line (or any decimal). Both "Más de 2.5" and "Menos de 2.5"
# match this pattern.
_OU_LINE_RE: Final[re.Pattern[str]] = re.compile(r"\b(\d+(?:\.\d+)?)$")

# Maximum plausible OU goals line for soccer. The 30s smoke captured
# one match (Sorocaba U21 vs Ad Centro Olímpico U21) where Bplay's
# `qt: "Total de Goles"` had lines at 54.5/55.5 — either a mislabeled
# stat-prop market or a feed error, either way nonsense for soccer.
# Bound to 12.0 (covers high-scoring matches with margin) to drop
# such noise without losing real coverage.
MAX_PLAUSIBLE_GOALS_LINE: Final[float] = 12.0

# Regex for extracting matchIds from the en-vivo SSR HTML.
_MATCH_ID_RE: Final[re.Pattern[str]] = re.compile(r'matchId:"(\d+)"')

# Per-cycle SSE streaming duration. Long enough to capture many
# odds updates from active matches, short enough that the discovery
# cycle re-runs frequently to pick up newly-live matches.
DEFAULT_STREAM_DURATION_SEC: Final[float] = 55.0

# When discovery finds NO live matches, wait this long before the next
# /en-vivo poll. The base poll_interval is short (good for streaming
# continuity), but with zero live matches that would re-hit /en-vivo
# every ~1s — exactly the baseline load that helped flag us on
# 2026-05-27. Idle re-discovery should be unhurried.
IDLE_REDISCOVERY_BACKOFF_SEC: Final[float] = 20.0

# HTTP timeouts. Discovery is fast (~1s); streaming has a long
# read timeout because the server keeps the connection open between
# event blocks.
DISCOVERY_HTTP_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(10.0, connect=5.0)
SSE_HTTP_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(60.0, connect=5.0, read=60.0)


class BplayContractError(RuntimeError):
    """Bplay's SSE feed returned a payload that doesn't match the
    recon-frozen contract. Distinct from BplayContractError in the
    XML scraper module to avoid coupling the two modules.

    Note: the symbol-name overlap (intentional — `from ...bplay
    import BplayContractError` should still work) is preserved by
    re-exporting from `bplay.py`; this class is private-ish."""


class BplayPbaSSEScraper(BaseScraper):
    """Live in-play odds scraper for Bplay PBA via SSE.

    Coverage: any match currently in-play at the time of polling.
    Markets: 1X2 + BTTS + OU goals.
    """

    platform_name = "bplay-pba"
    # Cycle = (discovery + streaming + brief wait). poll_interval_sec
    # is the BETWEEN-cycle wait — set short because the bulk of the
    # cycle time is consumed by the streaming itself.
    poll_interval_sec = 1.0
    backoff_seconds_on_error = 30.0

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        stream_duration_sec: float = DEFAULT_STREAM_DURATION_SEC,
        extra_competition_slugs: tuple[str, ...] = (),
        guard: RateLimitGuard | None = None,
        idle_rediscovery_backoff_sec: float = IDLE_REDISCOVERY_BACKOFF_SEC,
    ) -> None:
        """
        Args:
            http_client: shared httpx client.
            stream_duration_sec: how long to keep the SSE connection
                open per cycle before closing and restarting discovery.
            extra_competition_slugs: optional additional competition
                page slugs to fetch during discovery (in case a
                competition has a live match that the /en-vivo page
                doesn't surface — defensive, usually unused).
            guard: rate-limit circuit breaker. Pass the SAME guard
                used by the Bplay XML scraper so a block detected on
                either source protects both — they share a host.
            idle_rediscovery_backoff_sec: wait after a no-live-matches
                discovery before the next /en-vivo poll. Keeps the idle
                case from hammering discovery at poll_interval_sec.
        """
        self._client = http_client
        self._stream_duration_sec = stream_duration_sec
        self._extra_competition_slugs = tuple(extra_competition_slugs)
        self._guard = guard or RateLimitGuard(platform=self.platform_name)
        self._idle_backoff_sec = idle_rediscovery_backoff_sec
        self._log = log.bind(platform=self.platform_name, mode="sse")
        self._discovery_headers: dict[str, str] = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Referer": SPA_ORIGIN + "/",
        }
        self._sse_headers: dict[str, str] = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/event-stream",
            "Origin": SPA_ORIGIN,
            "Referer": SPA_ORIGIN + "/en-vivo",
        }

    async def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]:
        match_ids = await self._discover_live_match_ids()
        if not match_ids:
            # No live matches. Back off before the next /en-vivo poll so
            # the idle case doesn't hammer discovery at poll_interval_sec.
            self._log.debug("sse.no_live_matches")
            if self._idle_backoff_sec > 0:
                await asyncio.sleep(self._idle_backoff_sec)
            return
        self._log.debug("sse.discovered_matches", n=len(match_ids))
        async for snapshot in self._stream_odds(match_ids):
            yield snapshot

    # ---- discovery ----

    async def _discover_live_match_ids(self) -> list[str]:
        """Fetch /en-vivo (and optionally per-competition pages) and
        extract `matchId:"X"` patterns from the SSR HTML."""
        urls = [LIVE_PAGE_URL] + [
            f"{SPA_ORIGIN}/competicion/{slug}" for slug in self._extra_competition_slugs
        ]
        seen: set[str] = set()
        for url in urls:
            try:
                resp = await self._guard.get(
                    partial(
                        self._client.get,
                        url,
                        headers=self._discovery_headers,
                        timeout=DISCOVERY_HTTP_TIMEOUT,
                    )
                )
            except CircuitOpenError as exc:
                # Recent block/rate-limit — stop hitting /en-vivo. Fail
                # fast, no network. Returning what we have (likely empty)
                # makes the cycle a no-op until the cooldown elapses.
                self._log.warning("sse.discovery_circuit_open", error=str(exc))
                break
            except httpx.HTTPError as exc:
                self._log.warning("sse.discovery_failed", url=url, error=str(exc))
                continue
            if resp.status_code >= 400:
                self._log.warning(
                    "sse.discovery_http_error", url=url, status=resp.status_code
                )
                continue
            for m in _MATCH_ID_RE.finditer(resp.text):
                seen.add(m.group(1))
        return sorted(seen)

    # ---- streaming ----

    async def _stream_odds(
        self, match_ids: list[str]
    ) -> AsyncIterator[RawOddsSnapshot]:
        params = dict(SSE_QUERY_PARAMS)
        params["id"] = "|".join(match_ids)
        deadline = time.monotonic() + self._stream_duration_sec

        # match_id → "Home / Away" string built from `act1`/`act2` in
        # `match` events. Populated lazily as events arrive. The SSE
        # protocol sends `match` events before/alongside `odds` events,
        # so the map is usually populated by the time the first `odds`
        # event for that match_id is processed. If not, the snapshot
        # gets an empty raw_event_name and the canonicalizer will
        # drop it; the NEXT odds event after the match event lands
        # will succeed.
        event_names: dict[str, str] = {}

        try:
            async with self._client.stream(
                "GET",
                SSE_BASE_URL,
                params=params,
                headers=self._sse_headers,
                timeout=SSE_HTTP_TIMEOUT,
            ) as response:
                if response.status_code >= 400:
                    self._log.warning(
                        "sse.stream_http_error", status=response.status_code
                    )
                    return
                async for event in _parse_sse_events(response):
                    if time.monotonic() >= deadline:
                        break
                    try:
                        payload = json.loads(event.data)
                    except json.JSONDecodeError as exc:
                        self._log.warning(
                            "sse.bad_json", event_type=event.event_type, error=str(exc)
                        )
                        continue
                    if event.event_type == "match":
                        # Update the name map; no snapshots emitted here.
                        match_id = str(payload.get("match_id") or "")
                        home = payload.get("act1")
                        away = payload.get("act2")
                        if match_id and isinstance(home, str) and isinstance(away, str):
                            event_names[match_id] = f"{home} vs {away}"
                        continue
                    if event.event_type != "odds":
                        continue
                    match_id = str(payload.get("match_id") or "")
                    raw_event_name = event_names.get(match_id, "")
                    for snapshot in _snapshots_from_odds_payload(
                        payload,
                        platform_name=self.platform_name,
                        raw_event_name=raw_event_name,
                    ):
                        yield snapshot
        except httpx.HTTPError as exc:
            self._log.warning("sse.stream_disconnected", error=str(exc))


# ---- SSE protocol parsing (pure functions, no I/O) ----


class _SSEEvent:
    """One parsed Server-Sent Event."""

    __slots__ = ("event_type", "data")

    def __init__(self, event_type: str, data: str) -> None:
        self.event_type = event_type
        self.data = data


async def _parse_sse_events(
    response: httpx.Response,
) -> AsyncIterator[_SSEEvent]:
    """Parse a streamed SSE response line by line into events.

    SSE protocol: lines starting with `:` are comments (skip),
    `event:` sets the event type for the next data, `data:` sets the
    data, and a blank line dispatches the event. Multiple `data:`
    lines for one event are joined with newlines per spec, though
    Bplay's payload is always one-line JSON.
    """
    current_type = "message"  # SSE default if no `event:` is sent
    current_data: list[str] = []

    async for line in response.aiter_lines():
        if not line:
            # Blank line → dispatch
            if current_data:
                data = "\n".join(current_data)
                yield _SSEEvent(event_type=current_type, data=data)
                current_data = []
                current_type = "message"
            continue
        if line.startswith(":"):
            # Comment
            continue
        if line.startswith("event:"):
            current_type = line[6:].strip()
        elif line.startswith("data:"):
            current_data.append(line[5:].lstrip())
        # Other fields (id:, retry:) ignored for our use case.

    # Tail: dispatch any final event without trailing blank line
    if current_data:
        yield _SSEEvent(event_type=current_type, data="\n".join(current_data))


def _snapshots_from_odds_payload(
    payload: dict[str, Any],
    platform_name: str,
    raw_event_name: str = "",
) -> list[RawOddsSnapshot]:
    """Extract v1-scope snapshots from a parsed `odds` event payload.

    Skips markets outside the v1 filter, malformed entries, and
    sub-unity odds. One snapshot per outcome.

    `raw_event_name` is supplied by the caller from a side-channel
    `event: match` payload (the `odds` payload itself doesn't carry
    team names). Empty string is acceptable — canonicalizer will
    drop until the next cycle when the match-event lookup populates.
    """
    snapshots: list[RawOddsSnapshot] = []
    match_id = str(payload.get("match_id") or "")
    if not match_id:
        return snapshots

    observed_at = time.time()
    markets = payload.get("odds")
    if not isinstance(markets, list):
        return snapshots

    for market in markets:
        if not isinstance(market, dict):
            continue
        qt = market.get("qt")
        if qt not in TARGET_QTS:
            continue
        qlid = market.get("qlid")
        if qlid is None:
            continue
        bets = market.get("bets")
        if not isinstance(bets, list) or not bets:
            continue

        # The OU line lives in the outcome `act` (e.g. "Más de 2.5").
        # Extract it once from the first outcome with a line.
        line_value: float | None = None
        if qt == QT_OU_GOALS:
            line_value = _extract_ou_line(bets)
            if line_value is None:
                continue
            raw_market_name = f"{qt} {line_value:g}"
        else:
            raw_market_name = qt

        market_id_str = f"m{match_id}-q{qlid}" + (
            f"-l{line_value:g}" if line_value is not None else ""
        )

        # Each bet has a `tch` dict of outcomes (c1, c2, c3, ...).
        for bet in bets:
            if not isinstance(bet, dict):
                continue
            tch = bet.get("tch")
            if not isinstance(tch, dict):
                continue
            for outcome in tch.values():
                if not isinstance(outcome, dict):
                    continue
                act = outcome.get("act")
                ct = outcome.get("ct")
                cid = outcome.get("cid")
                if not isinstance(act, str) or not isinstance(ct, int | float):
                    continue
                decimal_odds = float(ct)
                if decimal_odds <= 1.0:
                    continue
                outcome_id_str = f"{market_id_str}-{cid}" if cid is not None else (
                    f"{market_id_str}-{act[:24]}"
                )
                snapshots.append(
                    RawOddsSnapshot(
                        platform=platform_name,
                        platform_event_id=match_id,
                        platform_market_id=market_id_str,
                        platform_outcome_id=outcome_id_str,
                        raw_event_name=raw_event_name,
                        raw_market_name=raw_market_name,
                        raw_outcome_name=act,
                        decimal_odds=decimal_odds,
                        max_stake=None,
                        timestamp=observed_at,
                    )
                )
            break  # only the first `bets` entry — they're per-line groupings
                   # for combo markets; v1 markets have one bet each
    return snapshots


def _extract_ou_line(bets: list[Any]) -> float | None:
    """Pull the half-line number from an OU outcome label like
    `"Más de 2.5"` or `"Menos de 2.5"`. Returns None if no outcome
    in the bets has a parseable line."""
    for bet in bets:
        if not isinstance(bet, dict):
            continue
        tch = bet.get("tch")
        if not isinstance(tch, dict):
            continue
        for outcome in tch.values():
            if not isinstance(outcome, dict):
                continue
            act = outcome.get("act")
            if not isinstance(act, str):
                continue
            m = _OU_LINE_RE.search(act)
            if m:
                try:
                    line = float(m.group(1))
                except ValueError:
                    continue
                # v1 OU resolver rejects integer (push) lines; emit
                # nothing for those at scraper time so the downstream
                # never sees them.
                if abs((line * 2) - round(line * 2)) > 1e-9 or (
                    round(line * 2)
                ) % 2 == 0:
                    return None
                # Sanity-bound to plausible soccer-goals range.
                if line > MAX_PLAUSIBLE_GOALS_LINE:
                    return None
                return line
    return None
