"""Tests for LiveOddsReverifier — the executor's live re-verify hook (fail-closed)."""

from __future__ import annotations

from src.arbitrage.quotes import OddsQuote
from src.execution.executor import Leg
from src.execution.reverify import LiveOddsReverifier
from src.risk.verifier import FreshQuote


class _FakeRefresher:
    platform_name = "betano"

    def __init__(self, odds: float | None, *, raise_exc: bool = False) -> None:
        self.odds = odds
        self.raise_exc = raise_exc

    async def refresh(self, leg: OddsQuote) -> FreshQuote:
        if self.raise_exc:
            raise RuntimeError("refetch boom")
        return FreshQuote(
            platform=leg.platform,
            platform_outcome_id=leg.platform_outcome_id,
            decimal_odds=self.odds,
            observed_at=0.0,
            tier=2,
        )


def _leg(platform: str = "betano") -> Leg:
    return Leg(
        platform=platform, match_id="m", market="1X2", outcome="home", stake_ars=50.0,
        odds=2.0, platform_outcome_id="o1", platform_event_ref="e1",
    )


async def test_returns_fresh_live_odds() -> None:
    rv = LiveOddsReverifier({"betano": _FakeRefresher(1.95)})  # type: ignore[dict-item]
    assert await rv(_leg()) == 1.95


async def test_no_refresher_for_platform_fails_closed() -> None:
    assert await LiveOddsReverifier({})(_leg("bplay-pba")) == 0.0


async def test_refresh_error_fails_closed() -> None:
    rv = LiveOddsReverifier({"betano": _FakeRefresher(0.0, raise_exc=True)})  # type: ignore[dict-item]
    assert await rv(_leg()) == 0.0


async def test_market_unavailable_fails_closed() -> None:
    rv = LiveOddsReverifier({"betano": _FakeRefresher(None)})  # type: ignore[dict-item]
    assert await rv(_leg()) == 0.0  # decimal_odds=None → unverified → abort
