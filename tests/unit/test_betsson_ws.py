"""Tests for the Betsson live WS scraper's snapshot building.

The network layer (connect/subscribe/stream) needs live validation and
isn't unit-tested; `market_value_to_snapshots` — the pure transform from a
decoded Diffusion value to RawOddsSnapshots — is, against the real MW3W
fixture decoded by `betsson_diffusion`.
"""

from __future__ import annotations

import base64
from pathlib import Path

from src.ingestion.scrapers.betsson_diffusion import decode_value_frame
from src.ingestion.scrapers.betsson_ws import (
    BetssonWsScraper,
    market_value_to_snapshots,
)

_MW3W = Path(__file__).parent.parent / "fixtures" / "betsson_diffusion_mw3w.b64"


def test_snapshots_from_real_mw3w_frame() -> None:
    value = decode_value_frame(base64.b64decode(_MW3W.read_text()))
    assert value is not None
    snaps = market_value_to_snapshots(value, "Nice vs Saint-Étienne", 1234.0)
    assert len(snaps) == 3
    by_outcome = {s.raw_outcome_name: s for s in snaps}
    assert by_outcome["home"].decimal_odds == 2.55
    assert by_outcome["draw"].decimal_odds == 2.25
    assert by_outcome["away"].decimal_odds == 3.90
    home = by_outcome["home"]
    assert home.platform == "betsson-pba"
    assert home.platform_event_id == "f-rdwm7m-uK0yqVgGGcW5KIg"
    assert home.platform_market_id == "m-f-rdwm7m-uK0yqVgGGcW5KIg-MW3W"
    assert home.platform_outcome_id.endswith("-MW3W-home")
    assert all(s.raw_event_name == "Nice vs Saint-Étienne" for s in snaps)
    assert all(s.timestamp == 1234.0 and s.max_stake is None for s in snaps)


def test_event_name_falls_back_to_event_id() -> None:
    value = decode_value_frame(base64.b64decode(_MW3W.read_text()))
    assert value is not None
    snaps = market_value_to_snapshots(value, "", 0.0)
    assert all(s.raw_event_name == "f-rdwm7m-uK0yqVgGGcW5KIg" for s in snaps)


def test_non_1x2_or_non_market_values_yield_nothing() -> None:
    assert market_value_to_snapshots({"t": 32, "d": {"messageType": 32}}, "", 0.0) == []
    assert (
        market_value_to_snapshots(
            {"t": 27, "id": "m", "d": {"ei": "x", "mti": "MTG2W", "odds": {}}}, "", 0.0
        )
        == []
    )


async def test_fetch_live_soccer_without_source_is_empty() -> None:
    """No http_client and no discover override → no subscriptions, no
    network, no snapshots."""
    scraper = BetssonWsScraper()
    out = [s async for s in scraper.fetch_live_soccer()]
    assert out == []


async def test_discover_uses_injected_override() -> None:
    async def fake_discover() -> list[tuple[str, str]]:
        return [("f-A", "Team A vs Team B"), ("f-C", "Team C vs Team D")]

    scraper = BetssonWsScraper(discover=fake_discover)
    assert await scraper._discover_live_events() == [
        ("f-A", "Team A vs Team B"),
        ("f-C", "Team C vs Team D"),
    ]


async def test_discover_without_source_is_empty() -> None:
    assert await BetssonWsScraper()._discover_live_events() == []
