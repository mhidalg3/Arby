"""Unit tests for the BetWarrior PBA scraper.

Uses `httpx.MockTransport` so the parser is exercised against
synthetic Kambi-shaped JSON without hitting the real network. The
fixtures here are hand-trimmed mirrors of the live response captured
during recon (see `scripts/recon/RECON_LOG.md` session
20260526-173113) — small enough to read in one screen, real enough
to catch parser drift.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from src.ingestion.scrapers.betwarrior import (
    BASE_URL,
    BRAND_ID,
    BetWarriorPbaDepthScraper,
    BetWarriorPbaScraper,
)

# ---- Synthetic JSON responses ----

# Trimmed Libertadores-style list view: two events, each with one
# primary "Match" (1X2) betoffer. Event 1027027525 is open; event
# 1027027526 is in "STARTED" state but still has open outcomes.
_BODY_LIBERTADORES: bytes = json.dumps(
    {
        "events": [
            {
                "event": {
                    "id": 1027027525,
                    "name": "LDU Quito - Always Ready",
                    "homeName": "LDU Quito",
                    "awayName": "Always Ready",
                    "start": "2026-05-26T22:00:00Z",
                    "state": "NOT_STARTED",
                    "group": "Copa Libertadores",
                    "sport": "FOOTBALL",
                    "path": [
                        {"termKey": "football", "name": "Fútbol"},
                        {"termKey": "copa_libertadores", "name": "Copa Libertadores"},
                    ],
                },
                "betOffers": [
                    {
                        "id": 2649147284,
                        "criterion": {"label": "Resultado Final"},
                        "betOfferType": {"englishName": "Match"},
                        "outcomes": [
                            {"id": 100001, "label": "1", "odds": 1290, "status": "OPEN"},
                            {"id": 100002, "label": "X", "odds": 5600, "status": "OPEN"},
                            {"id": 100003, "label": "2", "odds": 9000, "status": "OPEN"},
                        ],
                    }
                ],
            },
            {
                "event": {
                    "id": 1027027526,
                    "name": "Lanús - Mirassol-SP",
                    "homeName": "Lanús",
                    "awayName": "Mirassol-SP",
                    "state": "STARTED",
                    "sport": "FOOTBALL",
                },
                "betOffers": [
                    {
                        "id": 2649147285,
                        "criterion": {"label": "Resultado Final"},
                        "betOfferType": {"englishName": "Match"},
                        "outcomes": [
                            {"id": 100011, "label": "1", "odds": 2100, "status": "OPEN"},
                            {"id": 100012, "label": "X", "odds": 3400, "status": "OPEN"},
                            {"id": 100013, "label": "2", "odds": 3000, "status": "SUSPENDED"},
                        ],
                    }
                ],
            },
        ]
    }
).encode("utf-8")

# Feed with a non-Match betOfferType (Over/Under) attached — this can
# appear if Kambi widens the list-view payload in the future. The
# scraper must skip it defensively.
_BODY_WITH_NON_MATCH_OFFER: bytes = json.dumps(
    {
        "events": [
            {
                "event": {
                    "id": 999000001,
                    "name": "Test Home - Test Away",
                    "homeName": "Test Home",
                    "awayName": "Test Away",
                    "state": "NOT_STARTED",
                    "sport": "FOOTBALL",
                },
                "betOffers": [
                    {
                        "id": 1,
                        "criterion": {"label": "Resultado Final"},
                        "betOfferType": {"englishName": "Match"},
                        "outcomes": [
                            {"id": 1, "label": "1", "odds": 2000, "status": "OPEN"},
                            {"id": 2, "label": "X", "odds": 3000, "status": "OPEN"},
                            {"id": 3, "label": "2", "odds": 4000, "status": "OPEN"},
                        ],
                    },
                    {
                        "id": 2,
                        "criterion": {"label": "Total de goles"},
                        "betOfferType": {"englishName": "Over/Under"},
                        "outcomes": [
                            {
                                "id": 11,
                                "label": "Más",
                                "odds": 1770,
                                "status": "OPEN",
                                "line": 2500,
                            },
                            {
                                "id": 12,
                                "label": "Menos",
                                "odds": 2050,
                                "status": "OPEN",
                                "line": 2500,
                            },
                        ],
                    },
                ],
            }
        ]
    }
).encode("utf-8")

# Feed with sub-unity odds — outcome where odds=1000 (decimal 1.00).
# Sub-unity (≤ 1.0) odds are either suspended or nonsense; the
# scraper drops them silently.
_BODY_SUB_UNITY: bytes = json.dumps(
    {
        "events": [
            {
                "event": {
                    "id": 8888,
                    "name": "Sub Unity Home - Sub Unity Away",
                    "state": "NOT_STARTED",
                },
                "betOffers": [
                    {
                        "id": 7,
                        "criterion": {"label": "Resultado Final"},
                        "betOfferType": {"englishName": "Match"},
                        "outcomes": [
                            {"id": 71, "label": "1", "odds": 1000, "status": "OPEN"},
                            {"id": 72, "label": "X", "odds": 1500, "status": "OPEN"},
                            {"id": 73, "label": "2", "odds": 50000, "status": "OPEN"},
                        ],
                    }
                ],
            }
        ]
    }
).encode("utf-8")

# Feed where the event has no name and only homeName/awayName.
# Tests the fallback in _event_name().
_BODY_NAME_FALLBACK: bytes = json.dumps(
    {
        "events": [
            {
                "event": {
                    "id": 55555,
                    "homeName": "Argentina",
                    "awayName": "Brasil",
                    "state": "NOT_STARTED",
                },
                "betOffers": [
                    {
                        "id": 99,
                        "criterion": {"label": "Resultado Final"},
                        "betOfferType": {"englishName": "Match"},
                        "outcomes": [
                            {"id": 991, "label": "1", "odds": 2400, "status": "OPEN"},
                            {"id": 992, "label": "X", "odds": 3200, "status": "OPEN"},
                            {"id": 993, "label": "2", "odds": 2800, "status": "OPEN"},
                        ],
                    }
                ],
            }
        ]
    }
).encode("utf-8")

# Empty list-view (competition has no current offers).
_BODY_EMPTY: bytes = json.dumps({"events": []}).encode("utf-8")


Handler = Callable[[httpx.Request], httpx.Response]


def _make_client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _slug_routes_handler(
    routes: dict[str, tuple[int, bytes | None]],
) -> Handler:
    """Build a handler that serves JSON for the listed competition slugs.

    `routes[slug] = (status_code, body_or_None)`. Unmapped slugs return
    404. Path expected:
    `/offering/v2018/<brand>/listView/football/<slug>/all/all/matches.json`.
    """

    def _handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        prefix = f"/offering/v2018/{BRAND_ID}/listView/football/"
        suffix = "/all/all/matches.json"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return httpx.Response(404)
        slug = path[len(prefix) : -len(suffix)]
        if slug not in routes:
            return httpx.Response(404)
        status, body = routes[slug]
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})

    return _handler


# ---- Construction ----


class TestConstruction:
    async def test_default_platform_name_and_competitions(self) -> None:
        client = _make_client(_slug_routes_handler({}))
        s = BetWarriorPbaScraper(http_client=client)
        assert s.platform_name == "betwarrior-pba"
        assert "argentina" in s._competitions
        assert "copa_libertadores" in s._competitions
        await client.aclose()

    async def test_custom_competitions_override(self) -> None:
        client = _make_client(_slug_routes_handler({}))
        s = BetWarriorPbaScraper(http_client=client, competitions={"world_cup": "World Cup only"})
        assert s._competitions == {"world_cup": "World Cup only"}
        await client.aclose()


# ---- HTTP / parse errors → BetWarriorContractError caught per-competition ----


class TestContractErrors:
    async def test_4xx_5xx_skips_competition_silently(self) -> None:
        """One bad competition shouldn't kill the whole cycle. The
        per-competition try/except in fetch_live_soccer catches and
        logs; downstream sees an empty contribution from the bad
        competition and a full one from the good competition."""

        def handler(req: httpx.Request) -> httpx.Response:
            if "copa_libertadores" in req.url.path:
                return httpx.Response(
                    200, content=_BODY_LIBERTADORES, headers={"content-type": "application/json"}
                )
            return httpx.Response(503)

        client = _make_client(handler)
        s = BetWarriorPbaScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        # Libertadores produces snapshots; everything else 503'd.
        assert snaps
        assert {snap.platform_event_id for snap in snaps} == {"1027027525", "1027027526"}
        await client.aclose()

    async def test_malformed_json_skips_competition(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"not json", headers={"content-type": "application/json"}
            )

        client = _make_client(handler)
        s = BetWarriorPbaScraper(http_client=client, competitions={"copa_libertadores": "test"})
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert snaps == []
        await client.aclose()

    async def test_missing_events_key_skips_competition(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=json.dumps({"unexpected": "shape"}).encode("utf-8"),
                headers={"content-type": "application/json"},
            )

        client = _make_client(handler)
        s = BetWarriorPbaScraper(http_client=client, competitions={"copa_libertadores": "test"})
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert snaps == []
        await client.aclose()

    async def test_empty_events_is_not_an_error(self) -> None:
        """A competition with `events: []` is the legitimate "no
        matches scheduled" case — quiet, not an error."""
        client = _make_client(_slug_routes_handler({"copa_libertadores": (200, _BODY_EMPTY)}))
        s = BetWarriorPbaScraper(http_client=client, competitions={"copa_libertadores": "test"})
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert snaps == []
        await client.aclose()


