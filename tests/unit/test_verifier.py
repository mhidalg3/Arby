"""Unit tests for the pre-execution quote verifier.

Synthesizes Redis stream entries via a mocked `xrevrange` and runs
the verifier's logic against them. Anchored on the real arb shapes
from the 30-min run (Fluminense vs Bolivar, Avai vs Criciuma, etc.).
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from unittest.mock import AsyncMock

import pytest

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.arbitrage.quotes import OddsQuote
from src.ingestion.redis_sink import snapshot_to_stream_fields
from src.ingestion.scrapers.base import RawOddsSnapshot
from src.risk.verifier import (
    DEFAULT_SCAN_COUNT,
    QuoteVerifier,
    StreamCacheRefresher,
    VerificationPolicy,
    VerificationVerdict,
)


def _opp(
    leg_specs: Sequence[tuple[str, str, float, str]],
    realized_roi_pct: float = 5.0,
    market_id: str = "fx-test|1x2",
) -> ArbitrageOpportunity:
    """Build an opportunity. `leg_specs` is
    `[(platform, outcome_cell, decimal_odds, platform_outcome_id), ...]`."""
    legs = tuple(
        OddsQuote(
            platform=p,
            market_id=market_id,
            outcome=oc,
            decimal_odds=odds,
            max_stake=None,
            timestamp=1000.0,
            platform_outcome_id=poid,
        )
        for p, oc, odds, poid in leg_specs
    )
    stakes = tuple(1000.0 / len(legs) for _ in legs)
    total_stake = sum(stakes)
    # Compute the TRUE detection-time margin from the legs' odds —
    # the verifier compares against this, so it must equal what
    # `dutch_book.detect_arbitrage` would compute from the same odds.
    overround = sum(1.0 / odds for _, _, odds, _ in leg_specs)
    margin_pct = (1.0 - overround) * 100.0
    return ArbitrageOpportunity(
        legs=legs,
        stakes=stakes,
        total_stake=total_stake,
        guaranteed_profit=total_stake * realized_roi_pct / 100,
        margin_pct=margin_pct,
        realized_roi_pct=realized_roi_pct,
        capital_utilization=1.0,
    )


def _snapshot(
    platform: str,
    platform_outcome_id: str,
    decimal_odds: float,
    timestamp: float | None = None,
) -> RawOddsSnapshot:
    return RawOddsSnapshot(
        platform=platform,
        platform_event_id="evt-1",
        platform_market_id="mkt-1",
        platform_outcome_id=platform_outcome_id,
        raw_event_name="A vs B",
        raw_market_name="any",
        raw_outcome_name="any",
        decimal_odds=decimal_odds,
        max_stake=None,
        timestamp=timestamp if timestamp is not None else time.time(),
    )


def _mock_xrevrange(snapshots: list[RawOddsSnapshot]) -> AsyncMock:
    """Build a mock that returns the given snapshots (newest first)
    when XREVRANGE is called."""
    # Construct (entry_id, fields_dict) tuples in newest-first order.
    entries = [(f"{int(s.timestamp * 1000)}-0", snapshot_to_stream_fields(s)) for s in snapshots]
    return AsyncMock(return_value=entries)


def _make_refresher(
    snapshots: list[RawOddsSnapshot], *, use_hash: bool = True
) -> StreamCacheRefresher:
    """Build a `StreamCacheRefresher` whose Redis returns the given
    snapshots either via HMGET (hash hit) or XREVRANGE (scan
    fallback). Defaults to hash-hit because that's the production
    fast path. Set `use_hash=False` to force the XREVRANGE
    fallback (simulates hash miss)."""
    redis_client = AsyncMock()
    if use_hash:
        # HMGET path: returns one JSON-encoded value per requested
        # field; tests rebuild the mapping by (platform, outcome_id).
        index = {
            f"{s.platform}:{s.platform_outcome_id}": json.dumps(snapshot_to_stream_fields(s))
            for s in snapshots
        }

        async def _hmget(_hash_name, fields):
            return [index.get(f) for f in fields]

        redis_client.hmget = AsyncMock(side_effect=_hmget)
        # The verifier won't fall back to xrevrange when hash hits,
        # but for safety/legacy tests still return empty there.
        redis_client.xrevrange = AsyncMock(return_value=[])
    else:
        # Force the scan fallback by making hmget return all-None.
        async def _hmget_empty(_hash_name, fields):
            return [None] * len(fields)

        redis_client.hmget = AsyncMock(side_effect=_hmget_empty)
        redis_client.xrevrange = _mock_xrevrange(snapshots)
    return StreamCacheRefresher(redis_client=redis_client)


# ---- Headline path ----


class TestStillValid:
    async def test_unchanged_odds_returns_still_valid(self) -> None:
        """Detection-time odds match fresh odds exactly → STILL_VALID."""
        opp = _opp(
            [
                ("bplay-pba", "HOME", 1.75, "outcome-home-bp"),
                ("bplay-pba", "DRAW", 5.80, "outcome-draw-bp"),
                ("betsson-pba", "AWAY", 10.50, "outcome-away-bs"),
            ],
            realized_roi_pct=19.18,
        )
        fresh_snaps = [
            _snapshot("bplay-pba", "outcome-home-bp", 1.75),
            _snapshot("bplay-pba", "outcome-draw-bp", 5.80),
            _snapshot("betsson-pba", "outcome-away-bs", 10.50),
        ]
        v = QuoteVerifier(refresher=_make_refresher(fresh_snaps))
        result = await v.verify_opportunity(opp, detected_at=time.time() - 2.0)
        assert result.verdict == VerificationVerdict.STILL_VALID
        # fresh_margin should match the original margin (within float epsilon)
        assert result.fresh_margin_pct == pytest.approx(opp.margin_pct, abs=0.01)
        assert all(d.tier == 1 for d in result.per_leg_drift)

    async def test_mild_favorable_drift_still_valid(self) -> None:
        """Odds drifted UP slightly → margin INCREASED → STILL_VALID."""
        opp = _opp(
            [
                ("bplay-pba", "OVER", 2.05, "out-over"),
                ("betwarrior-pba", "UNDER", 2.00, "out-under"),
            ],
            realized_roi_pct=1.30,
        )
        # Both moved up by 1% — better for us
        fresh_snaps = [
            _snapshot("bplay-pba", "out-over", 2.075),
            _snapshot("betwarrior-pba", "out-under", 2.02),
        ]
        v = QuoteVerifier(refresher=_make_refresher(fresh_snaps))
        result = await v.verify_opportunity(opp, detected_at=time.time())
        assert result.verdict == VerificationVerdict.STILL_VALID
        assert result.fresh_margin_pct > opp.margin_pct  # we improved


# ---- Drift rejections ----


class TestDriftRejection:
    async def test_significant_drift_rejects(self) -> None:
        """Detected at 5% margin; odds drifted hard against us. Fresh
        margin below the retention floor → reject."""
        opp = _opp(
            [
                ("bplay-pba", "HOME", 2.5, "out-h"),
                ("betwarrior-pba", "AWAY", 2.5, "out-a"),
            ],
            realized_roi_pct=10.0,
        )
        # Both odds dropped 20% → overround now > 1 → no arb at all
        fresh_snaps = [
            _snapshot("bplay-pba", "out-h", 2.0),
            _snapshot("betwarrior-pba", "out-a", 2.0),
        ]
        v = QuoteVerifier(refresher=_make_refresher(fresh_snaps))
        result = await v.verify_opportunity(opp, detected_at=time.time())
        assert result.verdict == VerificationVerdict.DRIFT_BELOW_ACCEPTANCE
        assert result.fresh_margin_pct is not None
        assert result.fresh_margin_pct < 1.0  # below absolute floor

    async def test_below_retention_threshold_rejects(self) -> None:
        """Detected at 10% margin; fresh at 4% (40% retention < 60% floor).
        Absolute floor (1%) holds but retention floor doesn't."""
        opp = _opp(
            [
                ("bplay-pba", "OVER", 2.5, "out-over"),
                ("betwarrior-pba", "UNDER", 2.5, "out-under"),
            ],
            realized_roi_pct=20.0,
        )
        # detection margin: 1 - (1/2.5 + 1/2.5) = 1 - 0.8 = 20%
        # fresh: 1/2.05 + 1/2.05 ≈ 0.976 → margin 2.4%; retention 2.4/20 = 12% (way below 60%)
        fresh_snaps = [
            _snapshot("bplay-pba", "out-over", 2.05),
            _snapshot("betwarrior-pba", "out-under", 2.05),
        ]
        v = QuoteVerifier(refresher=_make_refresher(fresh_snaps))
        result = await v.verify_opportunity(opp, detected_at=time.time())
        assert result.verdict == VerificationVerdict.DRIFT_BELOW_ACCEPTANCE
        assert result.fresh_margin_pct == pytest.approx(2.4, abs=0.1)


