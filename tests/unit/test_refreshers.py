"""Unit tests for Tier-2 surgical quote refreshers.

Each refresher is tested in isolation with a mocked scraper.
`MultiPlatformRefresher` dispatch logic is tested with mock per-platform
refreshers + a mock Tier-1 fallback.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from src.arbitrage.quotes import OddsQuote
from src.ingestion.scrapers.base import RawOddsSnapshot
from src.ingestion.scrapers.betsson import BetssonContractError
from src.ingestion.scrapers.betwarrior import BetWarriorContractError
from src.ingestion.scrapers.bplay import BplayContractError
from src.risk.refreshers import (
    BetanoQuoteRefresher,
    BetssonQuoteRefresher,
    BetWarriorQuoteRefresher,
    BplayXMLQuoteRefresher,
    MultiPlatformRefresher,
)
from src.risk.verifier import FreshQuote


def _leg(
    platform: str,
    platform_event_id: str | None = "evt-1",
    platform_outcome_id: str | None = "outcome-1",
    decimal_odds: float = 2.0,
) -> OddsQuote:
    return OddsQuote(
        platform=platform,
        market_id="fx-test|1x2",
        outcome="HOME",
        decimal_odds=decimal_odds,
        max_stake=None,
        timestamp=1000.0,
        platform_outcome_id=platform_outcome_id,
        platform_event_id=platform_event_id,
    )


def _snap(
    platform: str,
    platform_event_id: str,
    platform_outcome_id: str,
    decimal_odds: float,
) -> RawOddsSnapshot:
    return RawOddsSnapshot(
        platform=platform,
        platform_event_id=platform_event_id,
        platform_market_id="mkt-1",
        platform_outcome_id=platform_outcome_id,
        raw_event_name="A vs B",
        raw_market_name="any",
        raw_outcome_name="any",
        decimal_odds=decimal_odds,
        max_stake=None,
        timestamp=time.time(),
    )


# ---- BetssonQuoteRefresher ----


class TestBetssonQuoteRefresher:
    async def test_finds_matching_outcome(self) -> None:
        scraper = AsyncMock()
        scraper.fetch_event_quotes = AsyncMock(
            return_value=[
                _snap("betsson-pba", "f-evt", "out-1", 2.05),
                _snap("betsson-pba", "f-evt", "out-2", 3.10),
            ]
        )
        refresher = BetssonQuoteRefresher(scraper=scraper)
        leg = _leg("betsson-pba", "f-evt", "out-2")

        fq = await refresher.refresh(leg)
        assert fq.decimal_odds == pytest.approx(3.10)
        assert fq.tier == 2
        assert fq.platform_outcome_id == "out-2"
        scraper.fetch_event_quotes.assert_awaited_once_with("f-evt")

    async def test_no_outcome_id_returns_tier_zero(self) -> None:
        scraper = AsyncMock()
        scraper.fetch_event_quotes = AsyncMock()
        refresher = BetssonQuoteRefresher(scraper=scraper)
        leg = _leg("betsson-pba", "f-evt", platform_outcome_id=None)

        fq = await refresher.refresh(leg)
        assert fq.tier == 0
        assert fq.decimal_odds is None
        scraper.fetch_event_quotes.assert_not_called()

    async def test_outcome_not_in_response_returns_not_found(self) -> None:
        scraper = AsyncMock()
        scraper.fetch_event_quotes = AsyncMock(
            return_value=[_snap("betsson-pba", "f-evt", "out-other", 2.05)]
        )
        refresher = BetssonQuoteRefresher(scraper=scraper)
        leg = _leg("betsson-pba", "f-evt", "out-missing")

        fq = await refresher.refresh(leg)
        assert fq.decimal_odds is None
        assert fq.tier == 2  # fetch succeeded, just no match

    async def test_scraper_error_raises(self) -> None:
        scraper = AsyncMock()
        scraper.fetch_event_quotes = AsyncMock(
            side_effect=BetssonContractError("simulated")
        )
        refresher = BetssonQuoteRefresher(scraper=scraper)
        leg = _leg("betsson-pba", "f-evt", "out-1")
        with pytest.raises(BetssonContractError):
            await refresher.refresh(leg)


# ---- BetanoQuoteRefresher (bulk-feed scan) ----


class _FakeBetanoScraper:
    def __init__(self, snaps: list[RawOddsSnapshot]) -> None:
        self._snaps = snaps

    async def fetch_live_soccer(self):  # type: ignore[no-untyped-def]
        for s in self._snaps:
            yield s


class TestBetanoQuoteRefresher:
    async def test_finds_matching_outcome_in_bulk_feed(self) -> None:
        scraper = _FakeBetanoScraper(
            [_snap("betano", "85", "sel-1", 2.37), _snap("betano", "85", "sel-2", 3.40)]
        )
        fq = await BetanoQuoteRefresher(scraper=scraper).refresh(  # type: ignore[arg-type]
            _leg("betano", "85", "sel-2")
        )
        assert fq.decimal_odds == pytest.approx(3.40) and fq.tier == 2

    async def test_outcome_not_in_feed_returns_none(self) -> None:
        scraper = _FakeBetanoScraper([_snap("betano", "85", "sel-1", 2.37)])
        fq = await BetanoQuoteRefresher(scraper=scraper).refresh(  # type: ignore[arg-type]
            _leg("betano", "85", "sel-missing")
        )
        assert fq.decimal_odds is None and fq.tier == 2


# ---- BetWarriorQuoteRefresher ----


class TestBetWarriorQuoteRefresher:
    async def test_finds_matching_outcome(self) -> None:
        scraper = AsyncMock()
        scraper.fetch_event_quotes = AsyncMock(
            return_value=[
                _snap("betwarrior-pba", "kambi-1", "kambi-out-1", 1.75),
                _snap("betwarrior-pba", "kambi-1", "kambi-out-2", 3.50),
            ]
        )
        refresher = BetWarriorQuoteRefresher(scraper=scraper)
        leg = _leg("betwarrior-pba", "kambi-1", "kambi-out-1")
        fq = await refresher.refresh(leg)
        assert fq.decimal_odds == pytest.approx(1.75)
        assert fq.tier == 2

    async def test_scraper_error_raises(self) -> None:
        scraper = AsyncMock()
        scraper.fetch_event_quotes = AsyncMock(
            side_effect=BetWarriorContractError("simulated")
        )
        refresher = BetWarriorQuoteRefresher(scraper=scraper)
        leg = _leg("betwarrior-pba", "kambi-1", "kambi-out-1")
        with pytest.raises(BetWarriorContractError):
            await refresher.refresh(leg)


# ---- BplayXMLQuoteRefresher ----


class TestBplayXMLQuoteRefresher:
    async def test_scans_target_competitions_and_finds_match(self) -> None:
        """Refresher tries each competition in parallel; returns
        match from the one that has the event."""
        scraper = AsyncMock()
        # Make `_competitions` a real dict for `.keys()` introspection
        scraper._competitions = {6674: "UCL", 36146: "Libertadores", 42958: "Conf"}

        def _per_comp(comp_id: int) -> list[RawOddsSnapshot]:
            # Only Libertadores (36146) has the event we want
            if comp_id == 36146:
                return [_snap("bplay-pba", "bp-evt", "bp-out", 5.20)]
            return []

        scraper.fetch_competition_quotes = AsyncMock(side_effect=_per_comp)
        refresher = BplayXMLQuoteRefresher(scraper=scraper)
        leg = _leg("bplay-pba", "bp-evt", "bp-out")

        fq = await refresher.refresh(leg)
        assert fq.decimal_odds == pytest.approx(5.20)
        assert fq.tier == 2
        # All 3 competitions were probed
        assert scraper.fetch_competition_quotes.await_count == 3

    async def test_one_competition_failing_doesnt_kill_others(self) -> None:
        scraper = AsyncMock()
        scraper._competitions = {6674: "UCL", 36146: "Libertadores"}

        async def _per_comp(comp_id: int) -> list[RawOddsSnapshot]:
            if comp_id == 6674:
                raise BplayContractError("simulated")
            return [_snap("bplay-pba", "bp-evt", "bp-out", 4.10)]

        scraper.fetch_competition_quotes = AsyncMock(side_effect=_per_comp)
        refresher = BplayXMLQuoteRefresher(scraper=scraper)
        leg = _leg("bplay-pba", "bp-evt", "bp-out")
        fq = await refresher.refresh(leg)
        assert fq.decimal_odds == pytest.approx(4.10)
        assert fq.tier == 2

    async def test_no_match_returns_not_found(self) -> None:
        """No XML feed has this event → refresher returns
        decimal_odds=None with tier=2; the dispatcher will fall
        back to Tier-1. This covers Bplay SSE-sourced legs whose
        events aren't in the XML feed pattern."""
        scraper = AsyncMock()
        scraper._competitions = {6674: "UCL"}
        scraper.fetch_competition_quotes = AsyncMock(return_value=[])
        refresher = BplayXMLQuoteRefresher(scraper=scraper)
        leg = _leg("bplay-pba", "bp-evt", "bp-out")
        fq = await refresher.refresh(leg)
        assert fq.decimal_odds is None
        assert fq.tier == 2


