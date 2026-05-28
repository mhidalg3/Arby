"""Unit tests for the Betsson scraper.

Uses `httpx.MockTransport` so the parser is exercised against synthetic
OBG-shaped JSON without hitting the real network. The fixtures here are
hand-trimmed mirrors of the live responses captured during the recon
session 20260525-210248 (see `scripts/recon/RECON_LOG.md`) — small
enough to read in a single screen, real enough to catch parser drift.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from src.ingestion.scrapers.betsson import (
    DEFAULT_FIXTURE_TTL_SEC,
    SUBDOMAIN_JURISDICTION,
    TARGET_MARKETS,
    BetssonContractError,
    BetssonScraper,
)

# ---- Synthetic responses ----

# Trimmed categories tree: one Argentine soccer fixture, plus one
# non-Argentine and one non-soccer entry that the filter must skip.
_CATEGORIES_OK: dict[str, Any] = {
    "data": {
        "items": {
            "indexBySlug": {
                "futbol": ["1"],
                "futbol/argentina": ["1", "117"],
                "futbol/argentina/copa-argentina": ["1", "117", "5292"],
                "futbol/argentina/copa-argentina/gimnasia-jujuy-belgrano": [
                    "1",
                    "117",
                    "5292",
                    "f-1BTUpOr2SEi33O_h-WHKyg",
                ],
                # non-Argentine — must be skipped
                "futbol/alemania/bundesliga/paderborn-wolfsburg": [
                    "1",
                    "14",
                    "15",
                    "f-OTHERMATCH",
                ],
                # too-shallow (league level, no fixture id) — must be skipped
                "futbol/argentina/argentina-primera-nacional": ["1", "117", "1234"],
            }
        }
    }
}

# Accordion response covering all three target market types for one fixture.
_ACCORDION_OK: dict[str, Any] = {
    "data": {
        "accordions": {
            "MW3W": {
                "markets": [
                    {
                        "eventId": "f-EVT",
                        "marketTemplateId": "MW3W",
                        "id": "m-f-EVT-MW3W",
                        "marketFriendlyName": "Ganador del partido",
                        "label": "Ganador del partido",
                        "lineValue": "",
                        "lineValueRaw": 0.0,
                        "status": "Open",
                    }
                ],
                "selections": [
                    {
                        "marketId": "m-f-EVT-MW3W",
                        "id": "s-m-f-EVT-MW3W-home",
                        "selectionTemplateId": "HOME",
                        "label": "Gimnasia Jujuy",
                        "participantLabel": "Gimnasia Jujuy",
                        "odds": 3.95,
                        "status": "Open",
                    },
                    {
                        "marketId": "m-f-EVT-MW3W",
                        "id": "s-m-f-EVT-MW3W-draw",
                        "selectionTemplateId": "DRAW",
                        "label": "Empate",
                        "odds": 3.05,
                        "status": "Open",
                    },
                    {
                        "marketId": "m-f-EVT-MW3W",
                        "id": "s-m-f-EVT-MW3W-away",
                        "selectionTemplateId": "AWAY",
                        "label": "Belgrano",
                        "odds": 2.02,
                        "status": "Open",
                    },
                ],
            },
            "BTTS": {
                "markets": [
                    {
                        "id": "m-f-EVT-BTTS",
                        "marketFriendlyName": "Ambos equipos anotan",
                        "lineValue": "",
                        "status": "Open",
                    }
                ],
                "selections": [
                    {
                        "marketId": "m-f-EVT-BTTS",
                        "id": "s-m-f-EVT-BTTS-yes",
                        "label": "Si",
                        "odds": 2.18,
                        "status": "Open",
                    },
                    {
                        "marketId": "m-f-EVT-BTTS",
                        "id": "s-m-f-EVT-BTTS-no",
                        "label": "No",
                        "odds": 1.62,
                        "status": "Open",
                    },
                ],
            },
            "MTG2W": {
                # Two markets in one group — one per O/U line.
                "markets": [
                    {
                        "id": "m-f-EVT-MTG2W-2.5",
                        "marketFriendlyName": "Total de goles",
                        "lineValue": "2.5",
                        "status": "Open",
                    },
                    {
                        "id": "m-f-EVT-MTG2W-3.5",
                        "marketFriendlyName": "Total de goles",
                        "lineValue": "3.5",
                        "status": "Open",
                    },
                ],
                "selections": [
                    {
                        "marketId": "m-f-EVT-MTG2W-2.5",
                        "id": "s-m-f-EVT-MTG2W-2.5-over",
                        "label": "Más de 2.5",
                        "odds": 1.90,
                        "status": "Open",
                    },
                    {
                        "marketId": "m-f-EVT-MTG2W-2.5",
                        "id": "s-m-f-EVT-MTG2W-2.5-under",
                        "label": "Menos de 2.5",
                        "odds": 1.95,
                        "status": "Open",
                    },
                    {
                        "marketId": "m-f-EVT-MTG2W-3.5",
                        "id": "s-m-f-EVT-MTG2W-3.5-over",
                        "label": "Más de 3.5",
                        "odds": 3.25,
                        "status": "Open",
                    },
                    {
                        "marketId": "m-f-EVT-MTG2W-3.5",
                        "id": "s-m-f-EVT-MTG2W-3.5-under",
                        "label": "Menos de 3.5",
                        "odds": 1.35,
                        "status": "Open",
                    },
                ],
            },
        }
    },
    "referenceId": "test-ref",
}


# ---- HTTP fixture: route URL paths to canned responses ----

Handler = Callable[[httpx.Request], httpx.Response]


def _make_client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _default_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/api/sb/v1/widgets/categories/v2"):
        return httpx.Response(200, json=_CATEGORIES_OK)
    if path.endswith("/api/sb/v1/widgets/accordion/v1"):
        return httpx.Response(200, json=_ACCORDION_OK)
    return httpx.Response(404)


# ---- Construction / validation ----


class TestConstruction:
    async def test_rejects_unknown_subdomain(self) -> None:
        client = _make_client(_default_handler)
        with pytest.raises(ValueError, match="unknown Betsson subdomain"):
            BetssonScraper(http_client=client, subdomain="xyz")
        await client.aclose()

    async def test_accepts_only_recon_confirmed_subdomains(self) -> None:
        """Only PBA has been recon'd. CABA/CBA need their own recon to
        discover the per-jurisdiction header value, so the constructor
        must reject them until that lands."""
        client = _make_client(_default_handler)
        # PBA confirmed
        s = BetssonScraper(http_client=client, subdomain="pba")
        assert s.platform_name == "betsson-pba"
        assert s.base_url == "https://pba.betsson.bet.ar"
        # CABA / CBA explicitly not yet supported
        for unsupported in ("caba", "cba"):
            with pytest.raises(ValueError, match="recon"):
                BetssonScraper(http_client=client, subdomain=unsupported)
        await client.aclose()

    async def test_default_fixture_ttl(self) -> None:
        client = _make_client(_default_handler)
        s = BetssonScraper(http_client=client)
        assert s._fixture_cache_ttl_sec == DEFAULT_FIXTURE_TTL_SEC
        await client.aclose()


# ---- Fixture discovery ----


class TestFixtureDiscovery:
    async def test_filters_to_argentine_soccer_matches_only(self) -> None:
        client = _make_client(_default_handler)
        s = BetssonScraper(http_client=client)
        fixtures = await s._discover_soccer_fixtures()
        assert len(fixtures) == 1
        assert fixtures[0].event_id == "f-1BTUpOr2SEi33O_h-WHKyg"
        assert fixtures[0].competition_id == "5292"
        assert "gimnasia-jujuy-belgrano" in fixtures[0].slug
        await client.aclose()

    async def test_caches_fixtures(self) -> None:
        """Categories call is expensive; second discovery within TTL is a no-op."""
        call_count = 0

        def counting_handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            if req.url.path.endswith("/categories/v2"):
                call_count += 1
            return _default_handler(req)

        client = _make_client(counting_handler)
        s = BetssonScraper(http_client=client)
        await s._discover_soccer_fixtures()
        await s._discover_soccer_fixtures()
        await s._discover_soccer_fixtures()
        assert call_count == 1
        await client.aclose()

    async def test_raises_contract_error_on_missing_index(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"items": {}}})

        client = _make_client(handler)
        s = BetssonScraper(http_client=client)
        with pytest.raises(BetssonContractError, match="indexBySlug"):
            await s._discover_soccer_fixtures()
        await client.aclose()

    async def test_raises_contract_error_on_http_error(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = _make_client(handler)
        s = BetssonScraper(http_client=client)
        with pytest.raises(BetssonContractError, match="HTTP 503"):
            await s._discover_soccer_fixtures()
        await client.aclose()


# ---- Odds extraction ----


class TestOddsExtraction:
    async def test_yields_all_open_selections_across_three_markets(self) -> None:
        client = _make_client(_default_handler)
        s = BetssonScraper(http_client=client)
        snaps = []
        async for snap in s.fetch_live_soccer():
            snaps.append(snap)
        # MW3W (3) + BTTS (2) + MTG2W 2.5 (2) + MTG2W 3.5 (2) = 9
        assert len(snaps) == 9
        await client.aclose()

    async def test_mw3w_carries_canonical_outcome_strings(self) -> None:
        client = _make_client(_default_handler)
        s = BetssonScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        mw3w = [s for s in snaps if "Ganador" in s.raw_market_name]
        assert len(mw3w) == 3
        labels = {s.raw_outcome_name for s in mw3w}
        assert labels == {"Gimnasia Jujuy", "Empate", "Belgrano"}
        odds_by_label = {s.raw_outcome_name: s.decimal_odds for s in mw3w}
        assert odds_by_label["Gimnasia Jujuy"] == pytest.approx(3.95)
        assert odds_by_label["Empate"] == pytest.approx(3.05)
        assert odds_by_label["Belgrano"] == pytest.approx(2.02)
        await client.aclose()

    async def test_btts_yields_yes_and_no(self) -> None:
        client = _make_client(_default_handler)
        s = BetssonScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        btts = [s for s in snaps if "Ambos" in s.raw_market_name]
        assert {snap.raw_outcome_name for snap in btts} == {"Si", "No"}
        await client.aclose()

    async def test_mtg2w_line_in_market_name(self) -> None:
        client = _make_client(_default_handler)
        s = BetssonScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        ou = [s for s in snaps if "Total de goles" in s.raw_market_name]
        assert len(ou) == 4
        # Both lines present, embedded in the market name.
        market_names = {s.raw_market_name for s in ou}
        assert market_names == {"Total de goles 2.5", "Total de goles 3.5"}
        await client.aclose()

    async def test_snapshot_carries_platform_ids(self) -> None:
        client = _make_client(_default_handler)
        s = BetssonScraper(http_client=client)
        snap = await anext(s.fetch_live_soccer())
        assert snap.platform == "betsson-pba"
        assert snap.platform_event_id.startswith("f-")
        assert snap.platform_market_id.startswith("m-")
        assert snap.platform_outcome_id.startswith("s-m-")
        assert snap.max_stake is None  # not in public response
        assert snap.timestamp > 0
        await client.aclose()

    async def test_skips_suspended_market(self) -> None:
        suspended = {
            "data": {
                "accordions": {
                    "MW3W": {
                        "markets": [
                            {
                                "id": "m-f-EVT-MW3W",
                                "marketFriendlyName": "Ganador del partido",
                                "lineValue": "",
                                "status": "Suspended",
                            }
                        ],
                        "selections": [
                            {
                                "marketId": "m-f-EVT-MW3W",
                                "id": "s-m-f-EVT-MW3W-home",
                                "label": "Home",
                                "odds": 2.0,
                                "status": "Open",
                            }
                        ],
                    }
                }
            }
        }

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/categories/v2"):
                return httpx.Response(200, json=_CATEGORIES_OK)
            return httpx.Response(200, json=suspended)

        client = _make_client(handler)
        s = BetssonScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert snaps == []
        await client.aclose()

    async def test_skips_suspended_selection(self) -> None:
        partial = {
            "data": {
                "accordions": {
                    "BTTS": {
                        "markets": [
                            {
                                "id": "m-f-EVT-BTTS",
                                "marketFriendlyName": "BTTS",
                                "lineValue": "",
                                "status": "Open",
                            }
                        ],
                        "selections": [
                            {
                                "marketId": "m-f-EVT-BTTS",
                                "id": "s-m-f-EVT-BTTS-yes",
                                "label": "Si",
                                "odds": 2.0,
                                "status": "Open",
                            },
                            {
                                "marketId": "m-f-EVT-BTTS",
                                "id": "s-m-f-EVT-BTTS-no",
                                "label": "No",
                                "odds": 1.5,
                                "status": "Suspended",
                            },
                        ],
                    }
                }
            }
        }

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/categories/v2"):
                return httpx.Response(200, json=_CATEGORIES_OK)
            return httpx.Response(200, json=partial)

        client = _make_client(handler)
        s = BetssonScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert len(snaps) == 1
        assert snaps[0].raw_outcome_name == "Si"
        await client.aclose()

    async def test_drops_odds_at_or_below_unity(self) -> None:
        """Decimal odds <= 1.0 are nonsensical; OddsQuote downstream would
        reject them, so don't emit them in the first place."""
        bad = {
            "data": {
                "accordions": {
                    "BTTS": {
                        "markets": [
                            {
                                "id": "m-f-EVT-BTTS",
                                "marketFriendlyName": "BTTS",
                                "lineValue": "",
                                "status": "Open",
                            }
                        ],
                        "selections": [
                            {
                                "marketId": "m-f-EVT-BTTS",
                                "id": "s-1",
                                "label": "Si",
                                "odds": 1.0,
                                "status": "Open",
                            },
                            {
                                "marketId": "m-f-EVT-BTTS",
                                "id": "s-2",
                                "label": "No",
                                "odds": 1.85,
                                "status": "Open",
                            },
                        ],
                    }
                }
            }
        }

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/categories/v2"):
                return httpx.Response(200, json=_CATEGORIES_OK)
            return httpx.Response(200, json=bad)

        client = _make_client(handler)
        s = BetssonScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert len(snaps) == 1
        assert snaps[0].raw_outcome_name == "No"
        await client.aclose()


