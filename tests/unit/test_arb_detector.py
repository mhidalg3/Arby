"""Unit tests for ArbDetector.

Redis is mocked at the `xread`/`xadd` boundary (same pattern as
`test_redis_sink.py`). The tests exercise the detector's logic
against synthetic snapshots that mirror real Betsson + Bplay +
BetWarrior strings observed during ingestion.

Key invariants verified:
  - Cross-platform group detection emits one opportunity when all
    cells are covered with non-stale quotes and margin > threshold.
  - No emission when only 2 of 3 cells are covered (partial group).
  - No emission when overround ≥ 1.0 (no arb).
  - Best (highest) odds per cell selected across platforms.
  - Staleness filter excludes ancient quotes.
  - Emit throttle prevents back-to-back duplicates.
  - Malformed Redis entries are skipped (no exception).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from src.ingestion.redis_sink import snapshot_to_stream_fields
from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.arb_detector import (
    OUTPUT_STREAM_NAME,
    ArbDetector,
    opportunity_to_stream_fields,
)
from src.semantic.canonicalizer import Canonicalizer
from src.semantic.fixture_resolver import FixtureResolver


def _snap(
    platform: str,
    platform_event_id: str,
    raw_event_name: str,
    raw_market_name: str,
    raw_outcome_name: str,
    decimal_odds: float,
    timestamp: float = 1000.0,
) -> RawOddsSnapshot:
    return RawOddsSnapshot(
        platform=platform,
        platform_event_id=platform_event_id,
        platform_market_id=f"mkt-{platform_event_id}",
        platform_outcome_id=f"out-{platform_event_id}-{raw_outcome_name}",
        raw_event_name=raw_event_name,
        raw_market_name=raw_market_name,
        raw_outcome_name=raw_outcome_name,
        decimal_odds=decimal_odds,
        max_stake=None,
        timestamp=timestamp,
    )


def _redis_entries(
    snapshots: list[RawOddsSnapshot], start_offset_ms: int = 1000
) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
    """Build the `xread` return shape from snapshots."""
    items: list[tuple[str, dict[str, str]]] = []
    for i, snap in enumerate(snapshots):
        entry_id = f"{start_offset_ms + i}-0"
        items.append((entry_id, snapshot_to_stream_fields(snap)))
    return [("odds:raw", items)]


async def _drain(
    detector: ArbDetector, snapshots: list[RawOddsSnapshot], pages: int = 1
) -> AsyncMock:
    """Wire `detector.redis_client.xread` to return one page of entries
    built from `snapshots`, then empty pages thereafter. Run the
    detector's loop until it has processed the page and emit count is
    settled, then stop it.
    """
    detector.redis_client.xadd = AsyncMock(return_value="0-0")
    pages_data = [_redis_entries(snapshots), *([[]] * 5)]
    detector.redis_client.xread = AsyncMock(side_effect=pages_data)

    stop_event = asyncio.Event()
    task = asyncio.create_task(detector.run(stop_event))
    # Give the loop time to process; then stop.
    await asyncio.sleep(0.05)
    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except TimeoutError:
        task.cancel()
        raise
    return detector.redis_client.xadd


# Three platforms × three outcomes — overround < 1 → arb exists.
# Order matters: anchor-platform snapshots (BetWarrior + Bplay) must
# arrive before Betsson, because Betsson's flat raw_event_name can't
# create a canonical fixture on its own — it needs an existing
# anchor to attach to via outcome-label matching.
def _arb_snapshots(t: float = 1000.0) -> list[RawOddsSnapshot]:
    """An obvious arb: home=2.5 (Betsson best), draw=4.0 (Bplay), away=4.0 (BetWarrior).
    Sum 1/2.5 + 1/4.0 + 1/4.0 = 0.9 → 10% margin."""
    return [
        # BetWarrior anchor (creates the canonical fixture)
        _snap(
            "betwarrior-pba", "k-1", "Boca - River", "Resultado Final", "1", 2.4, timestamp=t,
        ),
        _snap(
            "betwarrior-pba", "k-1", "Boca - River", "Resultado Final", "X", 3.9, timestamp=t,
        ),
        _snap(
            "betwarrior-pba", "k-1", "Boca - River", "Resultado Final", "2", 4.0, timestamp=t,
        ),
        # Bplay (also anchor — same teams → links to existing fixture)
        _snap(
            "bplay-pba", "b-1", "Boca vs River", "1-X-2", "Empate", 4.0, timestamp=t,
        ),
        # Betsson (uses outcome label "Boca" to anchor against canonical fixture)
        _snap(
            "betsson-pba", "f-1", "boca river", "Ganador del partido", "Boca", 2.5, timestamp=t,
        ),
    ]


def _no_arb_snapshots(t: float = 1000.0) -> list[RawOddsSnapshot]:
    """All three cells covered but overround ≥ 1.0 — no opportunity."""
    return [
        _snap("betwarrior-pba", "k-1", "Boca - River", "Resultado Final", "1", 2.0, timestamp=t),
        _snap("betwarrior-pba", "k-1", "Boca - River", "Resultado Final", "X", 3.0, timestamp=t),
        _snap("betwarrior-pba", "k-1", "Boca - River", "Resultado Final", "2", 4.0, timestamp=t),
    ]


# ---- Cell coverage & detection ----


class TestDetection:
    async def test_full_coverage_with_arb_emits_until_best(self) -> None:
        """As each platform's snapshot arrives, the best-per-cell view
        improves. The detector emits each strict improvement (the
        throttle's improvement-override). The final emission should
        carry the best-platform-per-cell selection."""
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
            budget=1000.0,
            min_margin_pct=1.0,
        )
        xadd_mock = await _drain(detector, _arb_snapshots())
        # ≥1 emission; the last one should reflect the final best-per-cell.
        assert xadd_mock.call_count >= 1
        args, _kwargs = xadd_mock.call_args  # last call
        assert args[0] == OUTPUT_STREAM_NAME
        fields = args[1]
        assert fields["home_team"] == "boca"
        assert fields["away_team"] == "river"
        # Final margin = 1 - (1/2.5 + 1/4.0 + 1/4.0) = 10%
        assert float(fields["margin_pct"]) == pytest.approx(10.0, abs=0.01)
        legs = json.loads(fields["legs_json"])
        assert len(legs) == 3
        platforms = {leg["platform"] for leg in legs}
        # Best-per-cell selection at the final emission:
        # Home: Betsson 2.5 vs BetWarrior 2.4 → Betsson
        # Draw: Bplay 4.0 vs BetWarrior 3.9 → Bplay
        # Away: BetWarrior 4.0 (only)
        assert platforms == {"betsson-pba", "bplay-pba", "betwarrior-pba"}

    async def test_partial_coverage_does_not_emit(self) -> None:
        """Only HOME and DRAW covered → no emission."""
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
        )
        partial = [
            _snap("betwarrior-pba", "k-1", "A - B", "Resultado Final", "1", 2.5),
            _snap("betwarrior-pba", "k-1", "A - B", "Resultado Final", "X", 4.0),
            # No "2" → no AWAY cell
        ]
        xadd_mock = await _drain(detector, partial)
        assert xadd_mock.call_count == 0

    async def test_betsson_without_prior_anchor_drops_without_emit(self) -> None:
        """If Betsson's snapshot arrives before any anchor platform has
        registered the canonical fixture, the canonicalizer drops it.
        The detector sees no quote and emits nothing. In production
        steady state, anchor platforms run continuously so this is rare
        — but order can flip in the first cycle after detector start."""
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
        )
        only_betsson = [
            _snap("betsson-pba", "f-1", "boca river", "Ganador del partido", "Boca", 2.5),
            _snap("betsson-pba", "f-1", "boca river", "Ganador del partido", "Empate", 3.5),
            _snap(
                "betsson-pba", "f-1", "boca river", "Ganador del partido", "River", 3.0
            ),
        ]
        xadd_mock = await _drain(detector, only_betsson)
        assert xadd_mock.call_count == 0

    async def test_no_arb_when_overround_high(self) -> None:
        """All cells covered, but the odds don't yield a Dutch book."""
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
            min_margin_pct=1.0,
        )
        xadd_mock = await _drain(detector, _no_arb_snapshots())
        assert xadd_mock.call_count == 0


