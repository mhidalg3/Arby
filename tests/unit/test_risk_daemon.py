"""Unit tests for the risk daemon (Redis loop). Redis is mocked at
the `xread`/`xadd` boundary."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.arbitrage.quotes import OddsQuote
from src.risk.evaluator import RiskEvaluator
from src.risk.policy import RiskPolicy
from src.risk.risk_daemon import (
    INPUT_STREAM_NAME,
    OUTPUT_STREAM_NAME,
    RiskDaemon,
    decision_to_stream_fields,
    opportunity_from_stream_fields,
)
from src.semantic.arb_detector import opportunity_to_stream_fields


def _build_opp(
    legs: list[tuple[str, str, float]],
    stakes: list[float],
    realized_roi_pct: float,
    market_id: str = "fx-test|1x2",
) -> ArbitrageOpportunity:
    quotes = tuple(
        OddsQuote(
            platform=p,
            market_id=market_id,
            outcome=o,
            decimal_odds=odds,
            max_stake=None,
            timestamp=1000.0,
        )
        for p, o, odds in legs
    )
    total = sum(stakes)
    return ArbitrageOpportunity(
        legs=quotes,
        stakes=tuple(stakes),
        total_stake=total,
        guaranteed_profit=total * realized_roi_pct / 100,
        margin_pct=realized_roi_pct * 0.95,
        realized_roi_pct=realized_roi_pct,
        capital_utilization=total / 1000,
    )


# ---- Round-trip serialization ----


class TestRoundTrip:
    def test_opportunity_serialize_deserialize_round_trip(self) -> None:
        original = _build_opp(
            legs=[
                ("bplay-pba", "HOME", 1.75),
                ("bplay-pba", "DRAW", 5.80),
                ("betsson-pba", "AWAY", 10.50),
            ],
            stakes=[681.0, 205.5, 113.5],
            realized_roi_pct=19.18,
        )
        # Forward serialization (detector's helper)
        fields = opportunity_to_stream_fields(
            original,
            fixture_id="fx-test",
            market_id="fx-test|1x2",
            home_team="fluminense",
            away_team="bolivar",
            detected_at=1000.0,
        )
        # Inverse (the risk daemon's helper)
        recovered = opportunity_from_stream_fields(fields)
        assert recovered is not None
        assert len(recovered.legs) == 3
        assert recovered.realized_roi_pct == 19.18
        platforms = [leg.platform for leg in recovered.legs]
        assert platforms == ["bplay-pba", "bplay-pba", "betsson-pba"]
        # Stakes preserved
        assert recovered.stakes == (681.0, 205.5, 113.5)

    def test_malformed_stream_entry_returns_none(self) -> None:
        assert opportunity_from_stream_fields({}) is None
        assert opportunity_from_stream_fields({"market_id": "x"}) is None
        # Bad JSON in legs_json
        assert opportunity_from_stream_fields(
            {"market_id": "x", "legs_json": "not json",
             "total_stake": "0", "guaranteed_profit": "0",
             "margin_pct": "0", "realized_roi_pct": "0",
             "capital_utilization": "0"}
        ) is None
        # Empty legs
        assert opportunity_from_stream_fields(
            {"market_id": "x", "legs_json": "[]",
             "total_stake": "0", "guaranteed_profit": "0",
             "margin_pct": "0", "realized_roi_pct": "0",
             "capital_utilization": "0"}
        ) is None


# ---- Decision serialization ----


class TestDecisionSerialization:
    def test_decision_to_stream_fields_all_strings(self) -> None:
        opp = _build_opp(
            legs=[("bplay-pba", "OVER", 2.05), ("betwarrior-pba", "UNDER", 2.00)],
            stakes=[488.0, 500.0],
            realized_roi_pct=1.30,
            market_id="fx-abc|ou_goals|2.5",
        )
        decision = RiskEvaluator(policy=RiskPolicy()).evaluate(opp, now=1500.0)
        fields = decision_to_stream_fields(decision)
        for k, v in fields.items():
            assert isinstance(k, str), k
            assert isinstance(v, str), (k, v)
        # Required audit keys present
        assert fields["verdict"] in ("APPROVED", "REJECTED")
        assert fields["fixture_id"] == "fx-abc"
        assert fields["market_id"] == "fx-abc|ou_goals|2.5"
        assert "," in fields["platforms"]
        assert "1.300000" in fields["realized_roi_pct"]


# ---- Daemon loop ----


async def _drain(
    daemon: RiskDaemon,
    opportunities: list[ArbitrageOpportunity],
    pages_after_first: int = 5,
) -> AsyncMock:
    """Inject `opportunities` as one Redis page of `arb:opportunities`
    entries, then empty pages. Run the daemon loop until it has
    processed the page, then stop it. Returns the xadd mock."""
    daemon.redis_client.xadd = AsyncMock(return_value="0-0")
    # Build one page of (entry_id, fields) tuples
    items = []
    for i, opp in enumerate(opportunities):
        fields = opportunity_to_stream_fields(
            opp,
            fixture_id=opp.legs[0].market_id.split("|")[0],
            market_id=opp.legs[0].market_id,
            home_team="home_team_x",
            away_team="away_team_y",
            detected_at=1000.0 + i,
        )
        items.append((f"{1000 + i}-0", fields))
    pages = [[(INPUT_STREAM_NAME, items)], *([[]] * pages_after_first)]
    daemon.redis_client.xread = AsyncMock(side_effect=pages)

    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except TimeoutError:
        task.cancel()
        raise
    return daemon.redis_client.xadd


class TestDaemonLoop:
    async def test_approves_passing_opportunity(self) -> None:
        daemon = RiskDaemon(
            redis_client=AsyncMock(),
            evaluator=RiskEvaluator(policy=RiskPolicy()),
        )
        opp = _build_opp(
            legs=[("bplay-pba", "OVER", 2.05), ("betwarrior-pba", "UNDER", 2.00)],
            stakes=[488.0, 500.0],
            realized_roi_pct=1.30,
            market_id="fx-abc|ou_goals|2.5",
        )
        xadd = await _drain(daemon, [opp])
        assert xadd.call_count == 1
        args, _ = xadd.call_args
        assert args[0] == OUTPUT_STREAM_NAME
        fields = args[1]
        assert fields["verdict"] == "APPROVED"
        assert fields["reason"] == "approved"
        assert fields["fixture_id"] == "fx-abc"

    async def test_rejects_and_still_emits_decision(self) -> None:
        """Rejected opportunities also get written to the audit
        stream — that's the design."""
        daemon = RiskDaemon(
            redis_client=AsyncMock(),
            evaluator=RiskEvaluator(policy=RiskPolicy()),
        )
        # Single-platform — will reject
        opp = _build_opp(
            legs=[
                ("betwarrior-pba", "YES", 2.10),
                ("betwarrior-pba", "NO", 2.10),
            ],
            stakes=[500.0, 500.0],
            realized_roi_pct=5.0,
            market_id="fx-solo|btts",
        )
        xadd = await _drain(daemon, [opp])
        assert xadd.call_count == 1
        fields = xadd.call_args[0][1]
        assert fields["verdict"] == "REJECTED"
        assert "distinct platform" in fields["reason"]

    async def test_mixed_batch_writes_one_per_opportunity(self) -> None:
        daemon = RiskDaemon(
            redis_client=AsyncMock(),
            evaluator=RiskEvaluator(policy=RiskPolicy()),
        )
        opps = [
            # APPROVED
            _build_opp(
                legs=[("bplay-pba", "HOME", 2.0), ("betwarrior-pba", "AWAY", 2.05)],
                stakes=[506.2, 493.8],
                realized_roi_pct=1.25,
                market_id="fx-a|1x2",
            ),
            # REJECTED (below margin)
            _build_opp(
                legs=[("bplay-pba", "OVER", 2.0), ("betwarrior-pba", "UNDER", 1.99)],
                stakes=[497.5, 502.5],
                realized_roi_pct=0.2,
                market_id="fx-b|ou_goals|2.5",
            ),
            # APPROVED but high-margin warning
            _build_opp(
                legs=[("bplay-pba", "HOME", 1.75), ("betsson-pba", "AWAY", 10.50)],
                stakes=[681.0, 113.5],
                realized_roi_pct=19.18,
                market_id="fx-c|1x2",
            ),
        ]
        xadd = await _drain(daemon, opps)
        assert xadd.call_count == 3
        verdicts = [c.args[1]["verdict"] for c in xadd.call_args_list]
        assert verdicts == ["APPROVED", "REJECTED", "APPROVED"]
        # High-margin warning flag on the third
        assert xadd.call_args_list[2].args[1]["high_margin_warning"] == "1"
        # And not on the first (1.25% < warning threshold)
        assert xadd.call_args_list[0].args[1]["high_margin_warning"] == "0"

    async def test_malformed_entry_skipped(self) -> None:
        daemon = RiskDaemon(
            redis_client=AsyncMock(),
            evaluator=RiskEvaluator(policy=RiskPolicy()),
        )
        daemon.redis_client.xadd = AsyncMock(return_value="0-0")
        pages = [
            [(INPUT_STREAM_NAME, [("1-0", {"only": "this"})])],
            *([[]] * 5),
        ]
        daemon.redis_client.xread = AsyncMock(side_effect=pages)
        stop = asyncio.Event()
        task = asyncio.create_task(daemon.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        # Malformed → no decision emitted
        assert daemon.redis_client.xadd.call_count == 0
