"""Compose market_resolver + fixture_resolver + outcome_resolver.

Takes a `RawOddsSnapshot` and emits a `CanonicalQuote` ready to be
grouped by `(canonical_fixture, canonical_market)` and fed into
`dutch_book.detect_arbitrage`.

Order of operations:
  1. Resolve the canonical market from the platform-specific
     raw_market_name. If None, drop — market not in v1 scope.
  2. Resolve the canonical fixture (stateful via FixtureResolver).
     If None, drop — fixture unresolvable (e.g. Betsson snapshot
     with no anchor yet, or ambiguous match with no LLM matcher).
  3. Resolve the canonical outcome using the resolved fixture
     (needed because team-name outcomes need home/away context).
     If None, drop.
  4. Build the `OddsQuote` with canonical `market_id` / `outcome`
     strings derived from the canonical IDs — these are what
     `dutch_book` groups on.

The `market_id` is constructed as `f"{fixture_id}|{code}"` (plus
`|{line}` once OU/AH ship). Two snapshots from different platforms
that resolved to the same canonical fixture and the same canonical
market produce OddsQuotes with identical `market_id` — exactly the
shape `dutch_book.detect_arbitrage` expects.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.arbitrage.quotes import OddsQuote
from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.canonical import (
    CanonicalMarket,
    CanonicalQuote,
)
from src.semantic.fixture_resolver import FixtureResolver
from src.semantic.market_resolver import resolve_market
from src.semantic.outcome_resolver import resolve_outcome


def canonical_market_id(fixture_id: str, market: CanonicalMarket) -> str:
    """Stable identifier for `(fixture, market)` across platforms.

    `dutch_book.detect_arbitrage` groups quotes by `market_id` —
    snapshots from different platforms that resolved to the same
    canonical fixture + canonical market MUST produce identical
    `market_id` strings. The line is included for OU/AH markets
    (v1 has no line so the suffix is omitted).
    """
    if market.line is None:
        return f"{fixture_id}|{market.code.value}"
    return f"{fixture_id}|{market.code.value}|{market.line}"


@dataclass
class Canonicalizer:
    """Stateful composer. One per detector task.

    Wraps a FixtureResolver (the one stateful component) and the
    pure market/outcome resolvers. Pass it into the arb detector
    loop and feed snapshots through `canonicalize`.
    """

    fixture_resolver: FixtureResolver

    async def canonicalize(self, snapshot: RawOddsSnapshot) -> CanonicalQuote | None:
        """Resolve a snapshot to a canonical quote, or None to drop."""
        market = resolve_market(snapshot.platform, snapshot.raw_market_name)
        if market is None:
            return None

        fixture = await self.fixture_resolver.resolve(snapshot)
        if fixture is None:
            return None

        outcome = resolve_outcome(snapshot, market, fixture)
        if outcome is None:
            return None

        odds_quote = OddsQuote(
            platform=snapshot.platform,
            market_id=canonical_market_id(fixture.fixture_id, market),
            outcome=outcome.cell,
            decimal_odds=snapshot.decimal_odds,
            max_stake=snapshot.max_stake,
            timestamp=snapshot.timestamp,
            # Carry the platform's own IDs forward. The verifier uses
            # `platform_outcome_id` to match a leg back to a fresh
            # `odds:raw` snapshot (Tier 1), and `platform_event_id`
            # to address the platform's per-event read endpoint
            # directly (Tier 2 surgical refetch).
            platform_outcome_id=snapshot.platform_outcome_id,
            platform_event_id=snapshot.platform_event_id,
        )
        return CanonicalQuote(
            fixture=fixture,
            outcome=outcome,
            odds_quote=odds_quote,
        )
