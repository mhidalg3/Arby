"""Unit tests for the Betano scraper.

Uses `httpx.MockTransport` so the shared danae parser is exercised
against synthetic JSON without hitting the real network. Fixtures are
hand-trimmed mirrors of the live/pre-match responses captured in recon
session `20260529-150524` — small enough to read at a glance, real
enough to catch parser drift.
"""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest

from src.ingestion.scrapers.base import RawOddsSnapshot
from src.ingestion.scrapers.betano import (
    BetanoContractError,
    BetanoScraper,
    _parse_danae_soccer_1x2,
)

# ---- Synthetic payloads ----

# Pre-match envelope: data.topEventsV2.{events,markets,selections}. One
# real FOOT match (with both an MRES 1X2 and an MR12 promo on the same
# event — the promo must be ignored), plus entries the filter must skip:
# a non-FOOT event, an outright FOOT event (no participants), and an
# esports FOOT event.
_PREMATCH_OK: dict[str, Any] = {
    "data": {
        "topEventsV2": {
            "events": {
                "85324875": {
                    "id": 85324875,
                    "sportId": "FOOT",
                    "leagueId": 182748,
                    "marketIdList": [2849, 2784995846],  # MR12 first, MRES second
                    "participants": [
                        {"name": "Paris Saint-Germain", "teamId": 107635},
                        {"name": "Arsenal FC", "teamId": 106359},
                    ],
                    "url": "/cuotas-de-partido/paris-saint-germain-arsenal-fc/85324875/",
                    "startTime": 4102444800000,
                },
                "999": {  # non-FOOT — skip
                    "id": 999,
                    "sportId": "TENN",
                    "marketIdList": [7000],
                    "participants": [{"name": "A"}, {"name": "B"}],
                },
                "888": {  # outright FOOT (no head-to-head participants) — skip
                    "id": 888,
                    "sportId": "FOOT",
                    "marketIdList": [],
                    "participants": [],
                    "isOutrightEvent": True,
                },
                "777": {  # esports FOOT — skip
                    "id": 777,
                    "sportId": "FOOT",
                    "marketIdList": [2784995846],
                    "participants": [{"name": "Salzburg (Cira)"}, {"name": "Porto (Pika)"}],
                    "url": "/live/salzburg-cira-esports-porto-pika/777/",
                },
            },
            "markets": {
                "2784995846": {
                    "id": 2784995846,
                    "type": "MRES",
                    "typeId": 1,
                    "name": "Resultado del partido",
                    "selectionIdList": [9704445156, 9704445157, 9704445158],
                },
                "2849": {  # MR12 "SuperCuotas" promo — must be ignored
                    "id": 2849,
                    "type": "MR12",
                    "typeId": 2850,
                    "name": "Resultado del partido SuperCuotas",
                    "selectionIdList": [1, 2, 3],
                },
                "7000": {
                    "id": 7000,
                    "type": "HTOH",
                    "typeId": 160,
                    "name": "Ganador",
                    "selectionIdList": [111, 112],
                },
            },
            "selections": {
                "9704445156": {"id": 9704445156, "name": "1", "shortName": "1", "price": 2.37},
                "9704445157": {"id": 9704445157, "name": "X", "shortName": "X", "price": 3.4},
                "9704445158": {"id": 9704445158, "name": "2", "shortName": "2", "price": 3.2},
                # promo selections — should never be emitted
                "1": {"id": 1, "shortName": "1", "price": 2.5},
                "2": {"id": 2, "shortName": "X", "price": 3.6},
                "3": {"id": 3, "shortName": "2", "price": 3.4},
            },
        }
    }
}

# Live envelope: events/markets/selections at the top level.
_LIVE_OK: dict[str, Any] = {
    "events": {
        "86519500": {
            "id": 86519500,
            "sportId": "FOOT",
            "leagueId": 195000,
            "marketIdList": [2784525690],
            "participants": [{"name": "River Plate"}, {"name": "Boca Juniors"}],
            "url": "/live/river-plate-boca-juniors/86519500/",
            "isLive": True,
        },
    },
    "markets": {
        "2784525690": {
            "id": 2784525690,
            "type": "MRES",
            "typeId": 1,
            "name": "Resultado del partido",
            "selectionIdList": [501, 502, 503],
        },
    },
    "selections": {
        "501": {"id": 501, "shortName": "1", "price": 1.8},
        "502": {"id": 502, "shortName": "X", "price": 3.5},
        "503": {"id": 503, "shortName": "2", "price": 4.2},
    },
}


def _client(payload: Any, *, status: int = 200, as_json: bool = True) -> httpx.AsyncClient:
    """An AsyncClient whose every request returns the given canned response."""

    def handler(_request: httpx.Request) -> httpx.Response:
        if as_json:
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text=str(payload))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- fetch_live_soccer: end to end through MockTransport ----


async def test_prematch_extracts_standard_1x2() -> None:
    async with _client(_PREMATCH_OK) as client:
        scraper = BetanoScraper(client, mode="prematch")
        snaps = [s async for s in scraper.fetch_live_soccer()]

    assert len(snaps) == 3  # only the one real match, MRES (not the promo)
    assert {s.platform for s in snaps} == {"betano"}
    assert {s.platform_event_id for s in snaps} == {"85324875"}
    assert {s.platform_market_id for s in snaps} == {"2784995846"}  # MRES, not MR12
    by_outcome = {s.raw_outcome_name: s for s in snaps}
    assert by_outcome["Paris Saint-Germain"].decimal_odds == 2.37
    assert by_outcome["Empate"].decimal_odds == 3.4
    assert by_outcome["Arsenal FC"].decimal_odds == 3.2
    assert all(s.raw_event_name == "Paris Saint-Germain vs Arsenal FC" for s in snaps)
    assert all(s.max_stake is None for s in snaps)