# ---- Market unavailable ----


class TestMarketUnavailable:
    async def test_missing_leg_returns_unavailable(self) -> None:
        """One leg's market vanishes from the refresh data."""
        opp = _opp(
            [
                ("bplay-pba", "HOME", 1.75, "out-h"),
                ("bplay-pba", "DRAW", 5.80, "out-d"),
                ("betsson-pba", "AWAY", 10.50, "out-a"),  # this one not in fresh
            ],
        )
        fresh_snaps = [
            _snapshot("bplay-pba", "out-h", 1.75),
            _snapshot("bplay-pba", "out-d", 5.80),
            # Betsson AWAY missing
        ]
        v = QuoteVerifier(refresher=_make_refresher(fresh_snaps))
        result = await v.verify_opportunity(opp, detected_at=time.time())
        assert result.verdict == VerificationVerdict.MARKET_UNAVAILABLE
        # The missing leg has fresh_odds=None in the drift record
        missing = [d for d in result.per_leg_drift if d.fresh_odds is None]
        assert len(missing) == 1
        assert missing[0].platform == "betsson-pba"


# ---- Staleness ----


class TestStale:
    async def test_old_quotes_yield_stale_data(self) -> None:
        """All quotes found but older than max_freshness_age_sec → STALE_DATA."""
        old_ts = time.time() - 120.0  # 2 minutes old
        opp = _opp(
            [
                ("bplay-pba", "OVER", 2.05, "out-over"),
                ("betwarrior-pba", "UNDER", 2.00, "out-under"),
            ],
            realized_roi_pct=2.0,
        )
        fresh_snaps = [
            _snapshot("bplay-pba", "out-over", 2.05, timestamp=old_ts),
            _snapshot("betwarrior-pba", "out-under", 2.00, timestamp=old_ts),
        ]
        v = QuoteVerifier(
            refresher=_make_refresher(fresh_snaps),
            policy=VerificationPolicy(max_freshness_age_sec=30.0),
        )
        result = await v.verify_opportunity(opp, detected_at=time.time() - 1.0)
        assert result.verdict == VerificationVerdict.STALE_DATA