# ---- Best-odds-per-cell selection ----


class TestBestOddsSelection:
    async def test_higher_odds_supersede(self) -> None:
        """If platform A then platform B both quote HOME, B's odds win
        iff higher. The emitted leg should carry the highest odds."""
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
        )
        t = 1000.0
        snaps = [
            # Anchor + BetWarrior cells
            _snap("betwarrior-pba", "k-1", "A - B", "Resultado Final", "1", 2.2, timestamp=t),
            _snap("betwarrior-pba", "k-1", "A - B", "Resultado Final", "X", 3.9, timestamp=t),
            _snap("betwarrior-pba", "k-1", "A - B", "Resultado Final", "2", 3.5, timestamp=t),
            # Bplay overlapping with higher odds on home
            _snap("bplay-pba", "b-1", "A vs B", "1-X-2", "A", 2.6, timestamp=t),
        ]
        xadd_mock = await _drain(detector, snaps)
        assert xadd_mock.call_count == 1
        fields = xadd_mock.call_args[0][1]
        legs = json.loads(fields["legs_json"])
        home_leg = next(leg for leg in legs if leg["outcome"] == "HOME")
        # Bplay 2.6 > BetWarrior 2.2 → Bplay wins HOME
        assert home_leg["platform"] == "bplay-pba"
        assert home_leg["decimal_odds"] == pytest.approx(2.6)