# ---- Odds extraction ----


class TestOddsExtraction:
    async def test_kambi_odds_scaled_by_1000(self) -> None:
        """Critical Kambi gotcha: odds 1290 ⇒ decimal 1.29."""
        client = _make_client(
            _slug_routes_handler({"copa_libertadores": (200, _BODY_LIBERTADORES)})
        )
        s = BetWarriorPbaScraper(http_client=client, competitions={"copa_libertadores": "test"})
        snaps = [snap async for snap in s.fetch_live_soccer()]
        match1 = [s for s in snaps if s.platform_event_id == "1027027525"]
        assert len(match1) == 3
        odds_by_label = {snap.raw_outcome_name: snap.decimal_odds for snap in match1}
        assert odds_by_label == {
            "1": pytest.approx(1.29),
            "X": pytest.approx(5.60),
            "2": pytest.approx(9.00),
        }
        await client.aclose()

    async def test_non_open_outcomes_dropped(self) -> None:
        """Event 1027027526's "2" outcome is SUSPENDED — must not emit."""
        client = _make_client(
            _slug_routes_handler({"copa_libertadores": (200, _BODY_LIBERTADORES)})
        )
        s = BetWarriorPbaScraper(http_client=client, competitions={"copa_libertadores": "test"})
        snaps = [snap async for snap in s.fetch_live_soccer()]
        match2 = [s for s in snaps if s.platform_event_id == "1027027526"]
        labels = {snap.raw_outcome_name for snap in match2}
        # SUSPENDED "2" outcome dropped; "1" and "X" remain.
        assert labels == {"1", "X"}
        await client.aclose()

    async def test_sub_unity_odds_dropped(self) -> None:
        """odds=1000 ⇒ decimal 1.0, which is sub-unity for our purposes
        (= 1.0 is "no profit on win") — drop, don't emit."""
        client = _make_client(_slug_routes_handler({"copa_libertadores": (200, _BODY_SUB_UNITY)}))
        s = BetWarriorPbaScraper(http_client=client, competitions={"copa_libertadores": "test"})
        snaps = [snap async for snap in s.fetch_live_soccer()]
        labels = {snap.raw_outcome_name for snap in snaps}
        # "1" (odds=1000 = 1.0) dropped; "X" (1.5) and "2" (50.0) kept.
        assert labels == {"X", "2"}
        await client.aclose()

    async def test_non_match_betoffers_skipped(self) -> None:
        """Defensive filter — list-view should only ever carry Match
        offers, but if Kambi widens the payload the scraper must keep
        emitting clean 1X2 until a deliberate widening here."""
        client = _make_client(
            _slug_routes_handler({"copa_libertadores": (200, _BODY_WITH_NON_MATCH_OFFER)})
        )
        s = BetWarriorPbaScraper(http_client=client, competitions={"copa_libertadores": "test"})
        snaps = [snap async for snap in s.fetch_live_soccer()]
        # Only the Match-type betoffer should produce snapshots.
        assert all(snap.raw_market_name == "Resultado Final" for snap in snaps)
        assert {snap.raw_outcome_name for snap in snaps} == {"1", "X", "2"}
        await client.aclose()

    async def test_event_name_fallback_to_home_away(self) -> None:
        client = _make_client(
            _slug_routes_handler({"copa_libertadores": (200, _BODY_NAME_FALLBACK)})
        )
        s = BetWarriorPbaScraper(http_client=client, competitions={"copa_libertadores": "test"})
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert snaps
        for snap in snaps:
            assert snap.raw_event_name == "Argentina - Brasil"
        await client.aclose()

    async def test_snapshot_platform_ids_and_metadata(self) -> None:
        client = _make_client(
            _slug_routes_handler({"copa_libertadores": (200, _BODY_LIBERTADORES)})
        )
        s = BetWarriorPbaScraper(http_client=client, competitions={"copa_libertadores": "test"})
        snap = await anext(s.fetch_live_soccer())
        assert snap.platform == "betwarrior-pba"
        assert snap.platform_event_id == "1027027525"
        # Kambi's offer.id and outcome.id (stringified)
        assert snap.platform_market_id == "2649147284"
        assert snap.platform_outcome_id in {"100001", "100002", "100003"}
        assert snap.raw_event_name == "LDU Quito - Always Ready"
        assert snap.raw_market_name == "Resultado Final"
        assert snap.max_stake is None
        assert snap.timestamp > 0
        await client.aclose()


