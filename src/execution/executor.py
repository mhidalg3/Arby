"""N-leg arbitrage execution — deterministic state machine (dry-run capable).

Consumes a sized opportunity (N≥2 legs — a two-outcome O/U or a three-outcome
1X2 alike) and places the legs under the guardrails, re-verifying odds before
each placement. Execution NEVER decides profitability (that's ``src/risk/``); it
sequences placement and contains the damage when reality diverges. See
``docs/architecture.md`` Layer 4.

State machine (fail-closed, naked-exposure-aware):

1. Resolve a placer for EVERY leg; kill-switch + per-leg guardrail pre-checks +
   re-verify every leg's odds within tolerance. Any failure → **abort before
   placing anything** (nothing at risk).
2. Place legs sequentially. Re-verify each leg's odds again right before placing
   it (drift accrues while earlier legs are placed). The FIRST leg's rejection →
   abort (nothing placed). Once ≥1 leg is live, any subsequent drift/rejection →
   **naked exposure** (some legs live, hedge incomplete): log + alert with the
   live-leg count, do not unwind automatically — the operator hedges manually.
3. All filled → complete.

Any unexpected error escalates to the `RecoveryHandler`; if unresolved, the
run **freezes** (halt + alert). Placement itself goes through a `LegPlacer`:
`DryRunPlacer` builds the intent without sending; a real per-platform API
placer slots in once the placement contract is captured.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
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
    NAKED_EXPOSURE = "naked_exposure"  # ≥1 leg live, hedge incomplete — needs attention
    FROZEN = "frozen"  # unexpected state, recovery failed — halt


@dataclass(frozen=True)
class ExecutionResult:
    outcome: ExecutionOutcome
    reason: str = ""
    legs: tuple[PlacementResult, ...] = ()  # filled legs, in placement order

    @property
    def leg_a(self) -> PlacementResult | None:
        """First filled leg (back-compat convenience for two-leg consumers)."""
        return self.legs[0] if self.legs else None

    @property
    def leg_b(self) -> PlacementResult | None:
        """Second filled leg (back-compat convenience for two-leg consumers)."""
        return self.legs[1] if len(self.legs) > 1 else None


async def _no_reverify(leg: Leg) -> float:
    """Default re-verify: trust the leg's stated odds (no live re-fetch)."""
    return leg.odds


