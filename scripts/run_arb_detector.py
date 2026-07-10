"""Long-running arbitrage detector — odds:raw → semantic layer → arb:opportunities.

Companion to `scripts/run_ingestion_daemon.py`. Runs as a SEPARATE
process: ingestion writes to `odds:raw`, the detector reads from it,
canonicalizes through the semantic layer, and emits opportunities to
`arb:opportunities`. Keeping the two daemons split means a misbehaving
detector (e.g. canonicalization bug, runaway memory) can be restarted
without disturbing the scrapers — and vice versa.

Pipeline (run alongside the ingestion daemon):

    [scrapers] ──XADD──► odds:raw ──XREAD──► [arb_detector] ──XADD──► arb:opportunities

Lifecycle:
- SIGINT / SIGTERM set the stop_event. The detector's XREAD has a
  short block timeout, so it sees stop quickly and exits cleanly.
- Redis client closes via `async with` at the end.

Prerequisites:
- Redis reachable at `settings.redis_url`
  (`docker compose up -d redis`).
- `.env` satisfies `src.config.Settings`.
- An ingestion daemon producing to `odds:raw`. Without one, the
  detector starts but emits nothing (XREAD blocks).

Operational env vars:
- `DETECTOR_RUN_SECONDS` — auto-stop after N seconds (smoke runs).
  Unset = run indefinitely until SIGINT.
- `DETECTOR_BUDGET` — total capital for stake allocation. Default 1000.
- `DETECTOR_MIN_MARGIN_PCT` — drop opportunities below this realized
  ROI percent. Default 1.0.
- `DETECTOR_STALENESS_SEC` — quotes older than this (relative to the
  most recent snapshot for the market) are excluded from
  best-per-cell selection. Default 30.
- `DETECTOR_EMIT_THROTTLE_SEC` — minimum gap between identical or
  worse emissions per market. Strictly better opportunities bypass
  the throttle. Default 5.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys

import structlog
from redis.asyncio import from_url

from src.config import get_settings
from src.logging_setup import configure_logging
from src.risk.stake_sizing import (
    DEFAULT_TOTAL_CAPITAL_ARS,
    StakeSizer,
    StakeSizingPolicy,
)
from src.semantic.arb_detector import (
    DEFAULT_EMIT_THROTTLE_SEC,
    DEFAULT_MIN_MARGIN_PCT,
    DEFAULT_STALENESS_SEC,
    ArbDetector,
)
from src.semantic.canonicalizer import Canonicalizer
from src.semantic.fixture_resolver import FixtureResolver


def _install_signal_handlers(stop_event: asyncio.Event, log: structlog.BoundLogger) -> None:
    loop = asyncio.get_running_loop()

    def _trip(sig_name: str) -> None:
        log.info("daemon.signal", signal=sig_name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _trip, sig.name)


async def _maybe_auto_stop_after(stop_event: asyncio.Event, seconds: float | None) -> None:
    if seconds is None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        return
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return
    stop_event.set()


def _read_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


async def main() -> int:
    configure_logging()
    log = structlog.get_logger("daemon.arb_detector")
    settings = get_settings()

    run_seconds_env = os.environ.get("DETECTOR_RUN_SECONDS", "").strip()
    run_seconds = float(run_seconds_env) if run_seconds_env else None
    min_margin_pct = _read_float_env("DETECTOR_MIN_MARGIN_PCT", DEFAULT_MIN_MARGIN_PCT)
    staleness_sec = _read_float_env("DETECTOR_STALENESS_SEC", DEFAULT_STALENESS_SEC)
    emit_throttle_sec = _read_float_env("DETECTOR_EMIT_THROTTLE_SEC", DEFAULT_EMIT_THROTTLE_SEC)

    # Stake-sizing knobs. Total capital MUST be tuned per deployment;
    # the default is a placeholder (~1M ARS ≈ $800-1k USD). Other
    # knobs default to the StakeSizingPolicy class defaults.
    total_capital_ars = _read_float_env("RISK_TOTAL_CAPITAL_ARS", DEFAULT_TOTAL_CAPITAL_ARS)
    max_fraction_per_arb = _read_float_env(
        "RISK_MAX_FRACTION_PER_ARB", StakeSizingPolicy().max_fraction_per_arb
    )
    min_total_stake_ars = _read_float_env(
        "RISK_MIN_TOTAL_STAKE_ARS", StakeSizingPolicy().min_total_stake_ars
    )

    stake_policy = StakeSizingPolicy(
        total_capital_ars=total_capital_ars,
        max_fraction_per_arb=max_fraction_per_arb,
        min_total_stake_ars=min_total_stake_ars,
    )
    stake_sizer = StakeSizer(policy=stake_policy)

    log.info(
        "daemon.starting",
        redis_url=settings.redis_url,
        run_seconds=run_seconds,
        total_capital_ars=total_capital_ars,
        max_fraction_per_arb=max_fraction_per_arb,
        min_total_stake_ars=min_total_stake_ars,
        min_margin_pct=min_margin_pct,
        staleness_sec=staleness_sec,
        emit_throttle_sec=emit_throttle_sec,
    )

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event, log)

    redis_client = from_url(settings.redis_url, decode_responses=True)
    try:
        # Fail fast if Redis is unreachable. Mirrors the ingestion
        # daemon's startup probe.
        await redis_client.ping()  # type: ignore[misc]
        log.info("daemon.redis_connected")

        canonicalizer = Canonicalizer(fixture_resolver=FixtureResolver())
        detector = ArbDetector(
            redis_client=redis_client,
            canonicalizer=canonicalizer,
            budget_fn=stake_sizer.compute_budget,
            min_margin_pct=min_margin_pct,
            staleness_threshold_sec=staleness_sec,
            emit_throttle_sec=emit_throttle_sec,
        )

        detector_task = asyncio.create_task(detector.run(stop_event), name="detector")
        auto_stop_task = asyncio.create_task(
            _maybe_auto_stop_after(stop_event, run_seconds), name="auto_stop"
        )
        try:
            await detector_task
        finally:
            auto_stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await auto_stop_task
    finally:
        await redis_client.aclose()
        log.info("daemon.stopped")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