# ---- Request shape ----


class TestRequestShape:
    async def test_one_get_per_target_competition(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.url.path)
            return httpx.Response(404)

        client = _make_client(handler)
        s = BetWarriorPbaScraper(http_client=client)
        async for _ in s.fetch_live_soccer():
            pass
        # One GET per competition in the default list
        expected = sorted(
            f"/offering/v2018/{BRAND_ID}/listView/football/{slug}/all/all/matches.json"
            for slug in s._competitions
        )
        assert sorted(seen) == expected
        await client.aclose()

    async def test_required_query_params_present(self) -> None:
        seen_queries: list[httpx.QueryParams] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen_queries.append(req.url.params)
            return httpx.Response(404)

        client = _make_client(handler)
        s = BetWarriorPbaScraper(http_client=client, competitions={"copa_libertadores": "test"})
        async for _ in s.fetch_live_soccer():
            pass
        assert len(seen_queries) == 1
        params = seen_queries[0]
        # `lang` is the hard requirement — Kambi 400s without it.
        assert params.get("lang") == "es_AR"
        assert params.get("market") == "AR"
        assert params.get("client_id") == "2"
        assert params.get("channel_id") == "1"
        await client.aclose()

    async def test_origin_and_referer_headers_sent(self) -> None:
        seen_headers: list[httpx.Headers] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen_headers.append(req.headers)
            return httpx.Response(404)

        client = _make_client(handler)
        s = BetWarriorPbaScraper(http_client=client, competitions={"copa_libertadores": "test"})
        async for _ in s.fetch_live_soccer():
            pass
        assert len(seen_headers) == 1
        headers = seen_headers[0]
        assert headers.get("origin") == "https://pba.betwarrior.bet.ar"
        assert headers.get("referer") == "https://pba.betwarrior.bet.ar/"
        await client.aclose()

    async def test_base_url_and_brand_unchanged(self) -> None:
        """Regression guard: BASE_URL and BRAND_ID are part of the
        recon-frozen contract. A future refactor pointing the scraper
        at a different host or brand would silently break production."""
        assert BASE_URL == "https://eu.offering-api.kambicdn.com"
        assert BRAND_ID == "bwargbap"


