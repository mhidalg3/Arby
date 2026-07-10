"""Integration test for the scraper↔canonicalizer↔detector seam.

This is the boundary where a real bug hid undetected for a day: the both-teams
fixture matcher needs `raw_event_name`, but `BetssonScraper.fetch_event_quotes`
emitted it empty (built `_Fixture(slug="")`), so every Betsson leg dropped at
fixture resolution → zero cross-platform markets. The per-component unit tests
missed it because they feed SYNTHETIC snapshots (with `raw_event_name` filled in)
or a STUB canonicalizer — the gap was precisely where the real scraper output
meets the real canonicalizer.

So this test wires the REAL pieces together — real `BetanoScraper` +
`BetssonScraper` (canned API payloads via `httpx.MockTransport`, no network) →
real `Canonicalizer`/`FixtureResolver` → `OverlapQuoteSource` → `detect_arbitrage`
— and asserts a cross-platform market forms. It would have failed on the
empty-slug regression. Lives in tests/unit (no infra) so it runs every suite.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.arbitrage.dutch_book import detect_arbitrage
from src.execution.quote_source import OverlapQuoteSource
from src.ingestion.scrapers.betano import BetanoScraper
from src.ingestion.scrapers.betsson import BetssonScraper
from src.semantic.canonicalizer import Canonicalizer
from src.semantic.fixture_resolver import FixtureResolver

# ---- Canned API payloads (both books on the SAME match: Gimnasia Jujuy vs Belgrano) ----

# Betano prematch envelope: data.topEventsV2.{events,markets,selections}. One FOOT
# 1X2 event; odds tuned so the best-per-cell partition mixes both books AND is a
# Dutch book (1/3.95 + 1/3.5 + 1/2.5 = 0.939 < 1 → ~6.5% margin).
_BETANO_PREMATCH: dict[str, Any] = {
    "data": {
        "topEventsV2": {
            "events": {
                "700001": {
                    "id": 700001,
                    "sportId": "FOOT",
                    "leagueId": 117,
                    "marketIdList": [800001],
                    "participants": [
                        {"name": "Gimnasia Jujuy", "teamId": 1},
                        {"name": "Belgrano", "teamId": 2},
                    ],
                    "url": "/cuotas-de-partido/gimnasia-jujuy-belgrano/700001/",
                    "startTime": 4102444800000,
                }
            },
            "markets": {
                "800001": {
                    "id": 800001,
                    "type": "MRES",
                    "typeId": 1,
                    "name": "Resultado del partido",
                    "selectionIdList": [900001, 900002, 900003],
                }
            },
            "selections": {
                "900001": {"id": 900001, "name": "1", "shortName": "1", "price": 3.0},
                "900002": {"id": 900002, "name": "X", "shortName": "X", "price": 3.5},
                "900003": {"id": 900003, "name": "2", "shortName": "2", "price": 2.5},
            },
        }
    }
}

# Betsson categories tree: the matching Argentine soccer fixture (slug carries both
# team names — what the fixture link parses).
_BETSSON_CATEGORIES: dict[str, Any] = {
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
                    "f-GJB",
                ],
            }
        }
    }
}

# Betsson accordion: MW3W (1X2) with team-name outcomes. Betsson is better on HOME,
# Betano on DRAW/AWAY → the assembled partition must contain BOTH books.
_BETSSON_ACCORDION: dict[str, Any] = {
    "data": {
        "accordions": {
            "MW3W": {
                "markets": [
                    {
                        "id": "m-GJB-MW3W",
                        "marketFriendlyName": "Ganador del partido",
                        "lineValue": "",
                        "status": "Open",
                    }
                ],
                "selections": [
                    {
                        "marketId": "m-GJB-MW3W",
                        "id": "s-home",
                        "label": "Gimnasia Jujuy",
                        "odds": 3.95,
                        "status": "Open",
                    },
                    {
                        "marketId": "m-GJB-MW3W",
                        "id": "s-draw",
                        "label": "Empate",
                        "odds": 3.05,
                        "status": "Open",
                    },
                    {
                        "marketId": "m-GJB-MW3W",
                        "id": "s-away",
                        "label": "Belgrano",
                        "odds": 2.02,
                        "status": "Open",
                    },
                ],
            }
        }
    }
}


def _handler(request: httpx.Request) -> httpx.Response:
    """Route by path — one transport serves both books (as the live loop shares one client)."""
    path = request.url.path
    if path.endswith("/api/home/top-events-v2/"):
        return httpx.Response(200, json=_BETANO_PREMATCH)
    if path.endswith("/api/sb/v1/widgets/categories/v2"):
        return httpx.Response(200, json=_BETSSON_CATEGORIES)
    if path.endswith("/api/sb/v1/widgets/accordion/v1"):
        return httpx.Response(200, json=_BETSSON_ACCORDION)
    return httpx.Response(404)


async def test_real_scrapers_and_canonicalizer_form_a_cross_platform_market() -> None:
    """End-to-end through the real scraper→canonicalizer seam: a cross-platform 1X2
    market must form with BOTH books' legs. The empty-slug regression would drop every
    Betsson leg here, leaving a single-platform (or no) market — caught by this test."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        src = OverlapQuoteSource(
            bulk_sources=[BetanoScraper(http_client=client, mode="prematch")],
            linkers=[BetssonScraper(http_client=client)],
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
        )
        out = await src.fetch()

    assert len(out) == 1, f"expected one cross-platform 1X2 market, got {list(out)}"
    ((market_id, quotes),) = out.items()
    assert market_id.endswith("|1x2")
    platforms = {q.platform for q in quotes}
    # THE regression guard: Betsson must link (empty slug would drop it).
    assert platforms == {"betano", "betsson-pba"}, platforms

    # And the seam feeds the detector a real 3-leg cross-platform arb.
    opp = detect_arbitrage(quotes, 300.0, 1.0)
    assert opp is not None and len(opp.legs) == 3
