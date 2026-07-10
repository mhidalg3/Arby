"""Unit tests for ``PgRecorder._build_row`` — change-compression and canonical stamping.

Redis and Postgres are NOT required: we test the row-building logic directly,
which is where the change-compression and canonical-column stamping lives.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.arbitrage.quotes import OddsQuote
from src.ingestion.pg_recorder import PgRecorder
from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.canonical import (
    CanonicalFixture,
    CanonicalMarket,
    CanonicalMarketCode,
    CanonicalOutcome,
    CanonicalQuote,
)


def _snap(
    platform: str = "betano",
    outcome_id: str = "o1",
    odds: float = 2.0,
    ts: float = 1000.0,
    kickoff: float | None = None,
) -> RawOddsSnapshot:
    return RawOddsSnapshot(
        platform=platform,
        platform_event_id="e1",
        platform_market_id="m1",
        platform_outcome_id=outcome_id,
        raw_event_name="Boca vs River",
        raw_market_name="1X2",
        raw_outcome_name="Boca",
        decimal_odds=odds,
        max_stake=None,
        timestamp=ts,
        kickoff_utc=kickoff,
        transport="poll",
    )


def _fake_cq(kickoff_home: str = "boca", kickoff_away: str = "river") -> CanonicalQuote:
    return CanonicalQuote(
        fixture=CanonicalFixture(
            fixture_id="fx-abc123",
            home_team=kickoff_home,
            away_team=kickoff_away,
        ),
        outcome=CanonicalOutcome(
            market=CanonicalMarket(code=CanonicalMarketCode.H2H_3WAY),
            cell="HOME",
        ),
        odds_quote=OddsQuote(
            platform="betano",
            market_id="fx-abc123|h2h_3way",
            outcome="HOME",
            decimal_odds=2.0,
            max_stake=None,
            timestamp=1000.0,
        ),
    )


def _make_recorder(cq: CanonicalQuote | None = None) -> PgRecorder:
    """Build a PgRecorder with a mocked canonicalizer (no Redis/DB needed)."""
    rec = PgRecorder.__new__(PgRecorder)
    rec._canonicalizer = AsyncMock()
    rec._canonicalizer.canonicalize = AsyncMock(return_value=cq)
    rec._session_id = "test-session"
    rec._last = {}
    rec._last_heartbeat = {}
    rec._heartbeat_sec = 60.0
    rec._log = AsyncMock()
    return rec


class TestChangeCompression:
    async def test_first_sighting_is_change_with_null_prev(self) -> None:
        rec = _make_recorder(cq=None)
        row = await rec._build_row(_snap(odds=2.0, ts=1000.0))
        assert row is not None
        assert row.is_change is True
        assert row.prev_observed_at is None
        assert row.decimal_odds == 2.0
        assert row.recorder_session_id == "test-session"

    async def test_odds_change_writes_change_with_correct_prev(self) -> None:
        rec = _make_recorder(cq=None)
        await rec._build_row(_snap(odds=2.0, ts=1000.0))
        row = await rec._build_row(_snap(odds=2.5, ts=1010.0))
        assert row is not None
        assert row.is_change is True
        assert row.prev_observed_at is not None
        assert row.prev_observed_at.timestamp() == 1000.0

    async def test_unchanged_within_heartbeat_window_is_skipped(self) -> None:
        rec = _make_recorder(cq=None)
        await rec._build_row(_snap(odds=2.0, ts=1000.0))
        row = await rec._build_row(_snap(odds=2.0, ts=1010.0))
        assert row is None

    async def test_unchanged_after_heartbeat_window_writes_heartbeat(self) -> None:
        rec = _make_recorder(cq=None)
        await rec._build_row(_snap(odds=2.0, ts=1000.0))
        row = await rec._build_row(_snap(odds=2.0, ts=1070.0))
        assert row is not None
        assert row.is_change is False
        assert row.prev_observed_at is not None


class TestCanonicalStamping:
    async def test_successful_canonicalization_fills_columns(self) -> None:
        rec = _make_recorder(cq=_fake_cq())
        row = await rec._build_row(_snap(odds=2.0, ts=1000.0, kickoff=1779742576.0))
        assert row is not None
        assert row.market_code == "1x2"
        assert row.cell == "HOME"
        assert row.session_fixture_id == "fx-abc123"
        assert row.fixture_key is not None
        assert "boca" in row.fixture_key
        assert "river" in row.fixture_key
        assert row.kickoff_utc is not None

    async def test_canonicalization_failure_still_persists_row(self) -> None:
        rec = _make_recorder(cq=None)
        row = await rec._build_row(_snap(odds=2.0, ts=1000.0, kickoff=1779742576.0))
        assert row is not None
        assert row.market_code is None
        assert row.cell is None
        assert row.session_fixture_id is None
        assert row.fixture_key is None
        # kickoff_utc is persisted on EVERY row regardless of canonicalization.
        assert row.kickoff_utc is not None
        assert row.platform == "betano"
        assert row.raw_market_name == "1X2"

    async def test_no_kickoff_means_no_fixture_key(self) -> None:
        rec = _make_recorder(cq=_fake_cq())
        row = await rec._build_row(_snap(odds=2.0, ts=1000.0, kickoff=None))
        assert row is not None
        assert row.fixture_key is None
        assert row.kickoff_utc is None
        assert row.session_fixture_id == "fx-abc123"
