"""Domain types for the arbitrage layer.

`OddsQuote` lives here (rather than in dutch_book.py) so the detector and the
stake allocator can both depend on it without a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OddsQuote:
    """A single odds quotation from one platform on one outcome.

    Attributes:
        platform: Identifier of the source platform (e.g. "codere", "betsson").
        market_id: Canonical market identifier, consistent across platforms.
        outcome: Canonical outcome identifier within the market.
        decimal_odds: Decimal-format odds; payout multiple including the stake.
            Must be strictly greater than 1.0.
        max_stake: Platform-imposed liquidity cap on a single bet. None means
            unbounded for our purposes (budget will dominate).
        timestamp: Unix epoch seconds when the odds were observed. Used by
            upstream staleness checks; this module does not inspect it.
        min_stake: Platform-imposed minimum bet size. If the optimal allocation
            for this leg falls below this value, the arb is unplaceable.
        stake_increment: Quantization grid for stakes on this platform (e.g.
            1.0 for whole-peso bets). Stakes are rounded DOWN to a multiple of
            this value so we never exceed `max_stake`. None means no rounding.
        platform_outcome_id: The platform's own unique identifier for this
            outcome (e.g. Betsson selection ID, Kambi outcome ID). The arb
            math doesn't consume it, but the pre-execution verifier needs
            it to match this leg back to a fresh stream snapshot. Optional
            for backwards compatibility with constructors that don't
            populate it (tests, ad-hoc opportunities); the canonicalizer
            always sets it from the source snapshot.
        platform_event_id: The platform's own unique identifier for the
            fixture (e.g. Betsson event UUID, Kambi event ID). Needed by
            the Tier-2 surgical refetcher to call the platform's
            per-event read endpoint directly. Optional with the same
            backwards-compatibility rationale as `platform_outcome_id`.
    """

    platform: str
    market_id: str
    outcome: str
    decimal_odds: float
    max_stake: float | None
    timestamp: float
    min_stake: float | None = None
    stake_increment: float | None = None
    platform_outcome_id: str | None = None
    platform_event_id: str | None = None
