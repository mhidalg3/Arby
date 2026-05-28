"""Long-running ingestion daemon — all PBA scrapers, one Redis stream.

Wires every registered scraper's `poll_forever` into a single shared
queue, drained by one `RedisSnapshotSink` into the `odds:raw` stream:

    BetssonScraper.poll_forever      ─┐
                                      │
    BplayPbaScraper.poll_forever      ┼─→ asyncio.Queue ─→ RedisSnapshotSink ─→ Redis
                                      │
    BetWarriorPbaScraper.poll_forever ┤
                                      │
    (next scraper here) ──────────────┘

Each `RawOddsSnapshot` carries its own `platform` field, so downstream
consumers (normalizer, audit tooling) can filter or split as needed.

Lifecycle:
- Every producer (`poll_forever`) plus the consumer (`sink.run`) all
  watch a single shared `stop_event`.
- SIGINT / SIGTERM set the event. Producers exit at their next poll
  boundary; the sink keeps draining until ALL producers have
  completed AND the queue is empty (the producer-task gate we added
  earlier).
- httpx clients + redis client close inside async context managers.

Per-platform httpx clients — same shape (UA, timeout) but separate
connection pools per host. Scraper-specific per-request headers
(Betsson's `brandid` / `marketcode` / `x-sb-type` / `x-sb-jurisdiction`,
BetWarrior's `Origin` / `Referer`) ride on top of the client defaults
via the `headers=` kwarg on each GET, so a shared client would
technically work too — keeping them separate is just operational
hygiene.

Prerequisites:
- Redis reachable at `settings.redis_url` (start with
  `docker compose up -d redis`).
- `.env` satisfies `src.config.Settings`.

Operational env vars:
- `INGESTION_RUN_SECONDS` — auto-stop after N seconds (time-boxed
  smoke). Unset = run indefinitely until SIGINT.
- `INGESTION_QUEUE_MAXSIZE` — bounded in-process buffer between
  producers and sink. Default 10000. Sustained Redis outage
  backpressures the slowest producer instead of leaking memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Sequence

import httpx
import structlog
from redis.asyncio import from_url

from src.config import get_settings
from src.ingestion.rate_limit import RateLimitGuard
from src.ingestion.redis_sink import RedisSnapshotSink
from src.ingestion.scrapers.base import BaseScraper, RawOddsSnapshot
from src.ingestion.scrapers.betsson import BetssonScraper
from src.ingestion.scrapers.betwarrior import (
    BetWarriorPbaDepthScraper,
    BetWarriorPbaScraper,
)
from src.ingestion.scrapers.bplay import BplayPbaScraper
from src.ingestion.scrapers.bplay_sse import BplayPbaSSEScraper
from src.logging_setup import configure_logging

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

DEFAULT_QUEUE_MAXSIZE = 10_000


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
            await asyncio.Event().wait()  # never tripped — equivalent to forever
        return
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return
    stop_event.set()


def _build_scrapers(
    betsson_http: httpx.AsyncClient,
    bplay_http: httpx.AsyncClient,
    bplay_sse_http: httpx.AsyncClient,
    betwarrior_http: httpx.AsyncClient,
    betwarrior_depth_http: httpx.AsyncClient,
) -> list[BaseScraper]:
    """One scraper per supported platform-and-mode.

    Bplay runs TWO scrapers: an HTTP XML feed (pre-match odds on
    tournament-style competitions like Libertadores, UCL, Conference
    League, World Cup) and an SSE live-odds stream (in-play markets
    across all currently-live matches, including Argentine domestic
    Reserves which the XML feed doesn't cover).

    BetWarrior runs two scrapers: a fast list-view (5s, 1X2 only) and
    a slow depth (30s, BTTS + OU goals via per-event polling). Same
    `platform_name = "betwarrior-pba"` on both — the canonicalizer
    treats their snapshots uniformly via the per-event-id cache.

    Set `DISABLE_BPLAY=1` to omit both Bplay scrapers — used when
    Bplay is rate-limited / blocked and we want to keep running
    Betsson + BetWarrior only.
    """
    # BetWarrior's two scrapers hit the same Kambi host — share one
    # circuit so a block detected by either protects both.
    betwarrior_guard = RateLimitGuard(platform="betwarrior-pba")
    scrapers: list[BaseScraper] = [
        BetssonScraper(http_client=betsson_http, subdomain="pba"),
        BetWarriorPbaScraper(http_client=betwarrior_http, guard=betwarrior_guard),
        BetWarriorPbaDepthScraper(
            http_client=betwarrior_depth_http, guard=betwarrior_guard
        ),
    ]
    if os.environ.get("DISABLE_BPLAY", "").strip() not in ("1", "true", "yes"):
        # Bplay XML + SSE also share a host (deportespba.bplay.bet.ar);
        # one shared circuit means a 403 on either source immediately
        # stops traffic from both — the exact escalation that burned us
        # on 2026-05-27.
        bplay_guard = RateLimitGuard(platform="bplay-pba")
        scrapers.extend(
            [
                BplayPbaScraper(http_client=bplay_http, guard=bplay_guard),
                BplayPbaSSEScraper(http_client=bplay_sse_http, guard=bplay_guard),
            ]
        )
    return scrapers


async def _run_pipeline(
    scrapers: Sequence[BaseScraper],
    queue: asyncio.Queue[RawOddsSnapshot],
    stop_event: asyncio.Event,
    sink: RedisSnapshotSink,
    run_seconds: float | None,
    log: structlog.BoundLogger,
) -> None:
    """Start every producer + the consumer, manage graceful shutdown."""
    producers: list[asyncio.Task[None]] = [
        asyncio.create_task(
            scraper.poll_forever(queue, stop_event), name=f"producer-{scraper.platform_name}"
        )
        for scraper in scrapers
    ]

    # The sink's `producer_task` gate needs a single Task representing
    # "all producers done." Wrap the aggregate.
    async def _all_producers_done() -> None:
        await asyncio.gather(*producers, return_exceptions=True)

    aggregator = asyncio.create_task(_all_producers_done(), name="all-producers")
    consumer = asyncio.create_task(
        sink.run(queue, stop_event, producer_task=aggregator), name="consumer"
    )
    auto_stop = asyncio.create_task(
        _maybe_auto_stop_after(stop_event, run_seconds), name="auto_stop"
    )

    watch_tasks: list[asyncio.Task[None]] = [*producers, consumer]
    try:
        # If any single watched task exits before stop is set, treat
        # it as unexpected: log + trip stop so the rest shut down
        # cleanly. (One producer crashing should not silently leave
        # the others running with no visibility.)
        done, _ = await asyncio.wait(watch_tasks, return_when=asyncio.FIRST_COMPLETED)
        if not stop_event.is_set():
            log.warning(
                "daemon.task_exited_unexpectedly",
                finished={t.get_name() for t in done},
            )
            stop_event.set()
        auto_stop.cancel()
        await asyncio.gather(*watch_tasks, aggregator, return_exceptions=True)
    finally:
        auto_stop.cancel()
        # Surface task exceptions so an early failure is visible.
        for t in (*producers, aggregator, consumer, auto_stop):
            if t.done() and not t.cancelled() and t.exception():
                log.exception(
                    "daemon.task_failed",
                    task=t.get_name(),
                    exc_info=t.exception(),
                )


async def main() -> int:
    configure_logging()
    log = structlog.get_logger("daemon.ingestion")
    settings = get_settings()

    run_seconds_env = os.environ.get("INGESTION_RUN_SECONDS", "").strip()
    run_seconds = float(run_seconds_env) if run_seconds_env else None
    queue_maxsize = int(os.environ.get("INGESTION_QUEUE_MAXSIZE", DEFAULT_QUEUE_MAXSIZE))

    log.info(
        "daemon.starting",
        redis_url=settings.redis_url,
        queue_maxsize=queue_maxsize,
        run_seconds=run_seconds,
    )

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event, log)

    queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue(maxsize=queue_maxsize)

    timeout = httpx.Timeout(15.0, connect=5.0)
    headers = {"User-Agent": BROWSER_USER_AGENT}
    # Depth client uses a longer timeout — per-event responses are
    # ~434 KB and large competitions can produce ~100 sequential
    # requests per cycle.
    depth_timeout = httpx.Timeout(45.0, connect=5.0)
    # SSE client needs a long read timeout — the server holds the
    # connection open between events. 60s is the per-cycle streaming
    # duration; leave headroom.
    sse_timeout = httpx.Timeout(75.0, connect=5.0, read=75.0)
    async with (
        httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as betsson_http,
        httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as bplay_http,
        httpx.AsyncClient(
            headers=headers, timeout=sse_timeout, follow_redirects=True
        ) as bplay_sse_http,
        httpx.AsyncClient(
            headers=headers, timeout=timeout, follow_redirects=True
        ) as betwarrior_http,
        httpx.AsyncClient(
            headers=headers, timeout=depth_timeout, follow_redirects=True
        ) as betwarrior_depth_http,
    ):
        scrapers = _build_scrapers(
            betsson_http, bplay_http, bplay_sse_http,
            betwarrior_http, betwarrior_depth_http,
        )
        log.info("daemon.scrapers_initialized", platforms=[s.platform_name for s in scrapers])

        redis_client = from_url(settings.redis_url, decode_responses=True)
        try:
            # Fail fast if Redis isn't reachable. redis-py stubs ping()
            # as `Awaitable[bool] | bool` to share types with the sync
            # client; async returns the awaitable.
            await redis_client.ping()  # type: ignore[misc]
            log.info("daemon.redis_connected")

            sink = RedisSnapshotSink(redis_client=redis_client)
            await _run_pipeline(scrapers, queue, stop_event, sink, run_seconds, log)
        finally:
            await redis_client.aclose()
            log.info("daemon.stopped")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
