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
# Hard bound on a single readiness probe (establish_betsson_context can reach ~90s
# legit: a 60s networkidle goto + a 30s passive ctx-poll). A probe that HANGS past
# this — observed 2026-06-24: Betsson's page.goto(networkidle) wedged when a stale
# betslip kept the SPA re-validating, and Playwright's own 60s goto timeout did NOT
# fire, freezing the whole heartbeat loop on one await (try/except can't catch a
# hang). asyncio.wait_for enforces a real cancellation so the loop survives + the
# session auto-recovers once the window is usable again. Generous vs the ~90s legit
# ceiling so a slow-but-healthy probe is never falsely killed.
_PROBE_TIMEOUT_S = 120.0

# Betsson's reality-check close is a real bookmaker-window click, so it stays bounded.
# But one attempt is too brittle: a click can land during SPA animation/cooldown, the
# custom element can ignore it once, or Playwright can miss the center point. Retry inside
# the same background recovery task, not on every heartbeat, so a persistent popup gets a
# few chances without creating an unbounded click loop.
_REALITY_CHECK_MAX_CLOSE_ATTEMPTS = 3
_REALITY_CHECK_CLOSE_RETRY_DELAY_S = 10.0
_REALITY_CHECK_CLOSE_ATTEMPT_TIMEOUT_S = 20.0
_REALITY_CHECK_RECHECK_TIMEOUT_S = 10.0


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
    # Auto-extend a session-timer warning by clicking the platform's "conserve session"
    # button. Returns True iff the popup dismissed; False → manager falls back to alert.
    # Real bookmaker interaction — operator-authorized (see InSessionTransport.attempt_session_extend).
    async def attempt_session_extend(self) -> bool: ...
    # Auto-close Betsson's reality-check reminder by clicking "Cerrar". Returns True
    # iff the popup dismissed; False → manager falls back to alert.
    # Real bookmaker interaction — operator-authorized (see InSessionTransport.attempt_reality_check_close).
    async def attempt_reality_check_close(self) -> bool: ...
    # Leave BetWarrior's promotions page by clicking top-nav "INICIO". Returns True iff
    # the page leaves the promotions route; False → manager falls back to alert.
    # Real bookmaker interaction — operator-authorized (see InSessionTransport.attempt_betwarrior_promotions_home).
    async def attempt_betwarrior_promotions_home(self) -> bool: ...
    # Full logout→login on BetWarrior's own window using keyring creds (a server-killed
    # Kambi session is replaced + its bearer re-captured). True iff a fresh bearer lands.
    # Real bookmaker interaction — operator-authorized (see InSessionTransport.attempt_betwarrior_relogin).
    async def attempt_betwarrior_relogin(self) -> bool: ...
    # Minimal inactivity avoidance (mouse/scroll/occasional click). Best-effort;
    # never raises. See InSessionTransport.keepalive for the operator authorization
    # and the 2026-06-13 lab result on why mouse-alone isn't enough.
    async def keepalive(self) -> None: ...
    # Persist the live session (session-only cookies the profile drops on close) so a
    # later restart can re-inject it. Called by the manager's heartbeat + on graceful
    # shutdown. Fail-soft. See InSessionTransport.save_session.
    async def save_session(self) -> None: ...


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
        # Minimal inactivity avoidance cadence. 90s strikes a balance: frequent enough
        # that BetWarrior's server-side inactivity timer (a few minutes) resets, sparse
        # enough not to look like a bot. Independent from the heartbeat (which is a
        # readiness probe, not user-visible activity).
        keepalive_sec: float = 90.0,
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
        self._suspended_for_cold = False  # we tripped the switch for a cold/session block
        self._readiness: dict[str, bool] = {}  # last probed per-platform readiness
        self._blocks: dict[str, SessionBlock] = {}  # platforms with an overlay/banner block
        # Per-platform popup-action guards. Session extension remains one-shot per
        # episode. Betsson reality-check uses one background task per episode, and that
        # task performs a small bounded retry loop; the guard prevents heartbeat-spawned
        # duplicate tasks while the popup persists. Pruned when the block clears so a
        # future popup episode gets a fresh recovery task.
        self._extend_attempted: set[str] = set()
        self._reality_check_attempted: set[str] = set()
        self._reality_check_tasks: dict[str, asyncio.Task[None]] = {}
        self._promotions_attempted: set[str] = set()
        self._promotions_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_reality_recoveries: dict[str, WarmTransport] = {}
        self._pending_promotions_recoveries: dict[str, WarmTransport] = {}
        self._session_reauth_attempted: set[str] = set()
        self._session_reauth_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_session_reauth: dict[str, WarmTransport] = {}
        self._stopping = False
        self._started_at = 0.0  # monotonic bot start (set in __aenter__), for block uptime
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._keepalive_sec = keepalive_sec
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
        not_ready = await self._probe_readiness()
        await self._apply_health(not_ready)
        self._start_pending_recoveries()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._status_task = asyncio.create_task(self._status_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        await self._notifier.send(f"🟢 Bot started — {self._status_line()}")
        self._log.info(
            "hot_sessions.started",
            heartbeat_sec=self._heartbeat_sec,
            keepalive_sec=self._keepalive_sec,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stopping = True
        for task in (
            self._heartbeat_task,
            self._status_task,
            self._keepalive_task,
            *self._reality_check_tasks.values(),
            *self._promotions_tasks.values(),
            *self._session_reauth_tasks.values(),
        ):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reality_check_tasks.clear()
        self._promotions_tasks.clear()
        self._pending_reality_recoveries.clear()
        self._pending_promotions_recoveries.clear()
        self._session_reauth_tasks.clear()
        self._pending_session_reauth.clear()
        # Last-known-good: persist READY sessions so a graceful restart can
        # re-inject them. Probe first so a session that's cold at shutdown is
        # skipped (keep the last good save). Best-effort — the bot is stopping.
        try:
            not_ready = await self._probe_readiness()
        except Exception as exc:  # noqa: BLE001 — shutdown must not hang on a probe fault
            self._log.warning("hot_sessions.shutdown_probe_error", error=str(exc))
            not_ready = []
        await self._save_sessions(not_ready)
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

    async def _probe_readiness(self) -> list[str]:
        """Probe every wired platform's session-readiness (the API equivalent of
        "is the place button green"): Betsson re-establishes + verifies its betting
        context; Betano hits /api/balance; BetWarrior checks its bearer exp. EACH is
        then also checked for a responsible-gambling LOCKOUT overlay OR a session-expired
        popup (inactivity logout) — the session can be authenticated (probe green) yet
        blocked behind a mandatory-break / killed-session popup, so a platform is
        placeable only if it's ready AND not overlay-blocked. Records the per-platform
        result (for the status report) and returns the list of not-ready platform names
        (overlay-blocked first — that's the actionable alert), empty if all are placeable.

        Returning the FULL list (not just the first not-ready) means concurrent
        failures all surface in the alert, and "ready again" only fires when EVERY
        platform is placeable. Previously returned a single name, which masked
        concurrent failures (one platform's recovery hid another's still-down state)."""

        async def _safe(name: str, coro: Awaitable[bool]) -> bool:
            # A probe that raises (cross-origin fetch, nav fault) means NOT READY —
            # suspend + alert, never crash startup or the heartbeat loop.
            try:
                return await asyncio.wait_for(coro, timeout=_PROBE_TIMEOUT_S)
            except TimeoutError:
                # A HUNG probe (e.g. Betsson networkidle goto wedging — Playwright's
                # own timeout didn't fire) means NOT READY, never a frozen heartbeat.
                self._log.warning("hot_sessions.probe_timeout", platform=name)
                return False
            except Exception as exc:  # noqa: BLE001
                self._log.warning("hot_sessions.probe_error", platform=name, error=str(exc))
                return False

        async def _block(name: str, transport: WarmTransport) -> SessionBlock | None:
            try:
                return await transport.check_session_blocked()
            except Exception as exc:  # noqa: BLE001 — never crash the heartbeat
                self._log.warning("hot_sessions.block_probe_error", platform=name, error=str(exc))
                return None

        async def _try_extend(name: str, transport: WarmTransport) -> bool:
            # Operator-authorized auto-extend (2026-06-19). Logs the outcome; never raises.
            try:
                ok = await transport.attempt_session_extend()
            except Exception as exc:  # noqa: BLE001 — never crash the heartbeat
                self._log.warning("hot_sessions.extend_error", platform=name, error=str(exc))
                return False
            if ok:
                self._log.info("hot_sessions.session_extended", platform=name)
            else:
                self._log.warning("hot_sessions.extend_failed", platform=name)
            return ok

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
            # Auto-extend path: if this is a session_timer_warning overlay, click the
            # platform's "conserve session" button BEFORE deciding to suspend. If the
            # extend succeeds (popup dismissed on re-probe), the platform stays placeable
            # and no alert fires — silent recovery. On failure, fall through to the
            # normal suspend + alert path with the original block. Only attempt for
            # overlay blocks (a banner doesn't block placement, no extend needed).
            if (
                block is not None
                and block.is_overlay
                and block.kind == "session_timer_warning"
                # Only ONE extend attempt per popup episode — the operator authorization
                # is for a single click, not a retry loop. If the click fails, the suspend
                # + alert path fires and the operator handles it. The guard is cleared
                # when the block clears (below), so the next popup episode re-attempts.
                and name not in self._extend_attempted
            ):
                self._extend_attempted.add(name)
                extended = await _try_extend(name, transport)
                if extended:
                    block = await _block(name, transport)  # re-probe; expect None now
            # Betsson reality-check path: record a pending cooldown close, but DO NOT
            # start it inside the probe. _apply_health must trip the kill switch first.
            if (
                block is not None
                and block.is_overlay
                and block.kind == "reality_check"
                and name not in self._reality_check_attempted
                and name not in self._reality_check_tasks
                and not self._stopping
            ):
                self._pending_reality_recoveries[name] = transport
            # BetWarrior promotions route: record a pending INICIO recovery, but start it
            # only after _apply_health has suspended placement for this known-bad route.
            if (
                block is not None
                and block.is_overlay
                and block.kind == "promotions_page"
                and name not in self._promotions_attempted
                and name not in self._promotions_tasks
                and not self._stopping
            ):
                self._pending_promotions_recoveries[name] = transport
            # BetWarrior session_expired (inactivity logout / server-side kill): record a
            # pending re-auth, started after _apply_health suspends placement. The reactive
            # path (Step 4) covers the silent 401 (no popup); this covers the popup case.
            # Only betwarrior has an auto relogin.
            if (
                block is not None
                and block.is_overlay
                and block.kind == "session_expired"
                and name == "betwarrior"
                and name not in self._session_reauth_attempted
                and name not in self._session_reauth_tasks
                and not self._stopping
            ):
                self._pending_session_reauth[name] = transport
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
        # Prune popup-action guards: any platform whose block has cleared is eligible
        # for a fresh click on a future popup episode. Persisting blocks keep their guard
        # set, so failed clicks are not retried every heartbeat.
        self._extend_attempted &= blocks.keys()
        self._reality_check_attempted &= blocks.keys()
        self._promotions_attempted &= blocks.keys()
        self._session_reauth_attempted &= blocks.keys()
        # Return ALL not-ready platform names — overlay-blocked first (actionable:
        # handle the popup), then plain cold. Overlay-blocked platforms are also in
        # `states` with value False, so exclude them from the cold list to avoid dupes.
        overlay_blocked = [p for p, b in blocks.items() if b.is_overlay]
        cold = [p for p, ok in states.items() if not ok and p not in overlay_blocked]
        return overlay_blocked + cold

    def _start_pending_recoveries(self) -> None:
        if self._stopping:
            self._pending_reality_recoveries.clear()
            self._pending_promotions_recoveries.clear()
            self._pending_session_reauth.clear()
            return
        for name, transport in list(self._pending_reality_recoveries.items()):
            self._pending_reality_recoveries.pop(name, None)
            if name in self._reality_check_attempted or name in self._reality_check_tasks:
                continue
            self._reality_check_attempted.add(name)
            self._schedule_reality_check_close(name, transport)
        for name, transport in list(self._pending_promotions_recoveries.items()):
            self._pending_promotions_recoveries.pop(name, None)
            if name in self._promotions_attempted or name in self._promotions_tasks:
                continue
            self._promotions_attempted.add(name)
            self._schedule_promotions_home(name, transport)
        for name, transport in list(self._pending_session_reauth.items()):
            self._pending_session_reauth.pop(name, None)
            if name in self._session_reauth_attempted or name in self._session_reauth_tasks:
                continue
            self._session_reauth_attempted.add(name)
            self._schedule_session_reauth(name, transport)

    def _schedule_reality_check_close(self, name: str, transport: WarmTransport) -> None:
        if name in self._reality_check_tasks:
            return
        self._reality_check_tasks[name] = asyncio.create_task(
            self._close_reality_check_after_cooldown(name, transport)
        )

    async def _close_reality_check_after_cooldown(
        self, name: str, transport: WarmTransport
    ) -> None:
        async def _apply_recovered(event: str, attempt: int) -> None:
            self._log.info(event, platform=name, attempt=attempt)
            if self._stopping:
                return
            try:
                not_ready = await self._probe_readiness()
            except Exception as exc:  # noqa: BLE001 — heartbeat will retry probe
                self._log.warning(
                    "hot_sessions.reality_check_reprobe_error",
                    platform=name,
                    error=str(exc),
                )
                return
            await self._apply_health(not_ready)
            self._start_pending_recoveries()

        async def _reality_still_blocked(attempt: int) -> bool:
            try:
                block = await asyncio.wait_for(
                    transport.check_session_blocked(),
                    timeout=_REALITY_CHECK_RECHECK_TIMEOUT_S,
                )
            except TimeoutError:
                self._log.warning(
                    "hot_sessions.reality_check_recheck_timeout",
                    platform=name,
                    attempt=attempt,
                    timeout_s=_REALITY_CHECK_RECHECK_TIMEOUT_S,
                )
                return True
            except Exception as exc:  # noqa: BLE001 — retry loop must stay fail-soft
                self._log.warning(
                    "hot_sessions.reality_check_recheck_error",
                    platform=name,
                    attempt=attempt,
                    error=str(exc),
                )
                return True
            return block is not None and block.kind == "reality_check" and block.is_overlay

        try:
            for attempt in range(1, _REALITY_CHECK_MAX_CLOSE_ATTEMPTS + 1):
                if not await _reality_still_blocked(attempt):
                    await _apply_recovered(
                        "hot_sessions.reality_check_cleared_before_attempt",
                        attempt,
                    )
                    return
                try:
                    ok = await asyncio.wait_for(
                        transport.attempt_reality_check_close(),
                        timeout=_REALITY_CHECK_CLOSE_ATTEMPT_TIMEOUT_S,
                    )
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    self._log.warning(
                        "hot_sessions.reality_check_close_timeout",
                        platform=name,
                        attempt=attempt,
                        max_attempts=_REALITY_CHECK_MAX_CLOSE_ATTEMPTS,
                        timeout_s=_REALITY_CHECK_CLOSE_ATTEMPT_TIMEOUT_S,
                    )
                    ok = False
                except Exception as exc:  # noqa: BLE001 — background recovery must never die noisily
                    self._log.warning(
                        "hot_sessions.reality_check_close_error",
                        platform=name,
                        attempt=attempt,
                        max_attempts=_REALITY_CHECK_MAX_CLOSE_ATTEMPTS,
                        error=str(exc),
                    )
                    ok = False
                if ok:
                    await _apply_recovered("hot_sessions.reality_check_closed", attempt)
                    return
                if not await _reality_still_blocked(attempt):
                    await _apply_recovered(
                        "hot_sessions.reality_check_cleared_before_retry",
                        attempt,
                    )
                    return
                if attempt < _REALITY_CHECK_MAX_CLOSE_ATTEMPTS:
                    self._log.warning(
                        "hot_sessions.reality_check_close_retrying",
                        platform=name,
                        attempt=attempt,
                        max_attempts=_REALITY_CHECK_MAX_CLOSE_ATTEMPTS,
                        retry_delay_s=_REALITY_CHECK_CLOSE_RETRY_DELAY_S,
                    )
                    await asyncio.sleep(_REALITY_CHECK_CLOSE_RETRY_DELAY_S)
            self._log.warning(
                "hot_sessions.reality_check_close_failed",
                platform=name,
                attempts=_REALITY_CHECK_MAX_CLOSE_ATTEMPTS,
            )
        finally:
            self._reality_check_tasks.pop(name, None)

    def _schedule_session_reauth(self, name: str, transport: WarmTransport) -> None:
        if name in self._session_reauth_tasks:
            return
        self._session_reauth_tasks[name] = asyncio.create_task(
            self._relogin_session(name, transport)
        )

    async def _relogin_session(self, name: str, transport: WarmTransport) -> None:
        try:
            ok = await transport.attempt_betwarrior_relogin()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — background recovery must never die noisily
            self._log.warning("hot_sessions.session_reauth_error", platform=name, error=str(exc))
            return
        finally:
            self._session_reauth_tasks.pop(name, None)
        if not ok:
            # Leave the existing suspend + alert — a failed/challenged re-auth degrades to
            # today's behavior (operator finishes the login manually); never adds exposure.
            self._log.warning("hot_sessions.session_reauth_failed", platform=name)
            return
        self._log.info("hot_sessions.session_reauth_ok", platform=name)
        if self._stopping:
            return
        try:
            not_ready = await self._probe_readiness()
        except Exception as exc:  # noqa: BLE001 — recovery failed; heartbeat will retry probe
            self._log.warning(
                "hot_sessions.session_reauth_reprobe_error", platform=name, error=str(exc)
            )
            return
        await self._apply_health(not_ready)
        self._start_pending_recoveries()

    def _schedule_promotions_home(self, name: str, transport: WarmTransport) -> None:
        if name in self._promotions_tasks:
            return
        self._promotions_tasks[name] = asyncio.create_task(
            self._return_from_promotions(name, transport)
        )

    async def _return_from_promotions(self, name: str, transport: WarmTransport) -> None:
        try:
            ok = await transport.attempt_betwarrior_promotions_home()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — background recovery must never die noisily
            self._log.warning("hot_sessions.promotions_home_error", platform=name, error=str(exc))
            return
        finally:
            self._promotions_tasks.pop(name, None)
        if not ok:
            self._log.warning("hot_sessions.promotions_home_failed", platform=name)
            return
        self._log.info("hot_sessions.promotions_home_recovered", platform=name)
        if self._stopping:
            return
        try:
            not_ready = await self._probe_readiness()
        except Exception as exc:  # noqa: BLE001 — recovery failed; heartbeat will retry probe
            self._log.warning(
                "hot_sessions.promotions_reprobe_error", platform=name, error=str(exc)
            )
            return
        await self._apply_health(not_ready)
        self._start_pending_recoveries()

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
            prev = self._blocks.get(name)
            # Record on a NEW block, OR when a banner ESCALATES to a real overlay — the
            # exact lockout we exist to ground. Betano shows its banner first, so without
            # the escalation case the platform is already in _blocks and the transition to
            # the real overlay would be silently skipped (no capture, no log). Stays
            # once-per-episode: a persistent block (banner or overlay) is not re-recorded.
            escalated = prev is not None and block.is_overlay and not prev.is_overlay
            if prev is not None and not escalated:
                continue
            evidence: str | None = None
            transport = transports.get(name)
            if transport is not None:
                evidence = await transport.capture_block_evidence(f"{name}:{block.phrase}")
            self._log.warning(
                "hot_sessions.rg_block",
                platform=name,
                phrase=block.phrase,
                is_overlay=block.is_overlay,
                escalated=escalated,
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
        return not not_ready

    def _warm_transports(self) -> list[tuple[str, WarmTransport]]:
        ts: list[tuple[str, WarmTransport]] = [
            ("betano", self._betano),
            ("betsson", self._betsson),
        ]
        if self._betwarrior is not None:
            ts.append(("betwarrior", self._betwarrior))
        return ts

    async def _save_sessions(self, not_ready: list[str] | None = None) -> None:
        """Persist every READY live session (session-only cookies the profile drops
        on close) so a later restart can re-inject it. A platform in ``not_ready``
        is SKIPPED — never overwrite a good saved session with a cold one (e.g.
        Betsson issues an anonymous cookie on inactivity logout). Fail-soft per
        platform — a save fault must never block the others or the loop/shutdown."""
        cold = set(not_ready or ())
        for name, t in self._warm_transports():
            if name in cold:
                continue
            try:
                await t.save_session()
            except Exception as exc:  # noqa: BLE001 — never break the loop/shutdown
                self._log.warning("hot_sessions.session_save_error", platform=name, error=str(exc))

    async def _heartbeat_loop(self) -> None:
        """Probe forever. A not-ready session suspends auto-placement + alerts; recovery
        resumes it. The loop never returns on a fault — detection must not stop."""
        while True:
            await asyncio.sleep(self._heartbeat_sec)
            try:
                not_ready = await self._probe_readiness()
            except Exception as exc:  # noqa: BLE001 — a fault suspends, never kills the loop
                self._log.error("hot_sessions.heartbeat_error", error=str(exc))
                not_ready = ["unknown"]
            await self._apply_health(not_ready)
            self._start_pending_recoveries()
            # Persist READY sessions only — a cold session is skipped so we never
            # overwrite the last good save (see _save_sessions).
            await self._save_sessions(not_ready)

    async def _apply_health(self, not_ready: list[str]) -> None:
        """Reconcile session readiness with the kill switch + alerts on transitions
        only (so we don't spam an alert every heartbeat while a session stays cold).
        ``not_ready`` is the list of un-placeable platform names (overlay-blocked
        first); empty iff every platform is placeable. ``not_ready`` non-empty AND
        not currently suspended ⇒ trip + alert naming ALL not-ready platforms.
        ``not_ready`` empty AND currently suspended ⇒ reset + "ready again"."""
        if not_ready and not self._suspended_for_cold:
            self._suspended_for_cold = True
            self._guardrails.trip_kill_switch(_NOT_READY_REASON)
            await self._notifier.send(self._suspend_alert(not_ready))
        elif not not_ready and self._suspended_for_cold:
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

    async def _keepalive_loop(self) -> None:
        """Periodic inactivity avoidance — dispatches ``transport.keepalive()`` on each
        wired platform every ``keepalive_sec``. Faults are logged but never crash the
        loop (one transport's failure must not stop keepalive on the others). The
        heartbeat's readiness probes already register as server-side activity for
        Betano (/api/balance) and Betsson (ctx- nav); this loop covers BetWarrior
        (whose readiness probe is passive) and any other platform that lacks an
        authenticated heartbeat probe."""
        while True:
            await asyncio.sleep(self._keepalive_sec)
            for transport in (self._betano, self._betsson, self._betwarrior):
                if transport is None:
                    continue
                try:
                    await transport.keepalive()
                except Exception as exc:  # noqa: BLE001 — never crash the loop
                    self._log.warning(
                        "hot_sessions.keepalive_error",
                        platform=getattr(transport, "_platform", "?"),
                        error=str(exc),
                    )

    def _suspend_alert(self, not_ready: list[str]) -> str:
        """The operator alert for a suspend transition — per-platform state surfaced
        (RG lockout vs session-timer vs session-expired vs cold) so the operator knows
        what to do per window. Lists ALL not-ready platforms (concurrent failures all
        surface — the first-not-ready masking bug stayed silent on the second)."""

        def _segment(name: str) -> str:
            block = self._blocks.get(name)
            if block is not None and block.is_overlay:
                if block.kind == "session_expired":
                    return f'{name} 🔌 SESSION EXPIRED ("{block.phrase}") — re-login'
                if block.kind == "session_timer_warning":
                    return f'{name} ⏱️ SESSION TIMER ("{block.phrase}") — extend or re-login'
                if block.kind == "reality_check":
                    return f'{name} 🕒 REALITY CHECK ("{block.phrase}") — click Cerrar'
                if block.kind == "promotions_page":
                    return f'{name} 🎁 PROMOTIONS PAGE ("{block.phrase}") — click INICIO'
                return f'{name} 🚫 RG LOCKOUT ("{block.phrase}") — handle the popup'
            return f"{name} (cold)"

        who = ", ".join(_segment(n) for n in not_ready)
        return (
            f"🔌 Sessions NOT READY — auto-placement suspended. Not placeable: {who}. "
            "Resolve each window (re-login or handle the popup); I'll resume automatically. "
            "Detection keeps running."
        )
