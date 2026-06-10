"""Canonical data types for cross-platform reconciliation.

Three platforms emit `RawOddsSnapshot`s with their own strings. The
semantic layer resolves those strings to platform-agnostic identifiers
so the arbitrage detector can group quotes by `(canonical_fixture,
canonical_market)` and feed them into `dutch_book.detect_arbitrage`.

This module is pure data — no I/O, no LLM, no async. The resolvers
that produce these types live in sibling modules (`market_resolver`,
`outcome_resolver`, `fixture_resolver`); the composer that emits
`CanonicalQuote`s lives in `canonicalizer`.

Design choices worth flagging:

- **`CanonicalMarketCode` is intentionally narrow.** v1 ships 1X2
  only because that's the one market all three current scrapers
  emit. The enum is set up to grow (BTTS, OU, DNB, AH placeholders
  are commented out below) but the resolver tables stay tight.

- **`CanonicalMarket` carries an optional `line`.** 1X2 / BTTS /
  DNB have `line=None`. Over/Under and Asian Handicap use the line
  value to discriminate distinct markets (the 2.5 OU and the 3.5
  OU are different partitions).

- **`CanonicalOutcome.cell`** is a string for now (`"HOME"`, `"DRAW"`,
  `"AWAY"`, `"OVER"`, `"UNDER"`, `"YES"`, `"NO"`). Could be a per-
  market enum later; keeping it as `str` avoids a combinatorial
  explosion of types while v1 only has 1X2.

- **`CanonicalFixture.fixture_id`** is opaque. The fixture_resolver
  assigns it; downstream code treats it as a black-box identifier
  for "the same real-world soccer match across all platforms."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from src.arbitrage.quotes import OddsQuote


class CanonicalMarketCode(StrEnum):
    """Platform-agnostic market identifier.

    Three markets ship today, all of them clean structural
    partitions: a complete coverage condition (`EXPECTED_CELLS`) is
    sufficient for the arb detector to proceed without invoking
    `partition_validator`. `DNB` and `AH` are reserved for later —
    Asian Handicap is where partition_validator becomes essential
    because the partition boundaries depend on push semantics that
    structural enumeration alone can't enforce.
    """

    H2H_3WAY = "1x2"  # home / draw / away
    BTTS = "btts"  # both teams to score (YES / NO)
    OU_GOALS = "ou_goals"  # total goals over/under, line-keyed
    # Reserved:
    # DNB  = "dnb"   # draw no bet (HOME / AWAY)
    # AH   = "ah"    # asian handicap (line carried, push semantics)


# Cell labels per market code. v1 uses strings rather than per-market
# enums to keep the partition checker general; the resolvers enforce
# which cells are valid for which market.
CELL_HOME = "HOME"
CELL_DRAW = "DRAW"
CELL_AWAY = "AWAY"
CELL_YES = "YES"
CELL_NO = "NO"
CELL_OVER = "OVER"
CELL_UNDER = "UNDER"

# The partition each market code is expected to cover. The arb
# detector uses this to know how many distinct cells a fully-covered
# group should produce before calling `dutch_book.detect_arbitrage`.
EXPECTED_CELLS: dict[CanonicalMarketCode, frozenset[str]] = {
    CanonicalMarketCode.H2H_3WAY: frozenset({CELL_HOME, CELL_DRAW, CELL_AWAY}),
    CanonicalMarketCode.BTTS: frozenset({CELL_YES, CELL_NO}),
    CanonicalMarketCode.OU_GOALS: frozenset({CELL_OVER, CELL_UNDER}),
}


@dataclass(frozen=True)
class CanonicalMarket:
    """A market is uniquely identified by (code, line).

    For 1X2 / BTTS / DNB the line is always None. For OU / AH the
    line discriminates distinct partitions.
    """

    code: CanonicalMarketCode
    line: float | None = None


@dataclass(frozen=True)
class CanonicalOutcome:
    """One cell of a market's partition.

    `cell` is a string label (`"HOME"` / `"DRAW"` / `"AWAY"` for 1X2;
    will extend per market). The resolver enforces that the cell is
    valid for the market.
    """

    market: CanonicalMarket
    cell: str


@dataclass(frozen=True)
class CanonicalFixture:
    """A real-world soccer match, abstracted across platforms.

    `fixture_id` is opaque — assigned by the fixture_resolver. Two
    raw events from different platforms get the same fixture_id iff
    the resolver judges them the same match. Treat it as a pointer,
    not a meaningful string.

    `home_team` and `away_team` are the canonical (normalized) forms
    (the output of `team_normalize.normalize_team_name`).

    `kickoff_utc` and `competition_slug` are `None` in v1 because the
    current `RawOddsSnapshot` does not carry them. When the scrapers
    are extended to emit kickoff + competition, the fixture_resolver
    will populate these fields and a future ambiguity-resolution pass
    (multiple fixtures with the same team names on different dates or
    in different competitions) becomes a clean filter rather than an
    LLM escalation. Equality is via fixture_id, not via the optional
    fields.
    """

    fixture_id: str
    home_team: str
    away_team: str
    # Reserve (segunda / youth) match flag. `home_team`/`away_team` are stored as
    # BASE names (reserve markers stripped) so cross-platform names compare cleanly;
    # this boolean keeps reserve matches distinct from their senior side. Two
    # fixtures with the same base teams but different `is_reserve` are NOT the same
    # match — never conflate them.
    is_reserve: bool = False
    kickoff_utc: datetime | None = None
    competition_slug: str | None = None


@dataclass(frozen=True)
class CanonicalQuote:
    """A platform's quote on one canonical outcome of one canonical fixture.

    This is what the arb detector groups by `(fixture, outcome.market)`
    before running `dutch_book.detect_arbitrage`. `odds_quote` is the
    existing `src.arbitrage.quotes.OddsQuote` ready to feed in —
    `market_id` and `outcome` on it carry the canonical strings.
    """

    fixture: CanonicalFixture
    outcome: CanonicalOutcome
    odds_quote: OddsQuote