# =============================================================================
# Depth scraper tests
# =============================================================================

# Synthetic depth-endpoint response — mirrors the live shape captured
# 2026-05-26 from the LDU Quito - Always Ready event. Carries:
#   - 1 BTTS market (criterion "Ambos Equipos Marcarán", englishName "Yes/No")
#   - 2 OU markets at lines 2.5 and 3.5 (englishName "Over/Under")
#   - 1 push-line OU at line 3 (must be rejected)
#   - 1 unrelated market (Asian Handicap) that must be skipped
#   - 1 suspended outcome that must be skipped
_BODY_DEPTH_EVENT: bytes = json.dumps(
    {
        "betOffers": [
            {
                "id": 9001,
                "criterion": {"label": "Ambos Equipos Marcarán"},
                "betOfferType": {"englishName": "Yes/No"},
                "outcomes": [
                    {"id": 90011, "label": "Sí", "type": "OT_YES",
                     "odds": 2040, "status": "OPEN"},
                    {"id": 90012, "label": "No", "type": "OT_NO",
                     "odds": 1680, "status": "OPEN"},
                ],
            },
            {
                "id": 9002,
                "criterion": {"label": "Total de goles"},
                "betOfferType": {"englishName": "Over/Under"},
                "outcomes": [
                    {"id": 90021, "label": "Más de", "type": "OT_OVER",
                     "line": 2500, "odds": 1900, "status": "OPEN"},
                    {"id": 90022, "label": "Menos de", "type": "OT_UNDER",
                     "line": 2500, "odds": 1950, "status": "OPEN"},
                ],
            },
            {
                "id": 9003,
                "criterion": {"label": "Total de goles"},
                "betOfferType": {"englishName": "Over/Under"},
                "outcomes": [
                    {"id": 90031, "label": "Más de", "type": "OT_OVER",
                     "line": 3500, "odds": 3400, "status": "OPEN"},
                    {"id": 90032, "label": "Menos de", "type": "OT_UNDER",
                     "line": 3500, "odds": 1300, "status": "OPEN"},
                ],
            },
            {
                # Push line — must be rejected.
                "id": 9004,
                "criterion": {"label": "Total de goles"},
                "betOfferType": {"englishName": "Over/Under"},
                "outcomes": [
                    {"id": 90041, "label": "Más de", "type": "OT_OVER",
                     "line": 3000, "odds": 2100, "status": "OPEN"},
                    {"id": 90042, "label": "Menos de", "type": "OT_UNDER",
                     "line": 3000, "odds": 1850, "status": "OPEN"},
                ],
            },
            {
                # Suspended outcome on an otherwise-valid OU line.
                "id": 9005,
                "criterion": {"label": "Total de goles"},
                "betOfferType": {"englishName": "Over/Under"},
                "outcomes": [
                    {"id": 90051, "label": "Más de", "type": "OT_OVER",
                     "line": 4500, "odds": 5000, "status": "SUSPENDED"},
                    {"id": 90052, "label": "Menos de", "type": "OT_UNDER",
                     "line": 4500, "odds": 1150, "status": "OPEN"},
                ],
            },
            {
                # Out of v1 scope — must be silently dropped.
                "id": 9006,
                "criterion": {"label": "Hándicap Asiático"},
                "betOfferType": {"englishName": "Asian Handicap"},
                "outcomes": [
                    {"id": 90061, "label": "1", "line": -500,
                     "odds": 1750, "status": "OPEN"},
                    {"id": 90062, "label": "2", "line": -500,
                     "odds": 2050, "status": "OPEN"},
                ],
            },
            {
                # Half-time BTTS — same `Yes/No` type but different criterion
                # label. Must NOT match the depth scraper's BTTS filter.
                "id": 9007,
                "criterion": {"label": "Ambos Equipos Marcarán - 1.ª parte"},
                "betOfferType": {"englishName": "Yes/No"},
                "outcomes": [
                    {"id": 90071, "label": "Sí", "type": "OT_YES",
                     "odds": 5000, "status": "OPEN"},
                    {"id": 90072, "label": "No", "type": "OT_NO",
                     "odds": 1200, "status": "OPEN"},
                ],
            },
        ]
    }
).encode("utf-8")


