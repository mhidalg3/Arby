"""Dry-run verifier daemon — measures detection→verification drift.

Tails `arb:opportunities`, runs the Tier-1 stream-cache verifier on
each entry, emits the verdict + per-leg drift to
`arb:verification_results`. NO bet placement.

After running for ~10-30 minutes alongside the live ingestion +
detector + risk daemons, the operator can XRANGE the
`arb:verification_results` stream to characterize:

- What % of detected opportunities survive re-verification
- The distribution of `margin_delta_pct` (detected − fresh)
- How often `MARKET_UNAVAILABLE` fires (markets suspending mid-flight)
- The distribution of `time_since_detection_sec` (how stale the
  detection was by the time the verifier saw it)

These are the empirical inputs needed before the operator turns on
real bet placement.

Pipeline:

    [arb_detector] → arb:opportunities → [verifier_daemon] → arb:verification_results

Operational env vars:
- `VERIFIER_RUN_SECONDS` — auto-stop after N seconds. Unset = run
  indefinitely until SIGINT.
- `VERIFIER_START_ID` — initial XREAD ID. Default `$` (live tail);
  set to `0` to replay the existing `arb:opportunities` stream
  retroactively.
- `VERIFIER_MIN_FRESH_MARGIN_PCT`, `VERIFIER_MIN_RETENTION_FRACTION`,
  `VERIFIER_MAX_FRESHNESS_AGE_SEC` — VerificationPolicy overrides.
- `VERIFIER_SCAN_COUNT` — how many recent `odds:raw` entries to
  scan per verification (default 5,000).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys

import httpx
import structlog
from redis.asyncio import from_url

from src.config import get_settings
from src.ingestion.scrapers.betsson import BetssonScraper
from src.ingestion.scrapers.betwarrior import BetWarriorPbaDepthScraper
from src.ingestion.scrapers.bplay import BplayPbaScraper
from src.logging_setup import configure_logging
from src.risk.refreshers import (
    BetssonQuoteRefresher,
    BetWarriorQuoteRefresher,
    BplayXMLQuoteRefresher,
    MultiPlatformRefresher,
)
from src.risk.verifier import (
    DEFAULT_SCAN_COUNT,
    QuoteRefresher,
    QuoteVerifier,
    StreamCacheRefresher,
    VerificationPolicy,
)
from src.risk.verifier_daemon import VerifierDaemon

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


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


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


async def main() -> int:
    configure_logging()
    log = structlog.get_logger("daemon.verifier")
    settings = get_settings()

    run_seconds_env = os.environ.get("VERIFIER_RUN_SECONDS", "").strip()
    run_seconds = float(run_seconds_env) if run_seconds_env else None
    start_id = os.environ.get("VERIFIER_START_ID", "$")

    policy_defaults = VerificationPolicy()
    policy = VerificationPolicy(
        min_fresh_margin_pct=_read_float_env(
            "VERIFIER_MIN_FRESH_MARGIN_PCT", policy_defaults.min_fresh_margin_pct
        ),
        min_retention_fraction=_read_float_env(
            "VERIFIER_MIN_RETENTION_FRACTION",
            policy_defaults.min_retention_fraction,
        ),
        max_freshness_age_sec=_read_float_env(
            "VERIFIER_MAX_FRESHNESS_AGE_SEC", policy_defaults.max_freshness_age_sec
        ),
        pre_refresh_delay_sec=_read_float_env(
            "VERIFIER_DELAY_SEC", policy_defaults.pre_refresh_delay_sec
        ),
    )
    scan_count = _read_int_env("VERIFIER_SCAN_COUNT", DEFAULT_SCAN_COUNT)

    log.info(
        "daemon.starting",
        redis_url=settings.redis_url,
        run_seconds=run_seconds,
        start_id=start_id,
        min_fresh_margin_pct=policy.min_fresh_margin_pct,
        min_retention_fraction=policy.min_retention_fraction,
        max_freshness_age_sec=policy.max_freshness_age_sec,
        pre_refresh_delay_sec=policy.pre_refresh_delay_sec,
        scan_count=scan_count,
    )

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event, log)

    redis_client = from_url(settings.redis_url, decode_responses=True)
    headers = {"User-Agent": BROWSER_USER_AGENT}
    # Each per-platform refresher gets its own httpx client. Timeouts
    # are tight — surgical refetch should answer in <1s; failure
    # gracefully degrades to Tier-1.
    surgical_timeout = httpx.Timeout(10.0, connect=5.0)
    # BetWarrior depth endpoint is ~434 KB per event — give it more
    # read budget.
    bw_timeout = httpx.Timeout(20.0, connect=5.0)

    async with (
        httpx.AsyncClient(
            headers=headers, timeout=surgical_timeout, follow_redirects=True
        ) as betsson_http,
        httpx.AsyncClient(
            headers=headers, timeout=bw_timeout, follow_redirects=True
        ) as betwarrior_http,
        httpx.AsyncClient(
            headers=headers, timeout=surgical_timeout, follow_redirects=True
        ) as bplay_http,
    ):
        try:
            await redis_client.ping()  # type: ignore[misc]
            log.info("daemon.redis_connected")

            # Per-platform Tier-2 refreshers — each owns a scraper +
            # http client. The dispatcher routes legs by
            # `platform_name`; legs with unrecognized platforms or
            # missing IDs degrade to Tier-1.
            tier_1 = StreamCacheRefresher(
                redis_client=redis_client, scan_count=scan_count
            )
            per_platform_refreshers: dict[str, QuoteRefresher] = {
                "betsson-pba": BetssonQuoteRefresher(
                    scraper=BetssonScraper(
                        http_client=betsson_http, subdomain="pba"
                    )
                ),
                "betwarrior-pba": BetWarriorQuoteRefresher(
                    scraper=BetWarriorPbaDepthScraper(http_client=betwarrior_http)
                ),
            }
            if os.environ.get("DISABLE_BPLAY", "").strip() not in (
                "1",
                "true",
                "yes",
            ):
                per_platform_refreshers["bplay-pba"] = BplayXMLQuoteRefresher(
                    scraper=BplayPbaScraper(http_client=bplay_http)
                )
            multi = MultiPlatformRefresher(
                per_platform=per_platform_refreshers,
                tier_1_fallback=tier_1,
            )

            verifier = QuoteVerifier(refresher=multi, policy=policy)
            daemon = VerifierDaemon(
                redis_client=redis_client,
                verifier=verifier,
                start_id=start_id,
            )
            daemon_task = asyncio.create_task(
                daemon.run(stop_event), name="verifier_daemon"
            )
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
