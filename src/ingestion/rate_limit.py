"""Rate-limit-aware circuit breaker for scraper HTTP calls.

Born from the 2026-05-27 Bplay incident: a buggy retry loop hammered
`/en-vivo` ~1×/s through repeated 429s until the platform escalated to
a hard 403 block that cost us the platform for ~24h. The scrapers had
no rate-limit awareness — they logged each failure and immediately
tried the next request at full cadence.

This guard sits in front of every scraper HTTP GET and does two
things:

1. **Honors back-pressure.** A `429` with a `Retry-After` header opens
   the circuit for exactly that long. A `403` (a block, not a
   rate-limit) opens it immediately — we stop hitting a site that has
   told us no.
2. **Fails fast while open.** Once open, subsequent requests raise
   `CircuitOpenError` *without touching the network* for a cooldown
   that grows exponentially on repeated trips. Zero traffic to a
   blocked host is the whole point — even if a caller swallows the
   error (as the old Bplay loop did), no request goes out.

After the cooldown the circuit goes HALF_OPEN: the next request is a
single probe. Success closes the circuit and resets the backoff;
failure re-opens it with a longer cooldown.

The guard is transport-agnostic: callers pass a zero-arg coroutine
that performs the actual request, so the same guard works for any
`httpx` call shape (different URLs, headers, timeouts).

Time is injected (`now_fn`, default `time.monotonic`) so the state
machine is unit-testable without real sleeps.
"""

from __future__ import annotations

import email.utils
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

import httpx
import structlog

log = structlog.get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"  # normal operation
    OPEN = "open"  # failing fast, no network
    HALF_OPEN = "half_open"  # cooldown elapsed, allowing one probe


class CircuitOpenError(RuntimeError):
    """Raised by `RateLimitGuard.get` when the circuit is open. No
    network request was made. Carries `retry_after_sec` so callers /
    pollers can back off intelligently."""

    def __init__(self, platform: str, retry_after_sec: float) -> None:
        self.platform = platform
        self.retry_after_sec = retry_after_sec
        super().__init__(f"{platform}: circuit OPEN, ~{retry_after_sec:.0f}s until retry")


# Statuses that indicate we're being rate-limited / blocked (as opposed
# to a normal 4xx like 404-not-found, which is a content signal).
_DEFAULT_BLOCK_STATUSES: Final[frozenset[int]] = frozenset({429, 503})
_DEFAULT_IMMEDIATE_OPEN_STATUSES: Final[frozenset[int]] = frozenset({403})


@dataclass(frozen=True)
class RateLimitPolicy:
    """Circuit-breaker tunables.

    `failure_threshold` — consecutive block-statuses (or transport
    errors) before the circuit opens. `403`/immediate-open statuses
    bypass this and open on the first occurrence.

    `base_cooldown_sec` doubles each consecutive trip up to
    `max_cooldown_sec`. A `403` block floors the cooldown at
    `block_cooldown_sec` (a block deserves a longer rest than a
    transient 429). A `Retry-After` header raises the cooldown to at
    least its value (capped at `max_cooldown_sec`)."""

    failure_threshold: int = 3
    base_cooldown_sec: float = 30.0
    max_cooldown_sec: float = 600.0
    block_cooldown_sec: float = 120.0  # floor for 403-style blocks
    max_retry_after_sec: float = 600.0  # cap on a server-supplied Retry-After
    block_statuses: frozenset[int] = _DEFAULT_BLOCK_STATUSES
    immediate_open_statuses: frozenset[int] = _DEFAULT_IMMEDIATE_OPEN_STATUSES


def _parse_retry_after(headers: Mapping[str, str], cap: float) -> float | None:
    """Parse a `Retry-After` header (delta-seconds or HTTP-date) into a
    bounded number of seconds. Returns None if absent/unparseable."""
    raw = None
    for k, v in headers.items():
        if k.lower() == "retry-after":
            raw = v
            break
    if raw is None:
        return None
    raw = raw.strip()
    # Form 1: delta-seconds.
    try:
        return min(float(int(raw)), cap)
    except ValueError:
        pass
    # Form 2: HTTP-date. parsedate_to_datetime raises (not returns None)
    # on unparseable input in modern Python — treat any failure as
    # "no usable Retry-After".
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    delta = dt.timestamp() - time.time()
    return max(0.0, min(delta, cap))