def _depth_routes_handler(
    list_view_routes: dict[str, tuple[int, bytes | None]],
    event_routes: dict[str, tuple[int, bytes | None]],
) -> Handler:
    """Two-route handler covering both list-view (slug-based) and
    depth (event-id-based) URLs."""

    list_prefix = f"/offering/v2018/{BRAND_ID}/listView/football/"
    list_suffix = "/all/all/matches.json"
    event_prefix = f"/offering/v2018/{BRAND_ID}/betoffer/event/"
    event_suffix = ".json"

    def _handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.startswith(list_prefix) and path.endswith(list_suffix):
            slug = path[len(list_prefix) : -len(list_suffix)]
            if slug in list_view_routes:
                status, body = list_view_routes[slug]
                if body is None:
                    return httpx.Response(status)
                return httpx.Response(
                    status, content=body, headers={"content-type": "application/json"}
                )
            return httpx.Response(404)
        if path.startswith(event_prefix) and path.endswith(event_suffix):
            event_id = path[len(event_prefix) : -len(event_suffix)]
            if event_id in event_routes:
                status, body = event_routes[event_id]
                if body is None:
                    return httpx.Response(status)
                return httpx.Response(
                    status, content=body, headers={"content-type": "application/json"}
                )
            return httpx.Response(404)
        return httpx.Response(404)

    return _handler