# ---- Request shape ----


class TestRequestShape:
    async def test_required_obg_headers_sent_on_every_request(self) -> None:
        """OBG backend returns HTTP 400 without these headers. Locking
        the contract in a test so a future refactor that drops them
        fails immediately instead of in production against real
        Betsson."""
        captured: list[dict[str, str]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            # Snapshot only platform-identifying headers (skip noise).
            captured.append({k.lower(): v for k, v in req.headers.items()})
            return _default_handler(req)

        client = _make_client(handler)
        s = BetssonScraper(http_client=client, subdomain="pba")
        async for _ in s.fetch_live_soccer():
            pass
        assert captured, "no requests were made"
        for headers in captured:
            # Empirically required (server returns 400 or 500 without
            # them) — see RECON_LOG.md. Deletion test against live API
            # narrowed the required set to exactly these three:
            assert headers.get("brandid"), "brandid → 400 if missing"
            assert headers.get("marketcode") == "ag", "marketcode → 400 if missing"
            assert headers.get("x-sb-type") == "b2b", "x-sb-type → 500 if missing"
            # Semantically required (so the response is scoped to this
            # province, not some default offering).
            assert headers.get("x-sb-jurisdiction") == SUBDOMAIN_JURISDICTION["pba"]
            assert headers.get("referer", "").startswith("https://pba.betsson.bet.ar/")
        await client.aclose()

    async def test_accordion_call_includes_all_target_markets(self) -> None:
        captured_params: list[httpx.QueryParams] = []

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/accordion/v1"):
                captured_params.append(req.url.params)
            return _default_handler(req)

        client = _make_client(handler)
        s = BetssonScraper(http_client=client)
        async for _ in s.fetch_live_soccer():
            pass
        assert len(captured_params) == 1
        params = captured_params[0]
        assert params["eventId"] == "f-1BTUpOr2SEi33O_h-WHKyg"
        market_ids = params["marketTemplateIds"].split(",")
        assert set(market_ids) == set(TARGET_MARKETS)
        await client.aclose()
