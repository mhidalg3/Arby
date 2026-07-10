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
from typing import Any, Protocol

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


# Re-price the arb at fresh reverify odds: given the original legs, the live
# odds the executor just re-fetched, and each leg's live per-bet cap (None when
# a platform has a static cap or the cap couldn't be read), return re-sized legs
# (same order, same platforms) when the edge still clears the floor, or None when
# it's gone. Sync — re-pricing is pure compute (detection + allocation). Injected
# by the bridge (`arb_executor`) so the executor itself never decides
# profitability (that's ``src/arbitrage`` + ``src/risk``).
ReverifyResize = Callable[[list[Leg], list[float], list[float | None]], list[Leg] | None]
# Re-price the NOT-YET-PLACED suffix of an arb around already-filled legs. Given
# the placed fills (their stake_filled/odds_filled are the fixed exposure), the
# remaining legs (always ``legs[i:]`` — original order, contiguous suffix), their
# FRESH odds, and their fresh live caps (None = static/unreadable), return
# re-sized remaining legs (same order, same length, same (platform, outcome)) when
# a hedge locking ≥ the arb layer's floor still exists, else None. Sync pure
# compute; injected by the bridge so the executor never decides profitability.
ResidualReprice = Callable[
    [list["PlacementResult"], list[Leg], list[float], list[float | None]],
    list[Leg] | None,
]

# Fetch a leg's live per-bet stake cap (Betano's dynamic ceiling). Returns None
# for static-cap platforms or when the cap can't be read (fail-soft). Injected so
# the executor never talks to a bookmaker; the cap feeds re-pricing so a dynamic
# leg sizes to the REAL book ceiling, not a stale fallback.
CapRefresh = Callable[[Leg], Awaitable[float | None]]
# Pre-place session-auth-liveness check (per leg). Returns False to ABORT the whole
# arb before any leg is placed — e.g. a BetWarrior (Kambi) bearer the server has
# invalidated (inactivity-logout 401), detected by a real authenticated probe so a
# dead session can't go naked on the earlier legs. Injected so the executor stays
# platform-agnostic; non-fragile platforms return True unconditionally from the wiring.
AuthPrecheck = Callable[[Leg], Awaitable[bool]]
# Drive a full re-auth for this leg's platform (e.g. a BetWarrior logout→login when the
# held Kambi bearer is server-rejected on a placement 401). True ⇒ session refreshed,
# caller may retry the failed leg ONCE. Injected so the executor stays platform-agnostic;
# returns False for platforms without auto re-auth or on a challenge/failure — the
# executor then keeps today's abort/naked, never adding exposure.
ReauthHandler = Callable[[Leg], Awaitable[bool]]