class TestDepthConstruction:
    async def test_default_settings(self) -> None:
        client = _make_client(_depth_routes_handler({}, {}))
        s = BetWarriorPbaDepthScraper(http_client=client)
        assert s.platform_name == "betwarrior-pba"
        assert s.poll_interval_sec == 30.0
        assert "argentina" in s._competitions
        await client.aclose()


class TestDepthDiscoveryAndFetch:
    async def test_full_event_yields_btts_plus_two_ou_lines(self) -> None:
        """One event yields: 2 BTTS outcomes + 4 OU outcomes (2 lines × 2 sides).
        Push-line is rejected; SUSPENDED outcome is dropped; Asian Handicap
        and first-half BTTS are silently skipped."""
        client = _make_client(
            _depth_routes_handler(
                list_view_routes={"copa_libertadores": (200, _BODY_LIBERTADORES)},
                event_routes={"1027027525": (200, _BODY_DEPTH_EVENT)},
            )
        )
        s = BetWarriorPbaDepthScraper(
            http_client=client, competitions={"copa_libertadores": "test"}
        )
        snaps = [snap async for snap in s.fetch_live_soccer()]
        # 2 BTTS + 2 OU(2.5) + 2 OU(3.5) + 1 OU(4.5 — partial because
        # the OVER is suspended) = 7 snapshots
        assert len(snaps) == 7

        btts = [snap for snap in snaps if snap.raw_market_name == "Ambos Equipos Marcarán"]
        assert len(btts) == 2
        btts_labels = sorted(snap.raw_outcome_name for snap in btts)
        assert btts_labels == ["No", "Sí"]

        ou_25 = [snap for snap in snaps if snap.raw_market_name == "Total de goles 2.5"]
        assert len(ou_25) == 2
        ou_35 = [snap for snap in snaps if snap.raw_market_name == "Total de goles 3.5"]
        assert len(ou_35) == 2
        ou_45 = [snap for snap in snaps if snap.raw_market_name == "Total de goles 4.5"]
        # The OVER 4.5 was SUSPENDED; only the UNDER survives.
        assert len(ou_45) == 1
        assert ou_45[0].raw_outcome_name == "Menos de"

        await client.aclose()

    async def test_kambi_odds_scaled_by_1000(self) -> None:
        client = _make_client(
            _depth_routes_handler(
                list_view_routes={"copa_libertadores": (200, _BODY_LIBERTADORES)},
                event_routes={"1027027525": (200, _BODY_DEPTH_EVENT)},
            )
        )
        s = BetWarriorPbaDepthScraper(
            http_client=client, competitions={"copa_libertadores": "test"}
        )
        snaps = [snap async for snap in s.fetch_live_soccer()]
        # BTTS YES: odds=2040 → 2.04
        btts_yes = next(snap for snap in snaps if snap.raw_outcome_name == "Sí")
        assert btts_yes.decimal_odds == pytest.approx(2.04)
        # OU 2.5 OVER: odds=1900 → 1.9
        ou_over_25 = next(
            snap for snap in snaps
            if snap.raw_market_name == "Total de goles 2.5"
            and snap.raw_outcome_name == "Más de"
        )
        assert ou_over_25.decimal_odds == pytest.approx(1.9)
        await client.aclose()

    async def test_push_line_rejected(self) -> None:
        """OU at integer line 3 (line=3000) must NOT produce any snapshots."""
        client = _make_client(
            _depth_routes_handler(
                list_view_routes={"copa_libertadores": (200, _BODY_LIBERTADORES)},
                event_routes={"1027027525": (200, _BODY_DEPTH_EVENT)},
            )
        )
        s = BetWarriorPbaDepthScraper(
            http_client=client, competitions={"copa_libertadores": "test"}
        )
        snaps = [snap async for snap in s.fetch_live_soccer()]
        push_line_market = "Total de goles 3"
        assert all(snap.raw_market_name != push_line_market for snap in snaps)

    async def test_event_404_skips_silently(self) -> None:
        """If the per-event endpoint 404s for one event, the depth
        scraper logs and moves on. No exception bubbles up; other
        events still get processed."""
        client = _make_client(
            _depth_routes_handler(
                list_view_routes={"copa_libertadores": (200, _BODY_LIBERTADORES)},
                event_routes={},  # all events 404
            )
        )
        s = BetWarriorPbaDepthScraper(
            http_client=client, competitions={"copa_libertadores": "test"}
        )
        snaps = [snap async for snap in s.fetch_live_soccer()]
        # No event responses → no depth snapshots emitted, no crash.
        assert snaps == []
        await client.aclose()

    async def test_list_view_failure_skips_competition(self) -> None:
        client = _make_client(_depth_routes_handler({}, {}))
        s = BetWarriorPbaDepthScraper(
            http_client=client, competitions={"copa_libertadores": "test"}
        )
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert snaps == []
        await client.aclose()


