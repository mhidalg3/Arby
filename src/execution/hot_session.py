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
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

import structlog

from src.execution.executor import LegPlacer
from src.execution.guardrails import Guardrails
from src.execution.leg_placer import BetanoLegPlacer, BetssonLegPlacer, BetWarriorLegPlacer
from src.execution.notify import Notifier, NullNotifier
from src.execution.session import SessionBlock

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
    # A SessionBlock if a responsible-gambling block is on the window (overlay lockout or
    # non-blocking banner), else None. Only is_overlay=True suspends placement. See
    # InSessionTransport.check_session_blocked.
    async def check_session_blocked(self) -> SessionBlock | None: ...
    # On the FIRST detection of a block, dump the overlay DOM + screenshot to ground the
    # exact selector from the real event. Returns the artifact path or None. Never raises.
    async def capture_block_evidence(self, reason: str) -> str | None: ...


class BetssonWarmTransport(WarmTransport, Protocol):
    # Readiness = re-establish + verify the placeable betting context (ctx-).
    async def establish_betsson_context(self) -> bool: ...


class BetanoWarmTransport(WarmTransport, Protocol):
    # Readiness = the cookie-auth /api/balance probe returns a logged-in customer.
    async def check_betano_ready(self) -> bool: ...


class BetWarriorWarmTransport(WarmTransport, Protocol):
    # Readiness = captured Kambi bearer present AND its JWT exp not lapsed (an
    # inactivity logout stops the SPA's token refresh, so the held bearer expires).
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
        status_interval_sec: float = 3600.0,
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
        # Cadence of the "I'm alive — here's what's live" Telegram status report.
        self._status_interval_sec = status_interval_sec
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
        self._readiness: dict[str, bool] = {}  # last probed per-platform readiness
        self._blocks: dict[str, SessionBlock] = {}  # platforms with an RG block (overlay/banner)
        self._started_at = 0.0  # monotonic bot start (set in __aenter__), for block uptime
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._log = log.bind(component="hot_sessions")

    async def __aenter__(self) -> HotSessionManager:
        self._started_at = time.monotonic()
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
        self._status_task = asyncio.create_task(self._status_loop())
        await self._notifier.send(f"🟢 Bot started — {self._status_line()}")
        self._log.info("hot_sessions.started", heartbeat_sec=self._heartbeat_sec)
        return self

    async def __aexit__(self, *exc: object) -> None:
        for task in (self._heartbeat_task, self._status_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
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
        context; Betano hits /api/balance; BetWarrior checks its bearer exp. EACH is
        then also checked for a responsible-gambling LOCKOUT overlay — the session can
        be authenticated (probe green) yet blocked behind a mandatory-break popup, so
        a platform is placeable only if it's ready AND not blocked. Records the
        per-platform result (for the status report) and returns the name of a not-ready
        platform (preferring a BLOCKED one — that's the actionable alert), or None if
        all are placeable."""
        async def _safe(name: str, coro: Awaitable[bool]) -> bool:
            # A probe that raises (cross-origin fetch, nav fault) means NOT READY —
            # suspend + alert, never crash startup or the heartbeat loop.
            try:
                return await coro
            except Exception as exc:  # noqa: BLE001
                self._log.warning("hot_sessions.probe_error", platform=name, error=str(exc))
                return False

        async def _block(name: str, transport: WarmTransport) -> SessionBlock | None:
            try:
                return await transport.check_session_blocked()
            except Exception as exc:  # noqa: BLE001 — never crash the heartbeat
                self._log.warning("hot_sessions.block_probe_error", platform=name, error=str(exc))
                return None

        probes: list[tuple[str, Awaitable[bool], WarmTransport]] = [
            ("betsson", self._betsson.establish_betsson_context(), self._betsson),
            ("betano", self._betano.check_betano_ready(), self._betano),
        ]
        if self._betwarrior is not None:
            probes.append(
                ("betwarrior", self._betwarrior.check_betwarrior_ready(), self._betwarrior)
            )

        states: dict[str, bool] = {}
        blocks: dict[str, SessionBlock] = {}
        for name, coro, transport in probes:
            ready = await _safe(name, coro)
            block = await _block(name, transport)
            if block is not None:
                blocks[name] = block
            # Only a true blocking OVERLAY makes a platform not-placeable; a non-blocking
            # banner (Betano's "12h descanso", confirmed placeable) leaves it ready.
            states[name] = ready and not (block is not None and block.is_overlay)

        self._readiness = states
        transports: dict[str, WarmTransport] = {name: t for name, _coro, t in probes}
        # log NEW lockouts + capture their DOM (uses _blocks as the prior state)
        await self._record_new_blocks(blocks, transports)
        self._blocks = blocks
        # Prefer naming an OVERLAY-blocked platform (actionable: handle the popup) over a
        # plain cold one; otherwise the first not-ready platform.
        overlay_blocked = next((p for p, b in blocks.items() if b.is_overlay), None)
        if overlay_blocked is not None:
            return overlay_blocked
        return next((p for p, ok in states.items() if not ok), None)

    async def _record_new_blocks(
        self, blocks: dict[str, SessionBlock], transports: dict[str, WarmTransport]
    ) -> None:
        """For every NEWLY-detected RG block (a platform not blocked on the prior probe):
        log it with bot uptime + wall-clock + whether it's an overlay, AND capture the
        block's DOM (overlay HTML + screenshot) so the first production event grounds the
        exact selector. Both overlays (suspend) and banners (placeable) are captured/logged
        — we never go blind on a banner. The log is the trigger-learning record: blocks
        clustered at a wall-clock hour ⇒ a curfew; at a ~constant uptime each run ⇒ an
        accumulated play-time limit. Captured once per episode (only on the transition into
        blocked). ``self._blocks`` still holds the PRIOR probe's blocks here."""
        for name, block in blocks.items():
            if name not in self._blocks:
                evidence: str | None = None
                transport = transports.get(name)
                if transport is not None:
                    evidence = await transport.capture_block_evidence(f"{name}:{block.phrase}")
                self._log.warning(
                    "hot_sessions.rg_block",
                    platform=name,
                    phrase=block.phrase,
                    is_overlay=block.is_overlay,
                    uptime_sec=round(time.monotonic() - self._started_at, 1),
                    wall_clock=datetime.now().astimezone().isoformat(timespec="seconds"),
                    evidence=evidence,
                )

    def _status_line(self) -> str:
        """One-line live status: per-platform readiness + whether auto-placement is on.
        A platform behind an RG lockout shows 🚫 (distinct from a plain ❌ cold one)."""
        if not self._readiness:
            return "starting…"

        def mark(p: str, ok: bool) -> str:
            block = self._blocks.get(p)
            if block is not None and block.is_overlay:
                return f"{p} 🚫"  # true lockout overlay; a banner leaves it placeable (✅)
            return f"{p} {'✅' if ok else '❌'}"

        parts = " · ".join(mark(p, ok) for p, ok in self._readiness.items())
        placement = "SUSPENDED" if self._guardrails.kill_switch_tripped else "ON"
        return f"{parts} | auto-placement: {placement}"

    async def _status_loop(self) -> None:
        """Periodic 'I'm alive — here's what's live' Telegram report (default hourly).
        Disconnects are alerted immediately by the readiness heartbeat; this is the
        steady all-clear so silence never looks like life."""
        while True:
            await asyncio.sleep(self._status_interval_sec)
            await self._notifier.send(f"🟢 Bot alive — {self._status_line()}")

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
            await self._notifier.send(self._suspend_alert(not_ready))
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

    def _suspend_alert(self, not_ready: str) -> str:
        """The operator alert for a suspend transition — popup-specific when the named
        platform is behind an RG lockout (so the operator knows to handle the popup,
        not re-login), else the generic cold-session message."""
        block = self._blocks.get(not_ready)
        if block is not None and block.is_overlay:
            return (
                f'🚫 {not_ready} — responsible-gambling LOCKOUT ("{block.phrase}") is blocking '
                f"the window. Auto-placement suspended. Handle it in the {not_ready} window "
                "(it may be a mandatory break — placement can't resume until it clears); "
                "I'll resume automatically. Detection keeps running."
            )
        return (
            f"🔌 {not_ready} session NOT READY — auto-placement suspended. Re-log into the "
            f"{not_ready} window; I'll resume automatically. Detection keeps running."
        )