async def test_live_extracts_1x2_from_top_level_block() -> None:
    async with _client(_LIVE_OK) as client:
        scraper = BetanoScraper(client, mode="live")
        snaps = [s async for s in scraper.fetch_live_soccer()]

    assert len(snaps) == 3
    assert {s.platform for s in snaps} == {"betano"}
    by_outcome = {s.raw_outcome_name: s.decimal_odds for s in snaps}
    assert by_outcome == {"River Plate": 1.8, "Empate": 3.5, "Boca Juniors": 4.2}


async def test_prematch_skips_non_foot_outright_and_esports() -> None:
    async with _client(_PREMATCH_OK) as client:
        scraper = BetanoScraper(client, mode="prematch")
        snaps = [s async for s in scraper.fetch_live_soccer()]
    # Tennis, outright, and esports events all produced nothing.
    assert {s.platform_event_id for s in snaps} == {"85324875"}


async def test_prematch_drops_started_event() -> None:
    """Prematch mode is fail-closed on in-play: an event whose startTime is
    already past, or one flagged ``liveNow``, yields nothing even when its
    1X2 market is otherwise valid."""
    # Past start time → treated as in-play.
    past = copy.deepcopy(_PREMATCH_OK)
    past["data"]["topEventsV2"]["events"]["85324875"]["startTime"] = 1700000000000
    async with _client(past) as client:
        scraper = BetanoScraper(client, mode="prematch")
        snaps = [s async for s in scraper.fetch_live_soccer()]
    assert snaps == []

    # Future start but flagged live → in-play.
    live = copy.deepcopy(_PREMATCH_OK)
    live["data"]["topEventsV2"]["events"]["85324875"]["liveNow"] = True
    async with _client(live) as client:
        scraper = BetanoScraper(client, mode="prematch")
        snaps = [s async for s in scraper.fetch_live_soccer()]
    assert snaps == []


# ---- contract errors ----


async def test_prematch_missing_envelope_raises() -> None:
    async with _client({"data": {}}) as client:
        scraper = BetanoScraper(client, mode="prematch")
        with pytest.raises(BetanoContractError, match="topEventsV2"):
            [s async for s in scraper.fetch_live_soccer()]


async def test_missing_blocks_raises() -> None:
    async with _client({"events": {}, "markets": {}}) as client:  # no selections
        scraper = BetanoScraper(client, mode="live")
        with pytest.raises(BetanoContractError, match="events/markets/selections"):
            [s async for s in scraper.fetch_live_soccer()]


async def test_cloudflare_html_body_raises() -> None:
    async with _client("<html>Just a moment...</html>", as_json=False) as client:
        scraper = BetanoScraper(client, mode="live")
        with pytest.raises(BetanoContractError, match="non-JSON"):
            [s async for s in scraper.fetch_live_soccer()]


async def test_http_error_status_raises() -> None:
    async with _client({}, status=403) as client:
        scraper = BetanoScraper(client, mode="live")
        with pytest.raises(BetanoContractError, match="HTTP 403"):
            [s async for s in scraper.fetch_live_soccer()]


# ---- construction ----


def test_unknown_mode_rejected() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(ValueError, match="unknown Betano mode"):
        BetanoScraper(client, mode="bogus")  # type: ignore[arg-type]


def test_both_modes_report_same_platform_distinct_cadence() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    live = BetanoScraper(client, mode="live")
    prematch = BetanoScraper(client, mode="prematch")
    assert live.platform_name == prematch.platform_name == "betano"
    assert live.poll_interval_sec < prematch.poll_interval_sec


# ---- pure parser edge cases ----


def test_parser_skips_event_without_mres_market() -> None:
    block = {
        "events": {
            "1": {
                "id": 1,
                "sportId": "FOOT",
                "marketIdList": [10],  # only a promo / non-1X2 market
                "participants": [{"name": "Home"}, {"name": "Away"}],
                "url": "/cuotas-de-partido/home-away/1/",
            }
        },
        "markets": {"10": {"id": 10, "typeId": 2850, "selectionIdList": [100, 101, 102]}},
        "selections": {
            "100": {"id": 100, "shortName": "1", "price": 2.0},
            "101": {"id": 101, "shortName": "X", "price": 3.0},
            "102": {"id": 102, "shortName": "2", "price": 4.0},
        },
    }
    assert list(_parse_danae_soccer_1x2(block, observed_at=0.0)) == []


def test_parser_skips_invalid_prices() -> None:
    block = {
        "events": {
            "1": {
                "id": 1,
                "sportId": "FOOT",
                "marketIdList": [10],
                "participants": [{"name": "Home"}, {"name": "Away"}],
                "url": "/live/home-away/1/",
            }
        },
        "markets": {"10": {"id": 10, "typeId": 1, "selectionIdList": [100, 101, 102]}},
        "selections": {
            "100": {"id": 100, "shortName": "1", "price": 1.0},  # <= 1.0, skip
            "101": {"id": 101, "shortName": "X", "price": "3.0"},  # non-numeric, skip
            "102": {"id": 102, "shortName": "2", "price": 4.0},  # ok
        },
    }
    snaps = list(_parse_danae_soccer_1x2(block, observed_at=0.0))
    assert len(snaps) == 1
    assert isinstance(snaps[0], RawOddsSnapshot)
    assert snaps[0].raw_outcome_name == "Away"
    assert snaps[0].decimal_odds == 4.0