# ---- Staleness filter ----


class TestStaleness:
    async def test_stale_quote_excluded_from_best_per_cell(self) -> None:
        """A quote older than `staleness_threshold_sec` relative to the
        latest snapshot is ignored. If excluding it drops cell coverage
        below complete, no emission."""
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
            staleness_threshold_sec=30.0,
        )
        t_old = 1000.0
        t_new = 1100.0  # 100s later — well past the 30s staleness window
        snaps = [
            # Old AWAY quote
            _snap("betwarrior-pba", "k-1", "A - B", "Resultado Final", "2", 4.0, timestamp=t_old),
            # Fresh HOME and DRAW
            _snap("betwarrior-pba", "k-1", "A - B", "Resultado Final", "1", 2.5, timestamp=t_new),
            _snap("betwarrior-pba", "k-1", "A - B", "Resultado Final", "X", 4.0, timestamp=t_new),
        ]
        xadd_mock = await _drain(detector, snaps)
        # AWAY excluded → partial coverage → no emit.
        assert xadd_mock.call_count == 0


# ---- Emission throttle ----


class TestThrottle:
    async def test_throttle_suppresses_unchanged_arb(self) -> None:
        """Two batches of the same arb (same margin) within
        emit_throttle_sec — the second batch is suppressed because no
        leg's odds improve the realized ROI."""
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
            emit_throttle_sec=10.0,
        )
        snaps = _arb_snapshots(t=1000.0) + _arb_snapshots(t=1001.0)
        xadd_mock = await _drain(detector, snaps)
        # First batch emits 1-3 times (improvement chain as each
        # platform contributes a better cell). Second batch (same
        # odds, within throttle) emits 0. With improvement-suppression,
        # total ≤ first-batch chain length, ≤ 3.
        assert 1 <= xadd_mock.call_count <= 3
        # Peak margin shown in the last emission.
        assert float(xadd_mock.call_args[0][1]["margin_pct"]) == pytest.approx(
            10.0, abs=0.01
        )

    async def test_emission_unblocks_after_throttle(self) -> None:
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
            emit_throttle_sec=5.0,
            staleness_threshold_sec=60.0,
        )
        # Two batches 10s apart. The second batch is past the throttle
        # window, so emits even though the margin is the same.
        snaps = _arb_snapshots(t=1000.0) + _arb_snapshots(t=1010.0)
        xadd_mock = await _drain(detector, snaps)
        # First batch contributes 1-3 emissions (improvement chain), second
        # batch adds at least 1 more (window cleared). Total ≥ 2.
        assert xadd_mock.call_count >= 2


