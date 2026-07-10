"""Base class for platform-specific scrapers.

Each platform implementation lives in its own module under this directory.
The base class defines the contract; concrete scrapers handle the platform's
quirks (auth, rate limits, API shape, anti-bot countermeasures).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RawOddsSnapshot:
    """A single odds observation as scraped from a platform.

    `raw_*` fields preserve the platform's user-facing labels so the
    semantic layer can resolve them to canonical identifiers without
    losing context. `platform_*_id` fields are the platform's own
    stable handles, used by downstream layers to address the same
    selection across snapshots (staleness checks, eventual bet
    placement) without relying on string matching against labels that
    may change.
    """

    platform: str
    platform_event_id: str
    platform_market_id: str
    platform_outcome_id: str
    raw_event_name: str
    raw_market_name: str
    raw_outcome_name: str
    decimal_odds: float
    max_stake: float | None
    timestamp: float  # unix epoch seconds
    # Optional league/competition label (e.g. Betsson's slug league segment).
    # The fixture resolver reads it to detect a RESERVE division when a platform
    # leaves the team names bare. Default "" — platforms that mark reserves in the
    # team name itself (Betano "… ii", Bplay "… Reserves") need not populate it.
    raw_competition: str = ""
    # Platform-stated kickoff (unix epoch seconds) when the payload carries one;
    # None otherwise. Enables a cross-run-stable analytics fixture key. Betano sets
    # this from `startTime` (ms); BetWarrior from the Kambi `event.start` field.
    # Betsson/Bplay: None unless a kickoff field surfaces in their payloads —
    # never guess.
    kickoff_utc: float | None = None
    # Observation transport: "poll" (timestamp = poll time, interval-censored) or
    # "push" (timestamp = message-decode time, ≈ server update time minus transport
    # latency). The lag analysis carries censoring bounds for "poll" platforms.
    transport: str = "poll"


class BaseScraper(ABC):
    """Contract for platform scrapers.

    Concrete subclasses implement `fetch_live_soccer()` to yield snapshots
    from a single polling pass. The framework calls this in a loop with
    error handling and back-off.
    """

    platform_name: str
    poll_interval_sec: float = 5.0
    backoff_seconds_on_error: float = 30.0

    @abstractmethod
    async def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]:
        """Yield odds snapshots for all currently-live soccer matches.

        Implementations should yield as data becomes available rather than
        buffering an entire response — this lets downstream consumers begin
        processing while the next page is fetched.
        """
        # Empty async generator stub; subclasses must override. The
        # never-reached `yield` is what tells the type checker this is
        # an async generator (rather than a coroutine returning an
        # AsyncIterator), which is what subclasses actually implement.
        if False:  # pragma: no cover
            yield

    async def poll_forever(
        self,
        output_queue: asyncio.Queue[RawOddsSnapshot],
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Run an infinite polling loop with error handling and back-off."""
        stop = stop_event or asyncio.Event()
        bound_log = log.bind(platform=self.platform_name)
        bound_log.info("scraper.started", poll_interval_sec=self.poll_interval_sec)

        while not stop.is_set():
            try:
                count = 0
                async for snapshot in self.fetch_live_soccer():
                    await output_queue.put(snapshot)
                    count += 1
                bound_log.debug("scraper.poll_complete", snapshots=count)
            except asyncio.CancelledError:
                bound_log.info("scraper.cancelled")
                raise
            except Exception as exc:
                bound_log.exception(
                    "scraper.poll_failed",
                    error=str(exc),
                    backoff_seconds=self.backoff_seconds_on_error,
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.backoff_seconds_on_error)
                    break  # stop was set during backoff
                except TimeoutError:
                    continue

            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_sec)
                break
            except TimeoutError:
                pass

        bound_log.info("scraper.stopped")