class TestDepthRequestShape:
    async def test_event_endpoint_path_and_params(self) -> None:
        seen: list[tuple[str, httpx.QueryParams]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append((req.url.path, req.url.params))
            path = req.url.path
            if path.endswith("/all/matches.json"):
                return httpx.Response(
                    200, content=_BODY_LIBERTADORES,
                    headers={"content-type": "application/json"},
                )
            if "/betoffer/event/" in path:
                return httpx.Response(
                    200, content=_BODY_DEPTH_EVENT,
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(404)

        client = _make_client(handler)
        s = BetWarriorPbaDepthScraper(
            http_client=client, competitions={"copa_libertadores": "test"}
        )
        async for _ in s.fetch_live_soccer():
            pass

        # Saw at least one list-view call + at least one event call.
        list_paths = [p for p, _ in seen if "matches.json" in p]
        event_paths = [p for p, _ in seen if "/betoffer/event/" in p]
        assert list_paths
        assert event_paths
        # Event endpoint format: /offering/v2018/<brand>/betoffer/event/<id>.json
        assert event_paths[0].startswith(
            f"/offering/v2018/{BRAND_ID}/betoffer/event/"
        )
        assert event_paths[0].endswith(".json")
        # All calls include the required lang param.
        for _, params in seen:
            assert params.get("lang") == "es_AR"

        await client.aclose()
