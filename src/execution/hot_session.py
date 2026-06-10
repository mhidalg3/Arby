"""Hot (warm) sessions: keep both platforms' transports open + authenticated for
the bot's lifetime, so execution places through already-live sessions instead of
launching a browser per bet.

Why this exists: the in-session transports authenticate slowly (login, and for
Betsson the in-app-nav that establishes the betting context). Doing that per bet
is far too slow for arb timing AND re-logging-in repeatedly is a bot signal. The
manager opens both once, arms them, establishes the Betsson context, and runs a
background **heartbeat** that re-warms the sessions (the apps refresh their tokens
while open + active; Betsson's context needs periodic re-establishment). If a
session goes cold the heartbeat **suspends auto-placement** (trips the kill switch)
and alerts the operator — but it does NOT stop: it keeps probing and AUTO-RESUMES
(resets the kill switch) once the operator has re-logged in. Detection upstream
keeps running throughout; we never trade through a half-dead session, but we never
stop watching either.

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
from src.execution.leg_placer import BetanoLegPlacer, BetssonLegPlacer, BetWarriorLegPlacer
from src.execution.notify import Notifier, NullNotifier

log = structlog.get_logger(__name__)

# The kill-switch reason the manager owns. The heartbeat auto-resets ONLY this
# trip when a session recovers — a hard trip (freeze / daily loss) carries a
# different reason and is left alone.
_NOT_READY_REASON = "session not ready"


class WarmTransport(Protocol):
    """A transport the manager opens once and keeps armed for the bot's life."""

    async def __aenter__(self) -> Any: ...
    async def __aexit__(self, *exc: object) -> None: ...
    def arm(self) -> None: ...


class BetssonWarmTransport(WarmTransport, Protocol):
    # Readiness = re-establish + verify the placeable betting context (ctx-).
    async def establish_betsson_context(self) -> bool: ...


class BetanoWarmTransport(WarmTransport, Protocol):
    # Readiness = the cookie-auth /api/balance probe returns a logged-in customer.
    async def check_betano_ready(self) -> bool: ...


class BetWarriorWarmTransport(WarmTransport, Protocol):
    # Readiness = the PAM checkSessionAlive probe ({"alive":"true"}).
    async def check_betwarrior_ready(self) -> bool: ...


class HotSessionManager:
    """Owns the live Betano + Betsson transports for the bot's lifetime."""

    def __init__(
        self,
        *,
        betano: BetanoWarmTransport,
        betsson: BetssonWarmTransport,
        guardrails: Guardrails,
        heartbeat_sec: float = 300.0,
        login_gate: Callable[[], Awaitable[None]] | None = None,
        arm: bool = True,
        notifier: Notifier | None = None,
        betwarrior: BetWarriorWarmTransport | None = None,
    ) -> None:
        self._betano = betano
        self._betsson = betsson
        # BetWarrior (Kambi) is optional: a stateless bearer session (no context
        # nav), so it just opens + arms + provides its placer. None ⇒ not wired.
        self._betwarrior = betwarrior
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
        self._notifier = notifier or NullNotifier()
        self._suspended_for_cold = False  # we tripped the switch for a cold session
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._log = log.bind(component="hot_sessions")

    async def __aenter__(self) -> HotSessionManager:
        await self._betano.__aenter__()
        await self._betsson.__aenter__()
        if self._betwarrior is not None:
            await self._betwarrior.__aenter__()
        if self._arm:
            self._betano.arm()
            self._betsson.arm()
            if self._betwarrior is not None:
                self._betwarrior.arm()
        if self._login_gate is not None:
            await self._login_gate()
        # Can't reach a placeable state on any platform at startup → suspend
        # auto-placement and alert, but DON'T halt: the heartbeat keeps probing and
        # resumes once the operator finishes logging in. Detection runs regardless.
        await self._apply_health(await self._probe_readiness())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._log.info("hot_sessions.started", heartbeat_sec=self._heartbeat_sec)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
        if self._betwarrior is not None:
            await self._betwarrior.__aexit__(*exc)
        await self._betsson.__aexit__(*exc)
        await self._betano.__aexit__(*exc)

    def placers(self) -> dict[str, LegPlacer]:
        """The per-platform placers, bound to the warm transports, for the Executor."""
        placers: dict[str, LegPlacer] = {
            "betano": BetanoLegPlacer(self._betano),  # type: ignore[arg-type]
            "betsson-pba": BetssonLegPlacer(self._betsson),  # type: ignore[arg-type]
        }
        if self._betwarrior is not None:
            placers["betwarrior-pba"] = BetWarriorLegPlacer(self._betwarrior)  # type: ignore[arg-type]
        return placers

    async def _probe_readiness(self) -> str | None:
        """Probe every wired platform's session-readiness (the API equivalent of
        "is the place button green"): Betsson re-establishes + verifies its betting
        context; Betano hits /api/balance; BetWarrior hits checkSessionAlive. Returns
        the name of the FIRST not-ready platform, or None if all are placeable."""
        if not await self._betsson.establish_betsson_context():
            return "betsson"
        if not await self._betano.check_betano_ready():
            return "betano"
        if self._betwarrior is not None and not await self._betwarrior.check_betwarrior_ready():
            return "betwarrior"
        return None

    async def heartbeat(self) -> bool:
        """Probe all sessions once. Returns True iff every wired platform is placeable."""
        not_ready = await self._probe_readiness()
        self._log.info("hot_sessions.heartbeat", not_ready=not_ready)
        return not_ready is None

    async def _heartbeat_loop(self) -> None:
        """Probe forever. A not-ready session suspends auto-placement + alerts; recovery
        resumes it. The loop never returns on a fault — detection must not stop."""
        while True:
            await asyncio.sleep(self._heartbeat_sec)
            try:
                not_ready = await self._probe_readiness()
            except Exception as exc:  # noqa: BLE001 — a fault suspends, never kills the loop
                self._log.error("hot_sessions.heartbeat_error", error=str(exc))
                not_ready = "unknown"
            await self._apply_health(not_ready)

    async def _apply_health(self, not_ready: str | None) -> None:
        """Reconcile session readiness with the kill switch + alerts on transitions
        only (so we don't spam an alert every heartbeat while a session stays cold).
        ``not_ready`` is the name of an un-placeable platform, or None if all ready."""
        if not_ready is not None and not self._suspended_for_cold:
            self._suspended_for_cold = True
            self._guardrails.trip_kill_switch(_NOT_READY_REASON)
            await self._notifier.send(
                f"🔌 {not_ready} session NOT READY — auto-placement suspended. Re-log into the "
                f"{not_ready} window; I'll resume automatically. Detection keeps running."
            )
        elif not_ready is None and self._suspended_for_cold:
            self._suspended_for_cold = False
            # Reset only OUR trip — leave a hard freeze / daily-loss stop in place.
            if self._guardrails.kill_switch_reason == _NOT_READY_REASON:
                self._guardrails.reset_kill_switch()
                await self._notifier.send("✅ Sessions ready again — auto-placement resumed.")
            else:
                await self._notifier.send(
                    "✅ Sessions ready, but auto-placement stays suspended "
                    f"({self._guardrails.kill_switch_reason}) — resolve + restart to resume."
                )
