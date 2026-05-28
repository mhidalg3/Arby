"""Long-running risk daemon — arb:opportunities → arb:risk_decisions.

Third standalone process in the arby pipeline, after the ingestion
daemon and the arb detector. Separate process means a risk-policy
change or a deterministic-rule bug can be redeployed without
disturbing ingestion or detection.

Pipeline:

    [scrapers] → odds:raw → [arb_detector] → arb:opportunities → [risk_daemon] → arb:risk_decisions

The risk daemon writes EVERY decision (approved AND rejected) to
`arb:risk_decisions` for audit. A future execution agent will
filter on `verdict == "APPROVED"`.

Lifecycle:
- SIGINT / SIGTERM set the stop_event. XREAD has a short block
  timeout so the daemon exits cleanly.
- Redis client closes via `async with` at the end.

Prerequisites:
- Redis reachable at `settings.redis_url`.
- `.env` satisfies `src.config.Settings`.
- An arb detector producing to `arb:opportunities` (otherwise the
  daemon idles forever).

Operational env vars:
- `RISK_RUN_SECONDS` — auto-stop after N seconds (smoke runs).
  Unset = run indefinitely until SIGINT.
- `RISK_START_ID` — initial XREAD ID. Default `$` (live tail). Set
  to `0` to retroactively re-evaluate the entire arb:opportunities
  stream — useful for testing a new policy against captured
  historical opportunities.
- `RISK_MIN_MARGIN_PCT`, `RISK_MAX_MARGIN_PCT`, `RISK_MIN_CONFIDENCE`,
  `RISK_DEFAULT_MAX_STAKE_PER_LEG_ARS` — policy overrides for the
  obvious knobs. Anything finer-grained requires editing
  `RiskPolicy` and redeploying.

Note: total stake is bounded by the stake-sizing policy at the
DETECTOR (see `RISK_TOTAL_CAPITAL_ARS` in `run_arb_detector.py`),
not by this daemon. The risk daemon checks per-leg feasibility
and confidence, not the total-stake total.
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
from src.risk.evaluator import RiskEvaluator
from src.risk.policy import RiskPolicy
from src.risk.risk_daemon import RiskDaemon


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


def _build_policy() -> RiskPolicy:
    """Apply the env-var policy overrides on top of the defaults."""
    defaults = RiskPolicy()
    return RiskPolicy(
        min_margin_pct=_read_float_env("RISK_MIN_MARGIN_PCT", defaults.min_margin_pct),
        max_margin_pct=_read_float_env("RISK_MAX_MARGIN_PCT", defaults.max_margin_pct),
        high_margin_warning_pct=_read_float_env(
            "RISK_HIGH_MARGIN_WARNING_PCT", defaults.high_margin_warning_pct
        ),
        min_distinct_platforms=int(
            os.environ.get(
                "RISK_MIN_DISTINCT_PLATFORMS", defaults.min_distinct_platforms
            )
        ),
        min_confidence=_read_float_env("RISK_MIN_CONFIDENCE", defaults.min_confidence),
        # `max_total_stake_ars` was removed when stake sizing moved
        # to the detector — the detector now bounds total stake via
        # the StakeSizingPolicy (`total_capital_ars × max_fraction_per_arb`).
        default_max_stake_per_leg_ars=_read_float_env(
            "RISK_DEFAULT_MAX_STAKE_PER_LEG_ARS",
            defaults.default_max_stake_per_leg_ars,
        ),
        # platform_reliability + default_unknown_platform_reliability
        # taken straight from defaults — too granular for env config.
        platform_reliability=defaults.platform_reliability,
        default_unknown_platform_reliability=defaults.default_unknown_platform_reliability,
    )


async def main() -> int:
    configure_logging()
    log = structlog.get_logger("daemon.risk")
    settings = get_settings()

    run_seconds_env = os.environ.get("RISK_RUN_SECONDS", "").strip()
    run_seconds = float(run_seconds_env) if run_seconds_env else None
    start_id = os.environ.get("RISK_START_ID", "$")
    policy = _build_policy()

    log.info(
        "daemon.starting",
        redis_url=settings.redis_url,
        run_seconds=run_seconds,
        start_id=start_id,
        min_margin_pct=policy.min_margin_pct,
        max_margin_pct=policy.max_margin_pct,
        min_confidence=policy.min_confidence,
    )

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event, log)

    redis_client = from_url(settings.redis_url, decode_responses=True)
    try:
        await redis_client.ping()  # type: ignore[misc]
        log.info("daemon.redis_connected")

        daemon = RiskDaemon(
            redis_client=redis_client,
            evaluator=RiskEvaluator(policy=policy),
            start_id=start_id,
        )
        daemon_task = asyncio.create_task(daemon.run(stop_event), name="risk_daemon")
        auto_stop_task = asyncio.create_task(
            _maybe_auto_stop_after(stop_event, run_seconds), name="auto_stop"
        )
        try:
            await daemon_task
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