# ---- Malformed input ----


class TestBudgetFn:
    async def test_zero_budget_skips_emission(self) -> None:
        """When the stake sizer returns 0.0 (e.g. low-confidence
        opportunity), the detector skips emission entirely — no
        `detect_arbitrage` call, no XADD."""
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
            budget_fn=lambda _legs: 0.0,
        )
        xadd_mock = await _drain(detector, _arb_snapshots())
        assert xadd_mock.call_count == 0

    async def test_custom_budget_fn_drives_total_stake(self) -> None:
        """A budget_fn returning 100,000 ARS gives the opportunity a
        100k total_stake (subject to the dutch_book allocator's math).
        This is the production path with the StakeSizer wired in."""
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
            budget_fn=lambda _legs: 100_000.0,
            min_margin_pct=1.0,
        )
        xadd_mock = await _drain(detector, _arb_snapshots())
        assert xadd_mock.call_count >= 1
        last_fields = xadd_mock.call_args[0][1]
        # total_stake is a float-formatted string in the stream entry.
        # 100k budget → total_stake at or near 100k (allocator may
        # round down slightly for per-leg integer constraints).
        total_stake = float(last_fields["total_stake"])
        assert 99_000.0 < total_stake <= 100_000.0


class TestMalformed:
    async def test_malformed_entry_skipped_without_exception(self) -> None:
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
        )
        detector.redis_client.xadd = AsyncMock(return_value="0-0")
        # Entry missing required fields.
        malformed_page = [("odds:raw", [("1-0", {"only": "this_field"})])]
        detector.redis_client.xread = AsyncMock(
            side_effect=[malformed_page, *([[]] * 5)]
        )
        stop = asyncio.Event()
        task = asyncio.create_task(detector.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        # Detector survived; no emission.
        assert detector.redis_client.xadd.call_count == 0


# ---- Serialization ----


class TestOpportunitySerialization:
    async def test_round_trip_fields_are_strings(self) -> None:
        detector = ArbDetector(
            redis_client=AsyncMock(),
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
        )
        xadd_mock = await _drain(detector, _arb_snapshots())
        assert xadd_mock.call_count >= 1
        fields = xadd_mock.call_args[0][1]  # last call
        for k, v in fields.items():
            assert isinstance(k, str)
            assert isinstance(v, str)
        # Required keys exist
        for key in (
            "fixture_id",
            "market_id",
            "home_team",
            "away_team",
            "margin_pct",
            "realized_roi_pct",
            "guaranteed_profit",
            "total_stake",
            "capital_utilization",
            "legs_json",
            "detected_at",
        ):
            assert key in fields

    def test_opportunity_to_stream_fields_shape(self) -> None:
        """Pure-function shape check independent of the detector loop."""
        from src.arbitrage.dutch_book import ArbitrageOpportunity
        from src.arbitrage.quotes import OddsQuote

        legs = (
            OddsQuote("p1", "fx|1x2", "HOME", 2.5, None, 1000.0),
            OddsQuote("p2", "fx|1x2", "DRAW", 4.0, None, 1000.0),
            OddsQuote("p3", "fx|1x2", "AWAY", 4.0, None, 1000.0),
        )
        opp = ArbitrageOpportunity(
            legs=legs,
            stakes=(400.0, 300.0, 300.0),
            total_stake=1000.0,
            guaranteed_profit=100.0,
            margin_pct=10.0,
            realized_roi_pct=10.0,
            capital_utilization=1.0,
        )
        fields = opportunity_to_stream_fields(
            opp, "fx-test", "fx-test|1x2", "boca", "river", detected_at=1500.0
        )
        assert fields["fixture_id"] == "fx-test"
        assert fields["home_team"] == "boca"
        assert fields["away_team"] == "river"
        assert float(fields["margin_pct"]) == pytest.approx(10.0)
        legs_parsed = json.loads(fields["legs_json"])
        assert [leg["outcome"] for leg in legs_parsed] == ["HOME", "DRAW", "AWAY"]
        assert [leg["stake"] for leg in legs_parsed] == [400.0, 300.0, 300.0]
