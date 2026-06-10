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

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.arbitrage.quotes import OddsQuote
from src.execution.executor import ExecutionResult, Executor, Leg
from src.risk.stake_limits import is_dynamic


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


async def execute_opportunity(
    executor: Executor,
    opp: ArbitrageOpportunity,
    *,
    opp_id: str,
    dynamic_stake_cap_ars: float | None = None,
) -> ExecutionResult:
    """Execute an approved N-leg opportunity through the Executor (N≥2 — a
    two-outcome O/U or a three-outcome 1X2 alike). The risk layer must have
    APPROVED it already; this only translates + places.

    A one-sided opportunity (<2 legs) is not hedgeable; the Executor aborts it."""
    legs = legs_from_opportunity(opp, dynamic_stake_cap_ars=dynamic_stake_cap_ars)
    return await executor.execute_n_leg(opp_id, legs)