# ---- MultiPlatformRefresher ----


class TestMultiPlatformDispatch:
    async def test_tier_2_success_used_directly(self) -> None:
        tier_2 = AsyncMock()
        tier_2.refresh = AsyncMock(
            return_value=FreshQuote(
                platform="betsson-pba",
                platform_outcome_id="out-1",
                decimal_odds=2.05,
                observed_at=time.time(),
                tier=2,
            )
        )
        tier_2.platform_name = "betsson-pba"
        tier_1 = AsyncMock()
        tier_1.refresh_batch = AsyncMock()

        multi = MultiPlatformRefresher(
            per_platform={"betsson-pba": tier_2}, tier_1_fallback=tier_1
        )
        leg = _leg("betsson-pba", "f-evt", "out-1")
        results = await multi.refresh_batch([leg])
        assert len(results) == 1
        assert results[0].decimal_odds == pytest.approx(2.05)
        assert results[0].tier == 2
        # Tier-1 was NOT called (tier-2 succeeded)
        tier_1.refresh_batch.assert_not_called()

    async def test_tier_2_exception_falls_back_to_tier_1(self) -> None:
        tier_2 = AsyncMock()
        tier_2.refresh = AsyncMock(side_effect=RuntimeError("transport"))
        tier_2.platform_name = "betsson-pba"
        tier_1 = AsyncMock()
        tier_1.refresh_batch = AsyncMock(
            return_value=[
                FreshQuote(
                    platform="betsson-pba",
                    platform_outcome_id="out-1",
                    decimal_odds=1.95,
                    observed_at=time.time(),
                    tier=1,
                )
            ]
        )
        multi = MultiPlatformRefresher(
            per_platform={"betsson-pba": tier_2}, tier_1_fallback=tier_1
        )
        leg = _leg("betsson-pba", "f-evt", "out-1")
        results = await multi.refresh_batch([leg])
        assert len(results) == 1
        assert results[0].tier == 1
        assert results[0].decimal_odds == pytest.approx(1.95)
        tier_1.refresh_batch.assert_awaited_once()

    async def test_tier_2_not_found_falls_back_to_tier_1(self) -> None:
        """If Tier-2 returns decimal_odds=None (e.g., Bplay SSE leg
        not in XML feeds), the dispatcher tries Tier-1 to confirm."""
        tier_2 = AsyncMock()
        tier_2.refresh = AsyncMock(
            return_value=FreshQuote(
                platform="bplay-pba",
                platform_outcome_id="bp-out",
                decimal_odds=None,  # not found in any XML feed
                observed_at=time.time(),
                tier=2,
            )
        )
        tier_2.platform_name = "bplay-pba"
        tier_1 = AsyncMock()
        tier_1.refresh_batch = AsyncMock(
            return_value=[
                FreshQuote(
                    platform="bplay-pba",
                    platform_outcome_id="bp-out",
                    decimal_odds=3.10,  # found in odds:raw
                    observed_at=time.time(),
                    tier=1,
                )
            ]
        )
        multi = MultiPlatformRefresher(
            per_platform={"bplay-pba": tier_2}, tier_1_fallback=tier_1
        )
        leg = _leg("bplay-pba", "bp-evt", "bp-out")
        results = await multi.refresh_batch([leg])
        assert results[0].tier == 1
        assert results[0].decimal_odds == pytest.approx(3.10)

    async def test_leg_without_ids_uses_tier_1(self) -> None:
        tier_2 = AsyncMock()
        tier_2.refresh = AsyncMock()  # should not be called
        tier_1 = AsyncMock()
        tier_1.refresh_batch = AsyncMock(
            return_value=[
                FreshQuote(
                    platform="betsson-pba",
                    platform_outcome_id="",
                    decimal_odds=None,
                    observed_at=None,
                    tier=0,
                )
            ]
        )
        multi = MultiPlatformRefresher(
            per_platform={"betsson-pba": tier_2}, tier_1_fallback=tier_1
        )
        leg = _leg("betsson-pba", platform_event_id=None, platform_outcome_id=None)
        results = await multi.refresh_batch([leg])
        # Tier-1 was called; Tier-2 was skipped
        tier_2.refresh.assert_not_called()
        tier_1.refresh_batch.assert_awaited_once()
        assert results[0].tier == 0  # whatever tier-1 returned

    async def test_unknown_platform_uses_tier_1(self) -> None:
        """No Tier-2 refresher configured for the leg's platform."""
        tier_1 = AsyncMock()
        tier_1.refresh_batch = AsyncMock(
            return_value=[
                FreshQuote(
                    platform="unknown",
                    platform_outcome_id="x",
                    decimal_odds=2.0,
                    observed_at=time.time(),
                    tier=1,
                )
            ]
        )
        multi = MultiPlatformRefresher(per_platform={}, tier_1_fallback=tier_1)
        leg = _leg("unknown-platform", "evt", "x")
        results = await multi.refresh_batch([leg])
        tier_1.refresh_batch.assert_awaited_once()
        assert results[0].tier == 1

    async def test_mixed_batch_runs_parallel(self) -> None:
        """3 legs from 3 platforms — Tier-2 refreshers should all fire
        in parallel."""
        bets_t2 = AsyncMock()
        bets_t2.refresh = AsyncMock(
            return_value=FreshQuote("betsson-pba", "a", 2.05, time.time(), 2)
        )
        bets_t2.platform_name = "betsson-pba"
        bw_t2 = AsyncMock()
        bw_t2.refresh = AsyncMock(
            return_value=FreshQuote("betwarrior-pba", "b", 1.95, time.time(), 2)
        )
        bw_t2.platform_name = "betwarrior-pba"
        bp_t2 = AsyncMock()
        bp_t2.refresh = AsyncMock(
            return_value=FreshQuote("bplay-pba", "c", 3.10, time.time(), 2)
        )
        bp_t2.platform_name = "bplay-pba"

        multi = MultiPlatformRefresher(
            per_platform={
                "betsson-pba": bets_t2,
                "betwarrior-pba": bw_t2,
                "bplay-pba": bp_t2,
            },
            tier_1_fallback=AsyncMock(),
        )
        legs = [
            _leg("betsson-pba", "e1", "a"),
            _leg("betwarrior-pba", "e2", "b"),
            _leg("bplay-pba", "e3", "c"),
        ]
        results = await multi.refresh_batch(legs)
        assert len(results) == 3
        assert all(r.tier == 2 for r in results)
        # Each refresher was called exactly once
        bets_t2.refresh.assert_awaited_once()
        bw_t2.refresh.assert_awaited_once()
        bp_t2.refresh.assert_awaited_once()
