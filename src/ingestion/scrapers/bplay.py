"""Bplay PBA scraper.

Bplay PBA runs on the SportNCO white-label platform. Pre-rendered XML
feeds at `/oddsfeeds/odds-competition<ID>.xml` cover a curated subset
of "marquee" tournament-style competitions (UCL, Copa Libertadores,
Copa Sudamericana, World Cup). Each XML file contains:

    Data
      └─ SportList
          └─ Sport
              └─ RegionList
                  └─ Region
                      └─ CompetitionList
                          └─ Competition
                              ├─ OutrightList   (tournament-winner bets)
                              │   └─ Offer ...
                              └─ MatchList
                                  └─ Match
                                      └─ OfferList
                                          └─ Offer
                                              └─ Outcome

One HTTP per competition per poll cycle yields all that competition's
matches + offers. More bandwidth-efficient than per-fixture polling.

Argentine domestic leagues (Copa Argentina, Primera Nacional,
Reservas) are NOT served by this XML pattern — they flow through a
WebSocket channel on `ws-deportespba.bplay.bet.ar` that this scraper
does not currently consume. See `scripts/recon/RECON_LOG.md` for the
full architecture map. Adding domestic coverage requires a separate
WebSocket-based scraper.

API contract (frozen from recon, see `RECON_LOG.md` session
20260526-164300 and 20260526-170123):

    GET https://deportespba.bplay.bet.ar/oddsfeeds/odds-competition<ID>.xml
        Anonymous, plain httpx (Cloudflare sits on the SPA shell only).
        → 200 + application/xml with the structure above, OR
        → 404 when the competition currently has no offers (treated
          as "skip this competition this cycle", not an error).

Market codes consumed (the raw `type_name` strings):
    "1-X-2"               — 1X2  (selections: home / "Empate" / away)
    "Más de / Menos de"   — Over/Under total goals (selections: "Más" / "Menos",
                            line in `number` attribute)
    "1-2"                 — Draw No Bet
    "Handicap 1-2"        — Asian Handicap
    "Ambos *"             — both-teams-to-score variants (label confirmed
                            via prefix match — exact wording unverified
                            in recon and varies across SportNCO operators)

When the platform changes the XML shape this module raises
`BplayContractError` and the framework backs off. No runtime
recovery — fix the scraper manually per `docs/architecture.md`.

Stake limits (`max_stake`, `min_stake`, `stake_increment`) are not
in the XML feed. The bet-slip flow (logged-in only) would carry them.
Snapshots emit `max_stake=None`; the risk layer falls back to
platform-wide policy defaults.
"""

from __future__ import annotations

import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Iterator
from typing import Final

import httpx
import structlog

from src.ingestion.rate_limit import CircuitOpenError, RateLimitGuard
from src.ingestion.scrapers.base import BaseScraper, RawOddsSnapshot

log = structlog.get_logger(__name__)


# ---- Frozen contract (see RECON_LOG.md sessions 20260526-164300 / 170123) ----

BASE_URL: Final[str] = "https://deportespba.bplay.bet.ar"
ODDS_FEED_PATH: Final[str] = "/oddsfeeds/odds-competition{competition_id}.xml"

# Marquee competitions confirmed to have pre-rendered XML feeds.
# Argentine domestic competitions (Copa Argentina 1493, Primera Nacional 43411,
# Reservas 43414) are NOT here — they 404 on the XML pattern; the recon notes
# explain why.
TARGET_COMPETITIONS: Final[dict[int, str]] = {
    6674: "UEFA Champions League",
    36146: "Copa Libertadores",
    36148: "Copa Sudamericana",
    63057: "Copa Mundial",
    42958: "UEFA Conference League",
}

# Market template names (Bplay's `type_name` attribute) we emit snapshots for.
MARKET_1X2: Final[str] = "1-X-2"
MARKET_OVER_UNDER: Final[str] = "Más de / Menos de"
MARKET_DRAW_NO_BET: Final[str] = "1-2"
MARKET_HANDICAP: Final[str] = "Handicap 1-2"
TARGET_MARKET_NAMES: Final[frozenset[str]] = frozenset(
    {MARKET_1X2, MARKET_OVER_UNDER, MARKET_DRAW_NO_BET, MARKET_HANDICAP}
)
# BTTS-shaped markets — exact label not yet confirmed in recon; match by
# prefix so we catch the common "Ambos equipos anotan" / "Ambos marcan"
# variants without committing to one spelling.
MARKET_BTTS_PREFIX: Final[str] = "Ambos"

