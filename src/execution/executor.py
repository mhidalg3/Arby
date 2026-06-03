"""Two-leg arbitrage execution — deterministic state machine (dry-run capable).

Consumes a sized opportunity (two legs) and places them under the guardrails,
re-verifying odds before each placement. Execution NEVER decides profitability
(that's ``src/risk/``); it sequences placement and contains the damage when
reality diverges. See ``docs/architecture.md`` Layer 4.

State machine (fail-closed, naked-exposure-aware):

1. Kill-switch + per-leg guardrail pre-checks on BOTH legs, and re-verify both
   legs' odds within tolerance. Any failure → **abort before placing anything**.
2. Place Leg A. Rejected → abort (nothing at risk).
3. Re-verify Leg B's odds *again* (it may have drifted while Leg A was placed).
   Dropped beyond tolerance, or Leg B rejected → **naked exposure** (Leg A is
   live, Leg B isn't): log + alert, do not unwind automatically.
4. Both filled → complete.

Any unexpected error escalates to the `RecoveryHandler`; if unresolved, the
run **freezes** (halt + alert). Placement itself goes through a `LegPlacer`:
`DryRunPlacer` builds the intent without sending; a real per-platform API
placer slots in once the placement contract is captured.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import structlog

from src.execution.guardrails import Guardrails
from src.execution.notify import Notifier
from src.execution.recovery import RecoveryHandler, RecoveryOutcome

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Leg:
    """One side of an arb: a sized bet on a specific outcome."""

    platform: str
    match_id: str
    market: str
    outcome: str
    stake_ars: float
    odds: float  # the decimal odds we intend to bet at
    platform_outcome_id: str = ""  # the platform's selection ref, for placement
    platform_event_ref: str = ""  # event nav ref (Betsson slug / Bplay url_key)
    live_max_stake_ars: float | None = None  # live cap for dynamic platforms (Betano)


@dataclass(frozen=True)
class PlacementResult:
    accepted: bool
    stake_filled: float = 0.0
    odds_filled: float = 0.0
    ref: str = ""
    detail: str = ""


class LegPlacer(Protocol):
    async def place(self, leg: Leg) -> PlacementResult:
        """Place one leg. Returns the fill. May raise on transport/session
        failure (the executor escalates to recovery)."""
        ...


class DryRunPlacer:
    """Builds the placement intent and 'fills' at the requested stake/odds
    without sending anything. The safe default until the real API placer lands."""

    async def place(self, leg: Leg) -> PlacementResult:
        log.info(
            "executor.dry_run_place",
            platform=leg.platform,
            match_id=leg.match_id,
            outcome=leg.outcome,
            stake_ars=leg.stake_ars,
            odds=leg.odds,
        )
        return PlacementResult(
            accepted=True,
            stake_filled=leg.stake_ars,
            odds_filled=leg.odds,
            ref="dry-run",
            detail="dry-run: no request sent",
        )


class ExecutionOutcome(StrEnum):
    COMPLETED = "completed"
    ABORTED = "aborted"  # before any leg placed — nothing at risk
    NAKED_EXPOSURE = "naked_exposure"  # Leg A live, Leg B not — needs attention
    FROZEN = "frozen"  # unexpected state, recovery failed — halt


@dataclass(frozen=True)
class ExecutionResult:
    outcome: ExecutionOutcome
    reason: str = ""
    leg_a: PlacementResult | None = None
    leg_b: PlacementResult | None = None


async def _no_reverify(leg: Leg) -> float:
    """Default re-verify: trust the leg's stated odds (no live re-fetch)."""
    return leg.odds


