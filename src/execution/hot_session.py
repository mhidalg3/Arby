"""Hot (warm) sessions: keep both platforms' transports open + authenticated for
the bot's lifetime, so execution places through already-live sessions instead of
launching a browser per bet.

Why this exists: the in-session transports authenticate slowly (login, and for
Betsson the in-app-nav that establishes the betting context). Doing that per bet
is far too slow for arb timing AND re-logging-in repeatedly is a bot signal. The
manager opens both once, arms them, establishes the Betsson context, and runs a
background **heartbeat** that re-warms the sessions (the apps refresh their tokens
while open + active; Betsson's context needs periodic re-establishment). If a
session can't be (re)warmed, the heartbeat trips the kill switch — better to halt
than place through a half-dead session.

The Betsson in-app-nav trigger inside `establish_betsson_context` is
operator-validated against the live DOM; everything here is unit-tested against a
fake transport.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog

from src.execution.executor import LegPlacer
from src.execution.guardrails import Guardrails
from src.execution.leg_placer import BetanoLegPlacer, BetssonLegPlacer

log = structlog.get_logger(__name__)


class WarmTransport(Protocol):
    """A transport the manager opens once and keeps armed for the bot's life."""

    async def __aenter__(self) -> Any: ...
    async def __aexit__(self, *exc: object) -> None: ...
    def arm(self) -> None: ...


class BetssonWarmTransport(WarmTransport, Protocol):
    async def establish_betsson_context(self) -> bool: ...


class HotSessionManager:
    """Owns the live Betano + Betsson transports for the bot's lifetime."""

    def __init__(
        self,
        *,
        betano: WarmTransport,
        betsson: BetssonWarmTransport,
        guardrails: Guardrails,
        heartbeat_sec: float = 300.0,
        login_gate: Callable[[], Awaitable[None]] | None = None,
        arm: bool = True,
    ) -> None:
        self._betano = betano
        self._betsson = betsson
        self._guardrails = guardrails
        self._heartbeat_sec = heartbeat_sec
        # When False, the transports are NOT armed — they open + warm but `fetch`
        # refuses (a dry-run can validate the session lifecycle while making a
        # routing-bug placement impossible).
        self._arm = arm
        # Invoked once after the browsers open but before establishing context —
        # the seam for the initial operator login (or a future automated re-auth).
        # None ⇒ assume the persistent profiles are already logged in.
        self._login_gate = login_gate
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._log = log.bind(component="hot_sessions")

    async def __aenter__(self) -> HotSessionManager:
        await self._betano.__aenter__()
        await self._betsson.__aenter__()
        if self._arm:
            self._betano.arm()
            self._betsson.arm()
        if self._login_gate is not None:
            await self._login_gate()
        if not await self._betsson.establish_betsson_context():
            # Can't reach a placeable Betsson state at startup → don't pretend we
            # can trade; trip the kill switch so the executor aborts everything.
            self._guardrails.trip_kill_switch("betsson context not established at startup")
            self._log.error("hot_sessions.betsson_context_failed")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._log.info("hot_sessions.started", heartbeat_sec=self._heartbeat_sec)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
        await self._betsson.__aexit__(*exc)
        await self._betano.__aexit__(*exc)

    def placers(self) -> dict[str, LegPlacer]:
        """The per-platform placers, bound to the warm transports, for the Executor."""
        return {
            "betano": BetanoLegPlacer(self._betano),  # type: ignore[arg-type]
            "betsson-pba": BetssonLegPlacer(self._betsson),  # type: ignore[arg-type]
        }

    async def heartbeat(self) -> bool:
        """Re-warm the sessions once: re-establish the Betsson betting context
        (which also re-captures a fresh ctx-). Returns False if Betsson can't be
        re-warmed (the caller / loop trips the kill switch)."""
        ok = await self._betsson.establish_betsson_context()
        self._log.info("hot_sessions.heartbeat", betsson_ok=ok)
        return ok

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_sec)
            try:
                if not await self.heartbeat():
                    self._guardrails.trip_kill_switch("betsson session went cold")
                    return
            except Exception as exc:  # noqa: BLE001 — a heartbeat fault must halt, not crash silently
                self._log.error("hot_sessions.heartbeat_error", error=str(exc))
                self._guardrails.trip_kill_switch(f"heartbeat error: {exc!s}")
                return