@dataclass
class RateLimitGuard:
    """Per-platform circuit breaker. Construct one per scraper (or per
    host) and route every HTTP GET through `get`."""

    platform: str
    policy: RateLimitPolicy = field(default_factory=RateLimitPolicy)
    now_fn: Callable[[], float] = time.monotonic

    state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _trips: int = field(default=0, init=False)  # times opened without recovery
    _open_until: float = field(default=0.0, init=False)
    _log: structlog.BoundLogger = field(init=False)

    def __post_init__(self) -> None:
        self._log = log.bind(platform=self.platform, component="rate_limit_guard")

    def seconds_until_retry(self) -> float:
        """Seconds until the circuit would allow a probe. 0 if not open."""
        if self.state is not CircuitState.OPEN:
            return 0.0
        return max(0.0, self._open_until - self.now_fn())

    async def get(self, send: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
        """Run `send()` under the circuit breaker.

        - If the circuit is OPEN and still cooling: raise
          `CircuitOpenError` without calling `send` (no network).
        - Otherwise call `send`. On a block-status response or a
          transport error, record the failure (possibly opening the
          circuit) and either return the response (so the caller's own
          status handling runs) or re-raise the transport error.
        - On a non-block response, record success and return it.
        """
        now = self.now_fn()
        if self.state is CircuitState.OPEN:
            if now < self._open_until:
                raise CircuitOpenError(self.platform, self._open_until - now)
            # Cooldown elapsed — allow a single probe.
            self.state = CircuitState.HALF_OPEN
            self._log.info("circuit.half_open")

        try:
            resp = await send()
        except httpx.HTTPError as exc:
            # Transport failure (timeout, connection refused, reset).
            # Treat as a failure toward the breaker, then re-raise so
            # the scraper's existing handling runs.
            self._record_failure(status=None, headers={})
            raise exc

        if (
            resp.status_code in self.policy.block_statuses
            or resp.status_code in self.policy.immediate_open_statuses
        ):
            self._record_failure(status=resp.status_code, headers=resp.headers)
        else:
            self._record_success()
        return resp

    # ---- internals ----

    def _record_success(self) -> None:
        if self.state is not CircuitState.CLOSED:
            self._log.info("circuit.closed", recovered_from=self.state.value)
        self.state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._trips = 0

    def _record_failure(self, status: int | None, headers: Mapping[str, str]) -> None:
        self._consecutive_failures += 1
        immediate = status is not None and status in self.policy.immediate_open_statuses
        threshold_hit = self._consecutive_failures >= self.policy.failure_threshold
        # A probe failing in HALF_OPEN always re-opens.
        half_open_probe_failed = self.state is CircuitState.HALF_OPEN

        if not (immediate or threshold_hit or half_open_probe_failed):
            self._log.debug(
                "circuit.failure",
                status=status,
                consecutive_failures=self._consecutive_failures,
            )
            return

        self._open(status=status, headers=headers, immediate=immediate)

    def _open(self, status: int | None, headers: Mapping[str, str], immediate: bool) -> None:
        self._trips += 1
        # Exponential backoff on repeated trips.
        cooldown = min(
            self.policy.base_cooldown_sec * (2 ** (self._trips - 1)),
            self.policy.max_cooldown_sec,
        )
        # A hard block deserves at least the block floor.
        if immediate:
            cooldown = max(cooldown, self.policy.block_cooldown_sec)
        # Honor a server-supplied Retry-After.
        retry_after = _parse_retry_after(headers, self.policy.max_retry_after_sec)
        if retry_after is not None:
            cooldown = min(max(cooldown, retry_after), self.policy.max_cooldown_sec)

        self.state = CircuitState.OPEN
        self._open_until = self.now_fn() + cooldown
        self._consecutive_failures = 0
        self._log.warning(
            "circuit.open",
            status=status,
            cooldown_sec=round(cooldown, 1),
            trips=self._trips,
            retry_after=retry_after,
        )