class Executor:
    """Deterministic N-leg execution under guardrails (N≥2: two-outcome markets
    like O/U and three-outcome 1X2 alike)."""

    def __init__(
        self,
        *,
        guardrails: Guardrails,
        notifier: Notifier,
        recovery: RecoveryHandler,
        placer: LegPlacer | None = None,
        placers: Mapping[str, LegPlacer] | None = None,
        reverify: Callable[[Leg], Awaitable[float]] = _no_reverify,
        dry_run: bool = True,
    ) -> None:
        # A cross-platform arb routes each leg to its platform's placer (`placers`
        # keyed by leg.platform); `placer` is the single-placer fallback (used for
        # both legs when no per-platform mapping matches).
        if placer is None and not placers:
            raise ValueError("Executor needs placer= or placers=")
        self._guardrails = guardrails
        self._notifier = notifier
        self._recovery = recovery
        self._placer = placer
        self._placers = dict(placers or {})
        self._reverify = reverify
        self._dry_run = dry_run
        self._tag = "[DRY-RUN] " if dry_run else ""
        self._log = log.bind(component="executor", dry_run=dry_run)

    def _placer_for(self, leg: Leg) -> LegPlacer | None:
        return self._placers.get(leg.platform) or self._placer

    async def execute_two_leg(self, opp_id: str, leg_a: Leg, leg_b: Leg) -> ExecutionResult:
        """Two-leg convenience wrapper over :meth:`execute_n_leg`."""
        return await self.execute_n_leg(opp_id, [leg_a, leg_b])

    async def execute_n_leg(self, opp_id: str, legs: list[Leg]) -> ExecutionResult:
        """Place an N-leg arb (N≥2) sequentially, fail-closed. A one-sided 'arb'
        is never placeable (nothing to hedge against), so <2 legs aborts."""
        if len(legs) < 2:
            return await self._abort(opp_id, f"need ≥2 legs to hedge, got {len(legs)}")
        try:
            return await self._run(opp_id, legs)
        except Exception as exc:  # noqa: BLE001 — any unexpected state escalates, never improvises
            return await self._freeze(opp_id, f"unexpected error: {exc!s}")

    # ---- internals ----

    @staticmethod
    def _label(i: int) -> str:
        return chr(ord("A") + i)  # 0→A, 1→B, 2→C, …

    async def _run(self, opp_id: str, legs: list[Leg]) -> ExecutionResult:
        if self._guardrails.kill_switch_tripped:
            return await self._abort(opp_id, "kill switch tripped")

        # Resolve a placer for EVERY leg up front — abort before placing anything if
        # any platform is unwired (never place one leg of an arb we can't complete).
        placers: list[LegPlacer] = []
        for i, leg in enumerate(legs):
            p = self._placer_for(leg)
            if p is None:
                return await self._abort(
                    opp_id, f"no placer for leg {self._label(i)} platform {leg.platform!r}"
                )
            placers.append(p)

        # 1) Pre-check every leg (guardrails) and re-verify every leg's odds — nothing
        #    placed yet, so any failure aborts with zero at risk.
        for i, leg in enumerate(legs):
            check = self._guardrails.check_leg(
                platform=leg.platform,
                match_id=leg.match_id,
                stake_ars=leg.stake_ars,
                decimal_odds=leg.odds,
                live_max_stake_ars=leg.live_max_stake_ars,
            )
            if not check.allowed:
                return await self._abort(opp_id, f"leg {self._label(i)} guardrail: {check.reason}")
            current = await self._reverify(leg)
            if not self._guardrails.odds_still_acceptable(leg.odds, current):
                return await self._abort(
                    opp_id,
                    f"leg {self._label(i)} odds drifted {leg.odds}→{current} beyond tolerance",
                )

        # 2) Place sequentially. Re-verify each leg's LIVE odds right before placing it
        #    (drift accrues while earlier legs are placed) and place AT the re-verified
        #    odds — within tolerance the arb still holds; beyond it (or unverifiable →
        #    0.0) we stop. Before any leg is live that's an abort; once ≥1 leg is live,
        #    it's NAKED EXPOSURE (the hedge is incomplete).
        placed: list[PlacementResult] = []
        for i, (leg, placer) in enumerate(zip(legs, placers, strict=True)):
            current = await self._reverify(leg)
            if not self._guardrails.odds_still_acceptable(leg.odds, current):
                reason = f"leg {self._label(i)} odds drifted {leg.odds}→{current} beyond tolerance"
                if placed:  # earlier legs already live → unhedged
                    return await self._naked(opp_id, reason, placed)
                return await self._abort(opp_id, reason)
            res = await placer.place(replace(leg, odds=current))  # place AT the re-verified odds
            if not res.accepted:
                reason = f"leg {self._label(i)} rejected: {res.detail}"
                if placed:  # earlier legs already live → unhedged
                    return await self._naked(opp_id, reason, placed)
                return await self._abort(opp_id, reason)
            self._guardrails.record_exposure(leg.match_id, res.stake_filled)
            placed.append(res)
            await self._notifier.send(self._format_placed(opp_id, self._label(i), leg, res))

        await self._notifier.send(self._format_complete(opp_id, legs, placed))
        self._log.info("executor.completed", opp_id=opp_id, legs=len(placed))
        return ExecutionResult(ExecutionOutcome.COMPLETED, legs=tuple(placed))

    def _format_complete(
        self, opp_id: str, legs: list[Leg], placed: list[PlacementResult]
    ) -> str:
        lines = [f"{self._tag}✅ arb {opp_id}: COMPLETE — {len(placed)} legs filled (hedge secured)"]
        for i, (leg, res) in enumerate(zip(legs, placed, strict=True)):
            lines.append(
                f"   Leg {self._label(i)}: {leg.platform} {leg.outcome} "
                f"{res.stake_filled:.0f}@{res.odds_filled}"
            )
        return "\n".join(lines)

    def _format_placed(self, opp_id: str, label: str, leg: Leg, res: PlacementResult) -> str:
        """Operator alert for a placed bet: which leg, on what platform/event, the
        exact selection + stake + odds filled, and the platform's bet reference."""
        odds = res.odds_filled or leg.odds
        stake = res.stake_filled or leg.stake_ars
        return (
            f"{self._tag}✅ BET PLACED — Leg {label} of arb {opp_id}\n"
            f"   platform: {leg.platform}\n"
            f"   event: {leg.platform_event_ref or leg.match_id}\n"
            f"   market: {leg.market}\n"
            f"   bet: {leg.outcome} @ {odds} for {stake:.0f} ARS\n"
            f"   ref: {res.ref or '—'}"
        )

    async def _abort(self, opp_id: str, reason: str) -> ExecutionResult:
        self._log.info("executor.aborted", opp_id=opp_id, reason=reason)
        await self._notifier.send(f"{self._tag}arb {opp_id}: ABORTED (nothing placed) — {reason}")
        return ExecutionResult(ExecutionOutcome.ABORTED, reason=reason)

    async def _naked(
        self, opp_id: str, reason: str, placed: list[PlacementResult]
    ) -> ExecutionResult:
        self._log.error("executor.naked_exposure", opp_id=opp_id, reason=reason, live=len(placed))
        await self._notifier.send(
            f"🚨 {self._tag}arb {opp_id}: NAKED EXPOSURE — {len(placed)} leg(s) LIVE, hedge "
            f"incomplete: {reason}. Manual action needed (close/hedge the open position)."
        )
        return ExecutionResult(
            ExecutionOutcome.NAKED_EXPOSURE, reason=reason, legs=tuple(placed)
        )

    async def _freeze(self, opp_id: str, reason: str) -> ExecutionResult:
        self._log.error("executor.escalating", opp_id=opp_id, reason=reason)
        outcome = await self._recovery.recover(reason)
        if outcome is RecoveryOutcome.RESOLVED:
            # Recovery fixed it; caller may retry. We do not auto-retry here.
            return ExecutionResult(ExecutionOutcome.ABORTED, reason=f"recovered: {reason}")
        self._guardrails.trip_kill_switch(f"frozen: {reason}")
        await self._notifier.send(f"🧊 {self._tag}arb {opp_id}: FROZEN — recovery failed: {reason}")
        return ExecutionResult(ExecutionOutcome.FROZEN, reason=reason)