# ---- No outcome ID (legacy / non-canonicalized leg) ----


class TestNoOutcomeId:
    async def test_legs_without_outcome_id_punt(self) -> None:
        """Legs constructed without `platform_outcome_id` can't be
        verified via Tier 1. The verifier returns NO_OUTCOME_ID for
        the whole opportunity rather than silently approving."""
        legs = (
            OddsQuote(
                platform="bplay-pba",
                market_id="fx-test|btts",
                outcome="YES",
                decimal_odds=1.85,
                max_stake=None,
                timestamp=1000.0,
                # platform_outcome_id deliberately omitted
            ),
            OddsQuote(
                platform="betsson-pba",
                market_id="fx-test|btts",
                outcome="NO",
                decimal_odds=2.00,
                max_stake=None,
                timestamp=1000.0,
            ),
        )
        opp = ArbitrageOpportunity(
            legs=legs,
            stakes=(500.0, 500.0),
            total_stake=1000.0,
            guaranteed_profit=10.0,
            margin_pct=1.0,
            realized_roi_pct=1.0,
            capital_utilization=1.0,
        )
        v = QuoteVerifier(refresher=_make_refresher([]))
        result = await v.verify_opportunity(opp, detected_at=time.time())
        assert result.verdict == VerificationVerdict.NO_OUTCOME_ID


# ---- Refresher batching ----


class TestRefresherBatching:
    async def test_single_hmget_for_multi_leg(self) -> None:
        """One HMGET serves all legs in a multi-leg opportunity —
        we don't want N round-trips for an N-leg arb."""
        opp = _opp(
            [
                ("bplay-pba", "HOME", 1.75, "h"),
                ("bplay-pba", "DRAW", 5.80, "d"),
                ("betsson-pba", "AWAY", 10.50, "a"),
            ],
        )
        snaps = [
            _snapshot("bplay-pba", "h", 1.75),
            _snapshot("bplay-pba", "d", 5.80),
            _snapshot("betsson-pba", "a", 10.50),
        ]
        refresher = _make_refresher(snaps)
        v = QuoteVerifier(refresher=refresher)
        await v.verify_opportunity(opp, detected_at=time.time())
        # HMGET called once with all 3 leg fields. XREVRANGE not
        # called because the hash has all three entries.
        assert refresher.redis_client.hmget.call_count == 1
        refresher.redis_client.xrevrange.assert_not_called()