class Executor:
    """Deterministic two-leg execution under guardrails."""

    def __init__(
        self,
        *,
        guardrails: Guardrails,
        notifier: Notifier,
        recovery: RecoveryHandler,
        placer: LegPlacer,
        reverify: Callable[[Leg], Awaitable[float]] = _no_reverify,
        dry_run: bool = True,
    ) -> None:
        self._guardrails = guardrails
        self._notifier = notifier
        self._recovery = recovery
        self._placer = placer
        self._reverify = reverify
        self._dry_run = dry_run
        self._tag = "[DRY-RUN] " if dry_run else ""
        self._log = log.bind(component="executor", dry_run=dry_run)

    async def execute_two_leg(self, opp_id: str, leg_a: Leg, leg_b: Leg) -> ExecutionResult:
        try:
            return await self._run(opp_id, leg_a, leg_b)
        except Exception as exc:  # noqa: BLE001 — any unexpected state escalates, never improvises
            return await self._freeze(opp_id, f"unexpected error: {exc!s}")

    # ---- internals ----

    async def _run(self, opp_id: str, leg_a: Leg, leg_b: Leg) -> ExecutionResult:
        if self._guardrails.kill_switch_tripped:
            return await self._abort(opp_id, "kill switch tripped")

        # 1) Pre-check both legs (guardrails) and re-verify both odds — nothing placed yet.
        for label, leg in (("A", leg_a), ("B", leg_b)):
            check = self._guardrails.check_leg(
                platform=leg.platform,
                match_id=leg.match_id,
                stake_ars=leg.stake_ars,
                decimal_odds=leg.odds,
                live_max_stake_ars=leg.live_max_stake_ars,
            )
            if not check.allowed:
                return await self._abort(opp_id, f"leg {label} guardrail: {check.reason}")
            current = await self._reverify(leg)
            if not self._guardrails.odds_still_acceptable(leg.odds, current):
                return await self._abort(
                    opp_id, f"leg {label} odds drifted {leg.odds}→{current} beyond tolerance"
                )

        # 2) Place Leg A.
        await self._notifier.send(
            f"{self._tag}arb {opp_id}: placing Leg A ({leg_a.platform} {leg_a.outcome})"
        )
        res_a = await self._placer.place(leg_a)
        if not res_a.accepted:
            return await self._abort(opp_id, f"Leg A rejected: {res_a.detail}", leg_a=res_a)
        self._guardrails.record_exposure(leg_a.match_id, res_a.stake_filled)
        await self._notifier.send(f"{self._tag}arb {opp_id}: Leg A filled @ {res_a.odds_filled}")

        # 3) Re-verify Leg B before committing the second leg (the naked-exposure guard).
        current_b = await self._reverify(leg_b)
        if not self._guardrails.odds_still_acceptable(leg_b.odds, current_b):
            return await self._naked(opp_id, f"Leg B odds drifted {leg_b.odds}→{current_b}", res_a)
        res_b = await self._placer.place(leg_b)
        if not res_b.accepted:
            return await self._naked(opp_id, f"Leg B rejected: {res_b.detail}", res_a)
        self._guardrails.record_exposure(leg_b.match_id, res_b.stake_filled)

        await self._notifier.send(
            f"{self._tag}arb {opp_id}: COMPLETE — Leg A {res_a.stake_filled}@{res_a.odds_filled}, "
            f"Leg B {res_b.stake_filled}@{res_b.odds_filled}"
        )
        self._log.info("executor.completed", opp_id=opp_id)
        return ExecutionResult(ExecutionOutcome.COMPLETED, leg_a=res_a, leg_b=res_b)

    async def _abort(
        self, opp_id: str, reason: str, leg_a: PlacementResult | None = None
    ) -> ExecutionResult:
        self._log.info("executor.aborted", opp_id=opp_id, reason=reason)
        await self._notifier.send(f"{self._tag}arb {opp_id}: ABORTED (nothing placed) — {reason}")
        return ExecutionResult(ExecutionOutcome.ABORTED, reason=reason, leg_a=leg_a)

    async def _naked(self, opp_id: str, reason: str, leg_a: PlacementResult) -> ExecutionResult:
        self._log.error("executor.naked_exposure", opp_id=opp_id, reason=reason)
        await self._notifier.send(
            f"🚨 {self._tag}arb {opp_id}: NAKED EXPOSURE — Leg A is live, Leg B failed: {reason}"
        )
        return ExecutionResult(ExecutionOutcome.NAKED_EXPOSURE, reason=reason, leg_a=leg_a)

    async def _freeze(self, opp_id: str, reason: str) -> ExecutionResult:
        self._log.error("executor.escalating", opp_id=opp_id, reason=reason)
        outcome = await self._recovery.recover(reason)
        if outcome is RecoveryOutcome.RESOLVED:
            # Recovery fixed it; caller may retry. We do not auto-retry here.
            return ExecutionResult(ExecutionOutcome.ABORTED, reason=f"recovered: {reason}")
        self._guardrails.trip_kill_switch(f"frozen: {reason}")
        await self._notifier.send(f"🧊 {self._tag}arb {opp_id}: FROZEN — recovery failed: {reason}")
        return ExecutionResult(ExecutionOutcome.FROZEN, reason=reason)
