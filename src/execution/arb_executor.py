"""Bridge: a detected (and risk-approved) arbitrage opportunity → the Executor.

The detector (`src/arbitrage`) emits an `ArbitrageOpportunity` carrying the legs
(`OddsQuote`s) and their sized `stakes`; the risk layer (`src/risk`) approves it.
This module maps each quote to an execution `Leg` and drives
`Executor.execute_n_leg` (N≥2 — a two-outcome O/U or a three-outcome 1X2 alike).
It does NOT decide profitability or sizing — that's already done; it only
translates and sequences.

Field mapping (`OddsQuote` → `Leg`):
- ``platform``             → ``platform``       (routing key for the placers map)
- ``market_id``            → ``market`` and the canonical ``match_id`` (shared by
                             both legs, so per-match exposure counts them together)
- ``outcome``              → ``outcome``
- ``decimal_odds``         → ``odds``
- (opportunity ``stakes``) → ``stake_ars``
- ``platform_outcome_id``  → ``platform_outcome_id``  (the selection ref)
- ``platform_event_id``    → ``platform_event_ref``   (Betano's eventId; Betsson ignores it)
- ``max_stake``            → ``live_max_stake_ars``    (resolves the dynamic-cap guard)
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import replace

import structlog

from src.arbitrage.dutch_book import ArbitrageOpportunity, detect_arbitrage
from src.arbitrage.quotes import OddsQuote
from src.arbitrage.stake_allocator import allocate_residual
from src.execution.executor import ExecutionResult, Executor, Leg, PlacementResult
from src.risk.stake_limits import is_dynamic

log = structlog.get_logger(__name__)


def leg_from_quote(
    quote: OddsQuote, stake_ars: float, *, match_id: str, dynamic_stake_cap_ars: float | None = None
) -> Leg:
    """Translate one sized `OddsQuote` into an execution `Leg`.

    `dynamic_stake_cap_ars` is a conservative fallback cap for dynamic-limit
    platforms (Betano) whose public feed carries no `max_stake`: without a live
    cap the guardrail fail-closes, so a configured conservative cap lets the leg
    place (under-stake-safe) until the live limits query is wired."""
    cap = quote.max_stake
    if cap is None and dynamic_stake_cap_ars is not None and is_dynamic(quote.platform):
        cap = dynamic_stake_cap_ars
    return Leg(
        platform=quote.platform,
        match_id=match_id,
        market=quote.market_id,
        outcome=quote.outcome,
        stake_ars=stake_ars,
        odds=quote.decimal_odds,
        platform_outcome_id=quote.platform_outcome_id or "",
        platform_event_ref=quote.platform_event_id or "",
        live_max_stake_ars=cap,
    )


def legs_from_opportunity(
    opp: ArbitrageOpportunity, *, dynamic_stake_cap_ars: float | None = None
) -> list[Leg]:
    """Map an opportunity's quotes+stakes to legs, sharing a canonical match_id
    (the common ``market_id``) so the guardrails track both against one match."""
    if len(opp.legs) != len(opp.stakes):
        raise ValueError(f"legs/stakes length mismatch: {len(opp.legs)} vs {len(opp.stakes)}")
    match_id = opp.legs[0].market_id if opp.legs else ""
    return [
        leg_from_quote(q, s, match_id=match_id, dynamic_stake_cap_ars=dynamic_stake_cap_ars)
        for q, s in zip(opp.legs, opp.stakes, strict=True)
    ]


# Platforms whose session/auth can hard-fail at placement time (LEDGER: both live
# naked incidents were accepted Betsson legs followed by a BetWarrior failure).
# auth_precheck is still not wired in run_hot_loop, so this risk is reactive today.
_FRAGILE_AUTH_PLATFORMS = frozenset({"betwarrior-pba"})


def order_opportunity_for_execution(
    opp: ArbitrageOpportunity,
    *,
    staleness_rank: Mapping[str, Mapping[str, float]] | None = None,
) -> ArbitrageOpportunity:
    """Permute legs+stakes into placement order (leg_placement_order_decision.md):
    1. fragile-auth platform first (BW converts auth failures into clean leg-A
       aborts / gives the reauth rescue a zero-exposure window),
    2. laggard (perishable stale quote) before leader — rank source =
       ``staleness_rank`` in data/lag_model.json, keyed by the market_id suffix
       (``1x2``, ``btts``, ``ou_goals|2.5``). A missing market type ⇒ no
       staleness term (today's order). Fragile-auth still dominates (naked-
       incident evidence beats latency statistics).
    3. single-leg platform before a same-platform pair,
    4. smaller stake first,
    5. detector order as the stable tie-break.
    Pure local arithmetic — no wire calls (the doc's latency constraint).

    Applied at the orchestrator seam (before alert/record/execute) so ``opp.legs`` /
    ``opp.stakes`` stay positionally aligned with the executor's re-pricing closures
    (which zip against ``opp.legs``) and ``record_execution`` (which pairs
    ``result.legs[i]`` with ``opp.legs[i]`` / ``opp.stakes[i]``). Reordering anywhere
    downstream would desync audit/alerts from the actual placement order."""
    counts = Counter(q.platform for q in opp.legs)
    # Derive market type from the canonical market_id suffix (e.g. "fx-1|1x2" → "1x2").
    mid = opp.legs[0].market_id
    market_type = mid.split("|", 1)[1] if "|" in mid else ""
    rank = (staleness_rank or {}).get(market_type, {})
    order = sorted(
        range(len(opp.legs)),
        key=lambda i: (
            0 if opp.legs[i].platform in _FRAGILE_AUTH_PLATFORMS else 1,
            -rank.get(opp.legs[i].platform, 0.0),  # laggard (perishable quote) first
            counts[opp.legs[i].platform],
            opp.stakes[i],
            i,
        ),
    )
    return replace(
        opp,
        legs=tuple(opp.legs[i] for i in order),
        stakes=tuple(opp.stakes[i] for i in order),
    )


async def execute_opportunity(
    executor: Executor,
    opp: ArbitrageOpportunity,
    *,
    opp_id: str,
    budget: float,
    min_margin_pct: float,
    dynamic_stake_cap_ars: float | None = None,
) -> ExecutionResult:
    """Execute an approved N-leg opportunity through the Executor (N≥2 — a
    two-outcome O/U or a three-outcome 1X2 alike). The risk layer must have
    APPROVED it already; this only translates + places.

    ``budget`` and ``min_margin_pct`` mirror detection: they back the re-pricing
    closure that re-runs `detect_arbitrage` at the FRESH reverify odds and
    re-sizes the legs, so a drifted-but-still-profitable arb is captured instead
    of aborted on per-leg tolerance. ``budget`` is the per-arb budget (not
    ``opp.total_stake``), so a capped original allocation doesn't shrink the
    re-priced one. A one-sided opportunity (<2 legs) is not hedgeable; the
    Executor aborts it."""
    legs = legs_from_opportunity(opp, dynamic_stake_cap_ars=dynamic_stake_cap_ars)

    def _revalidate(
        current_legs: list[Leg],
        current_odds: list[float],
        current_caps: list[float | None],
    ) -> list[Leg] | None:
        # Re-price at fresh odds with the SAME detector. The fresh quote's
        # max_stake is the leg's EFFECTIVE live cap: a freshly-read dynamic cap
        # (current_caps[i], Betano's real per-bet ceiling) wins; otherwise the
        # leg's static cap (live_max_stake_ars, the conservative fallback). The
        # static fallback is None for dynamic platforms whose public feed carries
        # no max_stake, so WITHOUT a live cap the re-sized leg would fail the
        # guardrail — the cap_refresh hook exists to supply it.
        fresh = [
            replace(
                q,
                decimal_odds=o,
                max_stake=cap if cap is not None else leg.live_max_stake_ars,
            )
            for q, o, leg, cap in zip(
                opp.legs, current_odds, current_legs, current_caps, strict=True
            )
        ]
        try:
            repriced = detect_arbitrage(fresh, budget, min_margin_pct)
        except ValueError:
            repriced = None  # malformed fresh odds (≤1.0) → treat as no arb
        # One self-explanatory line per revalidation: the post-mortem record of
        # what the books said at the abort/survive decision (ledger 2026-07-04).
        overround = sum(1.0 / o for o in current_odds)
        log.info(
            "arb_executor.revalidated",
            opp_id=opp_id,
            original_odds=[leg.odds for leg in current_legs],
            current_odds=list(current_odds),
            fresh_margin_pct=round((1.0 - overround) * 100.0, 3),
            repriced_roi_pct=None if repriced is None else round(repriced.realized_roi_pct, 3),
        )
        if repriced is None:
            return None
        return [
            replace(leg, odds=q.decimal_odds, stake_ars=s, live_max_stake_ars=q.max_stake)
            for leg, q, s in zip(current_legs, repriced.legs, repriced.stakes, strict=True)
        ]

    def _residual(
        placed: list[PlacementResult],
        remaining: list[Leg],
        fresh_odds: list[float],
        fresh_caps: list[float | None],
    ) -> list[Leg] | None:
        # Re-price the NOT-yet-placed suffix around the already-filled legs.
        # ``remaining`` is always the ordered contiguous suffix legs[i:] (executor
        # contract) → opp.legs[offset:] are its source quotes (min_stake /
        # stake_increment / static caps), with offset = len(opp.legs) - len(remaining).
        offset = len(opp.legs) - len(remaining)
        fresh_quotes = [
            replace(
                q,
                decimal_odds=o,
                max_stake=cap if cap is not None else leg.live_max_stake_ars,
            )
            for q, leg, o, cap in zip(
                opp.legs[offset:], remaining, fresh_odds, fresh_caps, strict=True
            )
        ]
        placed_total = sum(r.stake_filled for r in placed)
        try:
            alloc = allocate_residual(
                [r.stake_filled * r.odds_filled for r in placed],
                placed_total,
                fresh_quotes,
                budget - placed_total,
            )
        except ValueError:
            return None  # malformed fresh odds (≤ 1.0) → treat as no salvage
        if alloc is None:
            return None
        return [
            replace(leg, odds=q.decimal_odds, stake_ars=s, live_max_stake_ars=q.max_stake)
            for leg, q, s in zip(remaining, fresh_quotes, alloc.stakes, strict=True)
        ]

    return await executor.execute_n_leg(opp_id, legs, revalidate=_revalidate, residual=_residual)