# Conservative per-request timeout. The XML feeds are pre-rendered static
# files; sub-second response is normal. 15s leaves headroom for Cloudflare's
# occasional slowness without letting a true outage stall the polling loop.
DEFAULT_HTTP_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(15.0, connect=5.0)

# Bplay's WAF now serves an empty/stub response to the default
# python-httpx User-Agent (confirmed live 2026-05-30: XML feeds returned
# nothing, /en-vivo served a 269-byte shell). A browser UA gets the real
# payload — same silent WAF break fixed for Betsson on 2026-05-29.
BROWSER_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
_XML_HEADERS: Final[dict[str, str]] = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "application/xml,text/xml,*/*",
}


class BplayContractError(RuntimeError):
    """Bplay returned a payload that doesn't match the recon-frozen contract.

    Raised on HTTP failures other than 404 (which means "no offers for
    this competition right now" — handled as a skip), on non-XML
    response bodies, and on XML parse failures. Alertable: a sustained
    schema break needs the scraper updated.
    """


class BplayPbaScraper(BaseScraper):
    """Polls Bplay PBA's pre-rendered XML feeds for marquee soccer competitions.

    Each XML feed covers one competition and carries all of its current
    matches with full offers. One HTTP per competition per poll cycle
    gets the data — no separate fixture-discovery pass.
    """

    poll_interval_sec = 5.0
    backoff_seconds_on_error = 30.0
    platform_name = "bplay-pba"

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        competitions: dict[int, str] | None = None,
        guard: RateLimitGuard | None = None,
    ) -> None:
        self._client = http_client
        # Caller can override the target set (e.g., focus on World Cup
        # only, or add new competition IDs as recon discovers them).
        self._competitions = competitions if competitions is not None else dict(TARGET_COMPETITIONS)
        # Rate-limit circuit breaker. Once a 403/429 trips it, all
        # competition fetches in this poll fail fast (no network) until
        # the cooldown elapses — this is what would have prevented the
        # 2026-05-27 escalation from 429 to a hard 403 block.
        self._guard = guard or RateLimitGuard(platform=self.platform_name)
        self._log = log.bind(platform=self.platform_name)

    async def fetch_competition_quotes(
        self, competition_id: int
    ) -> list[RawOddsSnapshot]:
        """Surgical per-competition fetch — exposes the same XML feed
        used by `fetch_live_soccer` but returns just one
        competition's snapshots as a list.

        Used by the Tier-2 pre-execution verifier. Note the unit is
        a COMPETITION, not an event — the XML feed is
        competition-scoped, so the verifier must call this for
        every target competition until it finds the leg's event.
        Raises `BplayContractError` on transport / schema failure.
        """
        label = self._competitions.get(competition_id, "")
        return [
            snap async for snap in self._fetch_competition(competition_id, label)
        ]

    async def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]:
        for competition_id, competition_label in self._competitions.items():
            try:
                async for snapshot in self._fetch_competition(competition_id, competition_label):
                    yield snapshot
            except BplayContractError as exc:
                # A single bad competition shouldn't kill the cycle.
                # A platform-wide schema break recurs across all four
                # competitions and surfaces clearly in logs.
                self._log.warning(
                    "scraper.competition_skipped",
                    competition_id=competition_id,
                    competition_label=competition_label,
                    error=str(exc),
                )

    # ---- internals ----

    async def _fetch_competition(
        self, competition_id: int, competition_label: str
    ) -> AsyncIterator[RawOddsSnapshot]:
        url = f"{BASE_URL}{ODDS_FEED_PATH.format(competition_id=competition_id)}"
        try:
            resp = await self._guard.get(
                lambda: self._client.get(
                    url, headers=_XML_HEADERS, timeout=DEFAULT_HTTP_TIMEOUT
                )
            )
        except CircuitOpenError as exc:
            # Circuit is open from a recent block/rate-limit — fail fast,
            # no network. Surfaced as a contract error so the cycle skips
            # this competition cleanly.
            raise BplayContractError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise BplayContractError(f"GET {url} failed: {exc!s}") from exc

        if resp.status_code == 404:
            # The XML for this competition isn't being generated right
            # now — competition has no current offers / between rounds.
            # Common and expected; log at debug, skip silently.
            self._log.debug(
                "scraper.competition_empty",
                competition_id=competition_id,
                competition_label=competition_label,
            )
            return
        if resp.status_code >= 400:
            raise BplayContractError(f"GET {url} returned HTTP {resp.status_code}")

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise BplayContractError(f"could not parse XML from {url}: {exc!s}") from exc

        observed_at = time.time()
        for match in root.iter("Match"):
            for snapshot in self._snapshots_for_match(
                match, competition_id, competition_label, observed_at
            ):
                yield snapshot

    def _snapshots_for_match(
        self,
        match: ET.Element,
        competition_id: int,
        competition_label: str,
        observed_at: float,
    ) -> Iterator[RawOddsSnapshot]:
        match_id = match.get("id") or ""
        if not match_id:
            return

        raw_event_name = self._derive_event_name(match) or f"Match {match_id}"

        for offer in match.findall("OfferList/Offer"):
            type_name = offer.get("type_name") or ""
            type_id = offer.get("type_id") or ""
            line_value = offer.get("number") or ""

            if not _is_target_market(type_name):
                continue
            if not type_id:
                # Without type_id we can't construct a stable market_id; skip.
                continue

            platform_market_id = self._market_id(match_id, type_id, line_value)
            raw_market_name = f"{type_name} {line_value}".strip() if line_value else type_name

            for outcome in offer.findall("Outcome"):
                outcome_name = outcome.get("name") or ""
                odds_str = outcome.get("odds") or ""
                try:
                    odds = float(odds_str)
                except ValueError:
                    continue
                if odds <= 1.0:
                    # Sub-unity decimal odds either suspended or a
                    # nonsense value — downstream `OddsQuote` rejects
                    # them anyway, so don't emit.
                    continue

                yield RawOddsSnapshot(
                    platform=self.platform_name,
                    platform_event_id=match_id,
                    platform_market_id=platform_market_id,
                    platform_outcome_id=f"{platform_market_id}-{_slug(outcome_name)}",
                    raw_event_name=raw_event_name,
                    raw_market_name=raw_market_name,
                    raw_outcome_name=outcome_name,
                    decimal_odds=odds,
                    max_stake=None,  # not in public XML; bet-slip-only
                    timestamp=observed_at,
                )

    @staticmethod
    def _market_id(match_id: str, type_id: str, line_value: str) -> str:
        """Stable per-match per-market identifier.

        Match-id alone isn't enough (a match has many markets). type_id
        alone isn't enough across matches. Different O/U lines have
        distinct type_ids in the Bplay XML — so type_id IS enough to
        disambiguate market+line within a match, but we still append
        the line value for human readability when scanning logs."""
        if line_value:
            return f"m{match_id}-{type_id}-{line_value}"
        return f"m{match_id}-{type_id}"

    @staticmethod
    def _derive_event_name(match: ET.Element) -> str:
        """Best-effort home vs away string for the snapshot.

        Bplay's XML sometimes includes `<Team>` children under
        `<Match>` carrying canonical participant names. When absent,
        fall back to the first 1-X-2 offer's outcome names (positions
        0 and 2 — position 1 is "Empate"). When neither is available,
        return empty and the caller uses a synthesized name.
        """
        teams = [t.get("name") or "" for t in match.findall("Team")]
        teams = [t for t in teams if t]
        if len(teams) >= 2:
            return f"{teams[0]} vs {teams[1]}"

        for offer in match.findall("OfferList/Offer"):
            if offer.get("type_name") != MARKET_1X2:
                continue
            outcomes = offer.findall("Outcome")
            if len(outcomes) >= 3:
                home = outcomes[0].get("name") or ""
                away = outcomes[2].get("name") or ""
                if home and away:
                    return f"{home} vs {away}"
            break
        return ""


def _is_target_market(type_name: str) -> bool:
    if type_name in TARGET_MARKET_NAMES:
        return True
    # BTTS variants — exact label varies. Anchor on the Spanish
    # "Ambos" prefix; this catches "Ambos equipos marcan", "Ambos
    # equipos anotan", and similar without over-matching unrelated
    # markets (which don't start with "Ambos").
    return type_name.startswith(MARKET_BTTS_PREFIX)


_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    """Stable, accent-folded, lowercase slug for use in outcome IDs.

    "Más" → "mas". "Always Ready" → "always-ready". "1.5" → "1-5".
    Doesn't need to round-trip — only needs to be deterministic and
    unique within a market's outcome set.
    """
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = nfkd.encode("ascii", "ignore").decode().lower()
    slug = _NON_SLUG_CHARS.sub("-", ascii_text).strip("-")
    return slug or "x"