class TestHashFallback:
    async def test_hash_miss_falls_back_to_xrevrange_scan(self) -> None:
        """When the hash has no entry for a leg, the verifier falls
        back to scanning `odds:raw`. This handles warmup periods
        and any future TTL'd hash variants."""
        opp = _opp(
            [
                ("bplay-pba", "HOME", 1.75, "h"),
                ("betsson-pba", "AWAY", 10.50, "a"),
            ],
        )
        snaps = [
            _snapshot("bplay-pba", "h", 1.75),
            _snapshot("betsson-pba", "a", 10.50),
        ]
        # Force hash miss; verifier should fall through to xrevrange
        refresher = _make_refresher(snaps, use_hash=False)
        v = QuoteVerifier(refresher=refresher)
        result = await v.verify_opportunity(opp, detected_at=time.time())
        assert result.verdict == VerificationVerdict.STILL_VALID
        refresher.redis_client.hmget.assert_called_once()
        refresher.redis_client.xrevrange.assert_called_once()


# ---- Policy override ----


class TestPolicyOverride:
    async def test_relaxed_retention_passes_more(self) -> None:
        """At default 60% retention, a 5%→3% drift fails. At 50%
        retention, the same drift passes."""
        opp = _opp(
            [
                ("bplay-pba", "OVER", 2.5, "out-o"),
                ("betwarrior-pba", "UNDER", 2.5, "out-u"),
            ],
            realized_roi_pct=20.0,
        )
        # margin detection: 20%; fresh: ~12% via odds shift
        fresh_snaps = [
            _snapshot("bplay-pba", "out-o", 2.20),
            _snapshot("betwarrior-pba", "out-u", 2.20),
        ]
        # Strict policy
        strict = QuoteVerifier(
            refresher=_make_refresher(fresh_snaps),
            policy=VerificationPolicy(min_retention_fraction=0.60),
        )
        r1 = await strict.verify_opportunity(opp, detected_at=time.time())
        # Relaxed policy
        relaxed = QuoteVerifier(
            refresher=_make_refresher(fresh_snaps),
            policy=VerificationPolicy(min_retention_fraction=0.40),
        )
        r2 = await relaxed.verify_opportunity(opp, detected_at=time.time())

        assert r1.verdict == VerificationVerdict.DRIFT_BELOW_ACCEPTANCE
        assert r2.verdict == VerificationVerdict.STILL_VALID


class TestDefaults:
    def test_default_scan_count(self) -> None:
        assert DEFAULT_SCAN_COUNT == 5_000


class TestPreRefreshDelay:
    async def test_delay_increases_time_since_detection(self) -> None:
        """Setting `pre_refresh_delay_sec` makes the verifier sleep
        before refresh — so `time_since_detection_sec` measures the
        actual gap a real placement flow would face."""
        opp = _opp(
            [
                ("bplay-pba", "OVER", 2.05, "out-over"),
                ("betwarrior-pba", "UNDER", 2.00, "out-under"),
            ],
            realized_roi_pct=1.30,
        )
        fresh_snaps = [
            _snapshot("bplay-pba", "out-over", 2.05),
            _snapshot("betwarrior-pba", "out-under", 2.00),
        ]
        v = QuoteVerifier(
            refresher=_make_refresher(fresh_snaps),
            policy=VerificationPolicy(pre_refresh_delay_sec=0.05),
        )
        detected_at = time.time()
        result = await v.verify_opportunity(opp, detected_at=detected_at)
        # The verifier should have slept at least 50ms before refresh
        assert result.time_since_detection_sec >= 0.05
        # Less than 1s — sanity bound; we asked for 50ms, not 1s
        assert result.time_since_detection_sec < 1.0
        assert result.verdict == VerificationVerdict.STILL_VALID

    async def test_zero_delay_default_no_sleep(self) -> None:
        """With the default zero delay, verification is essentially
        instantaneous."""
        opp = _opp(
            [
                ("bplay-pba", "OVER", 2.05, "out-over"),
                ("betwarrior-pba", "UNDER", 2.00, "out-under"),
            ],
            realized_roi_pct=1.30,
        )
        fresh_snaps = [
            _snapshot("bplay-pba", "out-over", 2.05),
            _snapshot("betwarrior-pba", "out-under", 2.00),
        ]
        v = QuoteVerifier(refresher=_make_refresher(fresh_snaps))
        result = await v.verify_opportunity(opp, detected_at=time.time())
        assert result.time_since_detection_sec < 0.1  # sub-100ms