@dataclass(frozen=True)
class PlacementResult:
    accepted: bool
    stake_filled: float = 0.0
    odds_filled: float = 0.0
    ref: str = ""
    detail: str = ""
    # The bet was SUBMITTED but its acceptance could not be confirmed (e.g. a
    # BetWarrior LIVE_DELAY_PENDING that didn't settle within the poll window). It
    # may be placed on the book, so this is NOT a clean reject: the executor routes
    # it to PENDING_UNKNOWN (halt + alert) rather than ABORTED ("nothing placed"),
    # which would hide a live position.
    pending_unknown: bool = False
    # The placement failed a session-auth check (HTTP 401 / no bearer) — eligible for
    # one auto re-auth + retry (see ReauthHandler). Other ≥400 rejects stay False so
    # they keep today's abort/naked behavior; only BetWarrior's auth failures set it.
    auth_failed: bool = False
    # The book rejected the placement because the submitted price no longer matches
    # (BetWarrior/Kambi HTTP 400 "Invalid odds specified" — a pre-acceptance reject,
    # no coupon created, so ONE re-POST is safe). Eligible for a single executor-level
    # recapture (re-fetch fresh odds + arb-layer re-price). All other rejects stay
    # False → today's abort/naked. Betsson's favorable in-placer resubmit is separate.
    odds_rejected: bool = False
    # The price actually POSTed on the LAST attempt when it differs from the odds the
    # executor handed in (Betsson's in-placer favorable resubmit POSTs validOdds).
    # 0.0 = "same as the handed-in price" — the executor logs `odds_requested or current`.
    odds_requested: float = 0.0
    # Server-truth price from a Betsson E_BETTING_ODDS_INVALID correction (accepted-
    # resubmit AND kept-reject paths). None for platforms whose rejects carry no
    # replacement price (BetWarrior/Kambi — documented in placers.py).
    server_valid_odds: float | None = None
    # Verbatim placement-response body (receipt) for audit. Bet-receipt bodies only
    # (couponStatus / coupon echo / receipts); auth lives in request headers, never here.
    raw_response: dict[str, Any] | None = None


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
    # A bet was submitted but its acceptance could not be confirmed — it may be
    # placed on the book. Halts auto-placement (kill switch) + alerts; neither a
    # clean abort ("nothing placed" would hide a live position) nor confirmed naked.
    PENDING_UNKNOWN = "pending_unknown"
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
        cap_refresh: CapRefresh | None = None,
        auth_precheck: AuthPrecheck | None = None,
        reauth: ReauthHandler | None = None,
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
        self._cap_refresh = cap_refresh
        self._auth_precheck = auth_precheck
        self._reauth = reauth
        self._dry_run = dry_run
        self._tag = "[DRY-RUN] " if dry_run else ""
        self._log = log.bind(component="executor", dry_run=dry_run)

    def _placer_for(self, leg: Leg) -> LegPlacer | None:
        return self._placers.get(leg.platform) or self._placer

    async def execute_two_leg(
        self,
        opp_id: str,
        leg_a: Leg,
        leg_b: Leg,
        *,
        revalidate: ReverifyResize | None = None,
        residual: ResidualReprice | None = None,
    ) -> ExecutionResult:
        """Two-leg convenience wrapper over :meth:`execute_n_leg`."""
        return await self.execute_n_leg(
            opp_id, [leg_a, leg_b], revalidate=revalidate, residual=residual
        )

    async def execute_n_leg(
        self,
        opp_id: str,
        legs: list[Leg],
        *,
        revalidate: ReverifyResize | None = None,
        residual: ResidualReprice | None = None,
    ) -> ExecutionResult:
        """Place an N-leg arb (N≥2) sequentially, fail-closed. A one-sided 'arb'
        is never placeable (nothing to hedge against), so <2 legs aborts.

        ``revalidate`` re-prices the arb at the fresh reverify odds (the arb layer
        owns profitability); without it the strict per-leg odds-tolerance gate
        applies (abort on any drift). ``residual`` re-prices the NOT-yet-placed
        suffix around already-filled legs when a mid-sequence odds move (a
        BetWarrior/Kambi invalid-odds reject, or pre-place drift beyond tolerance)
        would otherwise abort or leave the hedge naked."""
        if len(legs) < 2:
            return await self._abort(opp_id, f"need ≥2 legs to hedge, got {len(legs)}")
        try:
            return await self._run(opp_id, legs, revalidate, residual)
        except Exception as exc:  # noqa: BLE001 — any unexpected state escalates, never improvises
            return await self._freeze(opp_id, f"unexpected error: {exc!s}")

    # ---- internals ----

    @staticmethod
    def _label(i: int) -> str:
        return chr(ord("A") + i)  # 0→A, 1→B, 2→C, …

    async def _run(
        self,
        opp_id: str,
        legs: list[Leg],
        revalidate: ReverifyResize | None = None,
        residual: ResidualReprice | None = None,
    ) -> ExecutionResult:
        # Local copy: recapture assigns ``legs[i:] = repriced`` — never mutate the
        # caller's list.
        legs = list(legs)
        if self._guardrails.kill_switch_tripped:
            return await self._abort(opp_id, "kill switch tripped")
        # Snapshot the detector's odds BEFORE revalidate/recapture re-price the legs.
        # Index alignment is stable: both replace legs in place (same length, same order
        # — _repricing_matches enforces it), so detected_odds[i] stays paired with leg i.
        detected_odds = [leg.odds for leg in legs]
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

        # 1) Re-verify every leg's LIVE odds first (nothing placed yet). An
        #    unverifiable leg (re-fetch failed / market gone → 0.0 sentinel) aborts.
        current_odds: list[float] = []
        for i, leg in enumerate(legs):
            c = await self._reverify(leg)
            if c <= 0.0:  # _UNVERIFIED sentinel — re-fetch failed / market gone
                return await self._abort(
                    opp_id,
                    f"leg {self._label(i)} odds unverifiable (re-fetch failed / market gone)",
                )
            current_odds.append(c)

        # 2) Re-price the arb at the fresh odds. The arb layer owns profitability;
        #    `revalidate` re-runs detection + re-sizes, or returns None when the edge
        #    is gone. Without it, keep the strict per-leg tolerance gate (abort on drift).
        if revalidate is not None:
            # Collect each leg's live cap (only when re-pricing will consume it).
            # Betano's dynamic ceiling; None for static-cap platforms or on a read
            # failure (fail-soft → re-pricing falls back to the leg's static cap).
            current_caps: list[float | None]
            if self._cap_refresh is not None:
                current_caps = [await self._cap_refresh(leg) for leg in legs]
            else:
                current_caps = [None] * len(legs)
            repriced = revalidate(legs, current_odds, current_caps)
            if repriced is None:
                return await self._abort(opp_id, "arb no longer profitable at live odds")
            if not self._repricing_matches(legs, repriced):
                # A re-pricing may change odds/stake/cap ONLY — a book/selection
                # identity drift would misroute or misbuild a bet. Fail closed.
                return await self._abort(
                    opp_id, "arb re-validation changed leg book/selection identity"
                )
            legs = repriced
        else:
            for i, leg in enumerate(legs):
                if not self._guardrails.odds_still_acceptable(leg.odds, current_odds[i]):
                    return await self._abort(
                        opp_id,
                        f"leg {self._label(i)} odds drifted {leg.odds}→{current_odds[i]} beyond tolerance",
                    )

        # 3) Guardrail pre-check every (possibly re-sized) leg — abort before placing.
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

        # 4) Pre-place session-auth gate — abort before placing ANY leg if a leg's
        #    session is server-dead. BetWarrior (Kambi) readiness is the only one blind
        #    to a server-side kill (its bearer's JWT exp outlives an inactivity logout),
        #    so the wiring probes its live bearer; a 401/403 here aborts with ZERO
        #    position instead of placing leg A/B then going naked on the dead leg C.
        if self._auth_precheck is not None:
            for i, leg in enumerate(legs):
                if not await self._auth_precheck(leg):
                    return await self._abort(
                        opp_id, f"leg {self._label(i)} session auth not live (pre-place)"
                    )

        # Place sequentially. Re-verify each leg's LIVE odds right before placing it
        # (drift accrues while earlier legs are placed) and place AT the re-verified
        # odds — now against the RE-PRICED odds, so it only aborts/goes naked if the
        # market moves again during the place window. Beyond tolerance (or
        # unverifiable → 0.0) we stop: abort when nothing's live, NAKED EXPOSURE once
        # ≥1 leg is live (the hedge is incomplete). A single bounded recapture per leg
        # (re-fetch fresh odds + arb-layer re-price) salvages an odds-change reject or
        # a pre-place drift that would otherwise abort/naked: the re-priced suffix
        # replaces legs[i:] and the loop re-runs leg i (fresh reverify + place) against
        # the new baseline. Per-leg bound (the recaptured set): a second failure on the
        # same leg falls straight through to abort/naked — no loop is possible.
        placed: list[PlacementResult] = []
        reauthed = False  # one re-auth per execution (any later auth-failed leg reuses it)
        recaptured: set[int] = set()  # one recapture per leg index
        i = 0
        while i < len(legs):
            leg = legs[i]
            placer = placers[i]
            current = await self._reverify(leg)
            if not self._guardrails.odds_still_acceptable(leg.odds, current):
                # Pre-place drift beyond tolerance — one recapture if eligible.
                if i not in recaptured and (
                    residual is not None if placed else revalidate is not None
                ):
                    repriced = await self._recapture(
                        opp_id, legs, i, placed, revalidate, residual, fresh_i=current
                    )
                    if repriced is not None:
                        legs[i:] = repriced
                        recaptured.add(i)
                        continue  # re-run leg i against the re-priced baseline
                reason = f"leg {self._label(i)} odds drifted {leg.odds}→{current} beyond tolerance"
                if placed:  # earlier legs already live → unhedged
                    return await self._naked(opp_id, reason, placed)
                return await self._abort(opp_id, reason)
            res = await placer.place(replace(leg, odds=current))  # place AT the re-verified odds
            attempted_odds = current  # the price actually POSTed (pre-reauth)
            # One bounded re-auth: an auth_failed leg (BetWarrior 401/no-bearer, Betsson
            # 401) is eligible for a single platform logout→login + retry. Re-auth takes
            # time, so re-verify odds and re-check tolerance before the retry — a
            # challenged/failed re-auth or post-re-auth drift falls through to the
            # unchanged abort/naked block below, so the rescue NEVER adds exposure.
            # Per-execution bound: ONE re-auth total (the reauthed flag) — a later auth-
            # failed leg on the same platform reuses the now-fresh session, no 2nd re-
            # auth; if it still fails, one clean abort/naked follows (no loop).
            if not res.accepted and res.auth_failed and self._reauth is not None and not reauthed:
                reauthed = True
                await self._notifier.send(
                    f"{self._tag}arb {opp_id}: leg {self._label(i)} auth-failed "
                    f"— re-authenticating {leg.platform}…"
                )
                if await self._reauth(leg):
                    current = await self._reverify(leg)
                    if self._guardrails.odds_still_acceptable(leg.odds, current):
                        res = await placer.place(replace(leg, odds=current))
                        attempted_odds = current  # the retry POSTed at this price
            # Structured per-leg placement telemetry (one event per attempt, accepted
            # AND rejected): the full odds ladder for triage. detected↔preflight = feed
            # staleness at detection; preflight↔preplace = drift while earlier legs
            # placed; preplace/odds_requested↔odds_filled/server_valid_odds = race
            # during placement (Betsson's corrected price when it supplied one).
            self._log.info(
                "executor.leg_placement",
                opp_id=opp_id,
                leg=self._label(i),
                platform=leg.platform,
                outcome=leg.outcome,
                detected_odds=detected_odds[i],
                preflight_odds=current_odds[i],
                arb_odds=leg.odds,
                preplace_odds=attempted_odds,
                odds_requested=res.odds_requested or attempted_odds,
                server_valid_odds=res.server_valid_odds,
                stake_requested=leg.stake_ars,
                accepted=res.accepted,
                odds_filled=res.odds_filled or None,
                stake_filled=res.stake_filled or None,
                ref=res.ref or None,
                detail=None if res.accepted else res.detail,
            )
            if not res.accepted:
                if res.pending_unknown:
                    # Bet submitted but unconfirmed — may be placed. NOT a clean reject:
                    # if earlier legs are live this is naked (confirmed + unconfirmed);
                    # otherwise it's an unconfirmed position that must halt + alert.
                    reason = f"leg {self._label(i)} unconfirmed (may be placed): {res.detail}"
                    if placed:
                        return await self._naked(opp_id, reason, placed)
                    return await self._pending_unknown(opp_id, reason)
                # Placer odds-reject (BetWarrior/Kambi "Invalid odds specified"): the book
                # moved after our reverify, so the flag proves our in-hand price is stale —
                # re-fetch leg i too (fresh_i=None). One recapture if eligible; a submitted
                # (pending_unknown) bet is NEVER re-POSTed (handled above).
                if (
                    res.odds_rejected
                    and i not in recaptured
                    and (residual is not None if placed else revalidate is not None)
                ):
                    repriced = await self._recapture(
                        opp_id, legs, i, placed, revalidate, residual, fresh_i=None
                    )
                    if repriced is not None:
                        legs[i:] = repriced
                        recaptured.add(i)
                        continue  # re-run leg i against the re-priced baseline
                reason = f"leg {self._label(i)} rejected: {res.detail}"
                if placed:  # earlier legs already live → unhedged
                    return await self._naked(opp_id, reason, placed)
                return await self._abort(opp_id, reason)
            self._guardrails.record_exposure(leg.match_id, res.stake_filled)
            placed.append(res)
            await self._notifier.send(self._format_placed(opp_id, self._label(i), leg, res))
            i += 1

        await self._notifier.send(self._format_complete(opp_id, legs, placed))
        self._log.info("executor.completed", opp_id=opp_id, legs=len(placed))
        return ExecutionResult(ExecutionOutcome.COMPLETED, legs=tuple(placed))

    @staticmethod
    def _repricing_matches(expected: list[Leg], got: list[Leg]) -> bool:
        """True iff ``got`` preserves each leg's identity and length.

        A re-pricing may change ONLY odds/stake/cap. The placer list was resolved
        against the original leg order (keyed on ``platform``), and each request is
        BUILT from ``platform_outcome_id`` / ``platform_event_ref`` (Kambi outcome id,
        Betsson marketSelectionId, Bplay url_key) — so any drift in platform,
        match/market, outcome, or the selection/event refs would route or build a bet
        for the WRONG book/selection. The executor fails closed (abort, or recapture
        returns None → abort/naked) on any mismatch rather than risk a misplacement."""
        if len(got) != len(expected):
            return False
        for g, e in zip(got, expected, strict=True):
            if (
                g.platform,
                g.match_id,
                g.market,
                g.outcome,
                g.platform_outcome_id,
                g.platform_event_ref,
            ) != (
                e.platform,
                e.match_id,
                e.market,
                e.outcome,
                e.platform_outcome_id,
                e.platform_event_ref,
            ):
                return False
        return True

    async def _recapture(
        self,
        opp_id: str,
        legs: list[Leg],
        i: int,
        placed: list[PlacementResult],
        revalidate: ReverifyResize | None,
        residual: ResidualReprice | None,
        *,
        fresh_i: float | None,
    ) -> list[Leg] | None:
        """One bounded attempt to re-price the remaining suffix (``legs[i:]``) at
        FRESH odds and salvage a hedge an abort/naked would discard.

        ``fresh_i`` is the already-reverified odds for leg i when the trigger was a
        pre-place drift (refetch is wasted); None when the trigger was a placer
        odds-reject (the flag proves our in-hand price is stale — refetch leg i too).
        Returns the re-priced suffix (same length, same ``(platform, outcome)`` order)
        when a hedge still locks ≥ the arb floor, else None (caller aborts/goes naked).
        Never adds exposure: nothing is placed here."""
        remaining = legs[i:]
        fresh: list[float] = []
        for j, leg in enumerate(remaining):
            o = fresh_i if (j == 0 and fresh_i is not None) else await self._reverify(leg)
            if o <= 0.0:  # unverifiable — re-fetch failed / market gone
                self._log.info(
                    "executor.recapture_unverifiable", opp_id=opp_id, leg=self._label(i + j)
                )
                return None
            fresh.append(o)
        if self._cap_refresh is not None:
            caps = [await self._cap_refresh(leg) for leg in remaining]
        else:
            caps = [None] * len(remaining)
        # placed empty → recapture at leg A is a full re-price (remaining == all legs);
        # placed non-empty → hedge the suffix around the already-filled legs.
        if not placed:
            if revalidate is None:
                return None
            repriced = revalidate(remaining, fresh, caps)
        else:
            if residual is None:
                return None
            repriced = residual(list(placed), remaining, fresh, caps)
        if repriced is None:
            self._log.info("executor.recapture_no_arb", opp_id=opp_id, leg=self._label(i))
            return None
        if not self._repricing_matches(remaining, repriced):
            self._log.error("executor.recapture_shape_mismatch", opp_id=opp_id, leg=self._label(i))
            return None
        # Validate the re-priced suffix CUMULATIVELY against the caps: each leg is
        # checked against the exposure the PRIOR suffix legs would add (they place
        # before it), so a multi-leg re-price can't slip B+C past the per-match /
        # total caps on individual checks alone. Tentatively record + release so
        # check_leg sees the running totals; nothing commits unless the whole suffix
        # clears (the place loop records for real afterward).
        tentative: list[tuple[str, float]] = []
        try:
            for j, leg in enumerate(repriced):
                check = self._guardrails.check_leg(
                    platform=leg.platform,
                    match_id=leg.match_id,
                    stake_ars=leg.stake_ars,
                    decimal_odds=leg.odds,
                    live_max_stake_ars=leg.live_max_stake_ars,
                )
                if not check.allowed:
                    self._log.info(
                        "executor.recapture_guardrail",
                        opp_id=opp_id,
                        leg=self._label(i + j),
                        reason=check.reason,
                    )
                    return None
                self._guardrails.record_exposure(leg.match_id, leg.stake_ars)
                tentative.append((leg.match_id, leg.stake_ars))
        finally:
            for match_id, stake in tentative:
                self._guardrails.release_exposure(match_id, stake)
        self._log.info(
            "executor.recaptured",
            opp_id=opp_id,
            leg=self._label(i),
            old_odds=[legs[i + j].odds for j in range(len(remaining))],
            new_odds=[r.odds for r in repriced],
            new_stakes=[r.stake_ars for r in repriced],
        )
        await self._notifier.send(self._format_recaptured(opp_id, self._label(i), repriced))
        return repriced

    def _format_recaptured(self, opp_id: str, label: str, repriced: list[Leg]) -> str:
        """Operator alert naming the re-priced legs (mirrors _format_placed tone)."""
        lines = [f"{self._tag}♻️ arb {opp_id}: leg {label} RE-PRICED (odds-change recapture)"]
        for leg in repriced:
            lines.append(f"   {leg.platform} {leg.outcome} {leg.stake_ars:.0f}@{leg.odds}")
        return "\n".join(lines)

    def _format_complete(self, opp_id: str, legs: list[Leg], placed: list[PlacementResult]) -> str:
        lines = [
            f"{self._tag}✅ arb {opp_id}: COMPLETE — {len(placed)} legs filled (hedge secured)"
        ]
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
        return ExecutionResult(ExecutionOutcome.NAKED_EXPOSURE, reason=reason, legs=tuple(placed))

    async def _pending_unknown(self, opp_id: str, reason: str) -> ExecutionResult:
        """A bet was submitted but its acceptance could not be confirmed — it may be
        placed on the book. Trip the kill switch (halt auto-placement so the bot
        can't compound an unconfirmed position) and alert the operator to verify
        the coupon on the book NOW. Neither a clean abort (which falsely claims
        "nothing placed") nor a confirmed naked (we don't know if anything's live)."""
        self._log.error("executor.pending_unknown", opp_id=opp_id, reason=reason)
        self._guardrails.trip_kill_switch(f"pending unknown: {reason}")
        await self._notifier.send(
            f"⚠️ {self._tag}arb {opp_id}: PENDING UNKNOWN — a bet was submitted but its "
            f"acceptance could not be confirmed: {reason}. VERIFY the book now — the bet "
            f"may be placed. Auto-placement halted (kill switch) until you confirm + reset."
        )
        return ExecutionResult(ExecutionOutcome.PENDING_UNKNOWN, reason=reason)

    async def _freeze(self, opp_id: str, reason: str) -> ExecutionResult:
        self._log.error("executor.escalating", opp_id=opp_id, reason=reason)
        outcome = await self._recovery.recover(reason)
        if outcome is RecoveryOutcome.RESOLVED:
            # Recovery fixed it; caller may retry. We do not auto-retry here.
            return ExecutionResult(ExecutionOutcome.ABORTED, reason=f"recovered: {reason}")
        self._guardrails.trip_kill_switch(f"frozen: {reason}")
        await self._notifier.send(f"🧊 {self._tag}arb {opp_id}: FROZEN — recovery failed: {reason}")
        return ExecutionResult(ExecutionOutcome.FROZEN, reason=reason)
