"""Tests for the scraper rate-limit circuit breaker.

These pin the state machine that's supposed to prevent a repeat of the
2026-05-27 Bplay block: a 403 must open the circuit immediately, repeated
429s must open it on threshold, an open circuit must fail fast WITHOUT a
network call, and a successful probe must close it. Time is injected so
the tests run without real sleeps.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

from src.ingestion.rate_limit import (
    CircuitOpenError,
    CircuitState,
    RateLimitGuard,
    RateLimitPolicy,
    _parse_retry_after,
)


class _Clock:
    """Manually-advanced monotonic clock for deterministic tests."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _resp(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status_code=status, headers=headers or {})


def _send(
    status: int, headers: dict[str, str] | None = None
) -> Callable[[], Awaitable[httpx.Response]]:
    """A zero-arg async callable returning a canned response — the shape
    `RateLimitGuard.get` expects (mirrors `self._client.get(...)`)."""

    async def _f() -> httpx.Response:
        return _resp(status, headers)

    return _f


def _guard(clock: _Clock, **policy_kwargs: object) -> RateLimitGuard:
    return RateLimitGuard(
        platform="test",
        policy=RateLimitPolicy(**policy_kwargs),  # type: ignore[arg-type]
        now_fn=clock,
    )


class TestClosedPassthrough:
    async def test_success_stays_closed(self) -> None:
        clock = _Clock()
        g = _guard(clock)
        r = await g.get(_send(200))
        assert r.status_code == 200
        assert g.state is CircuitState.CLOSED

    async def test_non_block_4xx_does_not_open(self) -> None:
        """A 404 is a content signal, not a rate-limit — must not trip."""
        clock = _Clock()
        g = _guard(clock)
        for _ in range(5):
            await g.get(_send(404))
        assert g.state is CircuitState.CLOSED


class TestImmediateOpenOn403:
    async def test_single_403_opens_circuit(self) -> None:
        clock = _Clock()
        g = _guard(clock)
        await g.get(_send(403))
        assert g.state is CircuitState.OPEN

    async def test_open_circuit_fails_fast_no_network(self) -> None:
        clock = _Clock()
        g = _guard(clock)
        await g.get(_send(403))  # opens

        calls = {"n": 0}

        async def _counting() -> httpx.Response:
            calls["n"] += 1
            return _resp(200)

        with pytest.raises(CircuitOpenError):
            await g.get(_counting)
        assert calls["n"] == 0  # send() never invoked while open

    async def test_403_floors_cooldown_at_block_cooldown(self) -> None:
        clock = _Clock()
        g = _guard(clock, base_cooldown_sec=5.0, block_cooldown_sec=120.0)
        await g.get(_send(403))
        # base would be 5s but the 403 block floor is 120s.
        assert g.seconds_until_retry() == pytest.approx(120.0, abs=0.1)


class TestThresholdOpenOn429:
    async def test_429_opens_only_after_threshold(self) -> None:
        clock = _Clock()
        g = _guard(clock, failure_threshold=3)
        await g.get(_send(429))
        assert g.state is CircuitState.CLOSED
        await g.get(_send(429))
        assert g.state is CircuitState.CLOSED
        await g.get(_send(429))
        assert g.state is CircuitState.OPEN

    async def test_success_resets_failure_count(self) -> None:
        clock = _Clock()
        g = _guard(clock, failure_threshold=3)
        await g.get(_send(429))
        await g.get(_send(429))
        await g.get(_send(200))  # resets
        await g.get(_send(429))
        assert g.state is CircuitState.CLOSED  # only 1 failure since reset


class TestRetryAfter:
    async def test_retry_after_seconds_raises_cooldown(self) -> None:
        clock = _Clock()
        # threshold 1 so a single 429 opens; base small so Retry-After dominates.
        g = _guard(clock, failure_threshold=1, base_cooldown_sec=5.0, max_cooldown_sec=600.0)
        await g.get(_send(429, {"Retry-After": "90"}))
        assert g.state is CircuitState.OPEN
        assert g.seconds_until_retry() == pytest.approx(90.0, abs=0.1)

    async def test_retry_after_capped(self) -> None:
        clock = _Clock()
        g = _guard(clock, failure_threshold=1, max_retry_after_sec=60.0, max_cooldown_sec=60.0)
        await g.get(_send(429, {"Retry-After": "99999"}))
        assert g.seconds_until_retry() == pytest.approx(60.0, abs=0.1)

    def test_parse_retry_after_seconds(self) -> None:
        assert _parse_retry_after({"Retry-After": "30"}, cap=600) == 30.0

    def test_parse_retry_after_absent(self) -> None:
        assert _parse_retry_after({}, cap=600) is None

    def test_parse_retry_after_garbage(self) -> None:
        assert _parse_retry_after({"Retry-After": "soon"}, cap=600) is None


class TestHalfOpenRecovery:
    async def test_cooldown_elapses_then_probe_succeeds_closes(self) -> None:
        clock = _Clock()
        g = _guard(clock, block_cooldown_sec=100.0)
        await g.get(_send(403))  # open for 100s
        assert g.state is CircuitState.OPEN
        clock.advance(101.0)
        # Next call is a half-open probe; success closes the circuit.
        r = await g.get(_send(200))
        assert r.status_code == 200
        assert g.state is CircuitState.CLOSED
        assert g.seconds_until_retry() == 0.0

    async def test_probe_failure_reopens_with_longer_cooldown(self) -> None:
        clock = _Clock()
        g = _guard(clock, base_cooldown_sec=30.0, block_cooldown_sec=30.0)
        await g.get(_send(403))  # trip 1: ~30s
        first = g.seconds_until_retry()
        clock.advance(first + 1)
        await g.get(_send(403))  # half-open probe fails: trip 2
        second = g.seconds_until_retry()
        assert g.state is CircuitState.OPEN
        assert second > first  # exponential growth on repeated trips


class TestTransportErrors:
    async def test_transport_error_counts_as_failure_and_reraises(self) -> None:
        clock = _Clock()
        g = _guard(clock, failure_threshold=2)

        async def _boom() -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(httpx.ConnectError):
            await g.get(_boom)
        assert g.state is CircuitState.CLOSED  # 1 failure, threshold 2
        with pytest.raises(httpx.ConnectError):
            await g.get(_boom)
        assert g.state is CircuitState.OPEN  # 2nd failure opens
