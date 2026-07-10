"""Resolve a platform-specific raw outcome to a CanonicalOutcome cell.

Three markets, four resolution paths:

- **1X2 position labels** (BetWarrior / Kambi): `"1"` / `"X"` / `"2"`.
  Direct HOME / DRAW / AWAY mapping. No fixture context needed.

- **1X2 team-name labels** (Betsson, Bplay): the outcome carries the
  team name verbatim, with the draw cell labelled `"Empate"`. The
  resolved CanonicalFixture is required so we know which team is
  home and which is away.

- **BTTS labels** (Betsson; future Bplay): `"Si"` / `"Sí"` / `"Yes"`
  → YES, `"No"` → NO. No fixture context needed.

- **OU labels** with two shapes:
  - **Bplay** emits bare `"Más"` / `"Menos"` (no line in outcome).
  - **Betsson** embeds the line: `"más de 2.5"` / `"menos de 2.5"`.
  Both normalize so the prefix `"mas"` / `"menos"` is unambiguous
  after NFKD-folding.

`STRICT_MATCH_THRESHOLD = 0.85` for the team-name fuzzy compare —
generous enough to accept Bplay's `"Mirassol FC SP"` against
BetWarrior's canonical `"mirassol sp"` (≈0.94), tight enough to
reject `"Belgrano"` vs `"Belgrano Reserves"` (≈0.79).
"""

from __future__ import annotations

from typing import Final

from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.canonical import (
    CELL_AWAY,
    CELL_DRAW,
    CELL_HOME,
    CELL_NO,
    CELL_OVER,
    CELL_UNDER,
    CELL_YES,
    CanonicalFixture,
    CanonicalMarket,
    CanonicalMarketCode,
    CanonicalOutcome,
)
from src.semantic.team_normalize import normalize_team_name, strip_reserve, team_similarity

# 1X2 — platforms using position labels.
_POSITION_LABEL_PLATFORMS: Final[frozenset[str]] = frozenset({"betwarrior-pba"})
_POSITION_LABEL_CELLS: Final[dict[str, str]] = {
    "1": CELL_HOME,
    "X": CELL_DRAW,
    "2": CELL_AWAY,
}

# 1X2 — Spanish draw label used by Betsson and Bplay.
_DRAW_LABEL_NORMALIZED: Final[str] = "empate"

# BTTS — normalized label → cell. The normalize_team_name pipeline
# (NFKD fold + lowercase + alphanumeric-only) collapses `"Sí"`/`"Si"`
# /`"sí"`/`"yes"` into the keys here.
_BTTS_CELLS: Final[dict[str, str]] = {
    "si": CELL_YES,
    "yes": CELL_YES,
    "no": CELL_NO,
}

STRICT_MATCH_THRESHOLD: Final[float] = 0.85


def resolve_outcome(
    snapshot: RawOddsSnapshot,
    market: CanonicalMarket,
    fixture: CanonicalFixture,
) -> CanonicalOutcome | None:
    """Resolve a snapshot's outcome to a canonical cell. Returns None
    on unknown / ambiguous labels (canonicalizer drops the snapshot;
    next poll cycle gets another chance)."""
    if market.code is CanonicalMarketCode.H2H_3WAY:
        return _resolve_h2h_3way(snapshot, market, fixture)
    if market.code is CanonicalMarketCode.BTTS:
        return _resolve_btts(snapshot, market)
    if market.code is CanonicalMarketCode.OU_GOALS:
        return _resolve_ou_goals(snapshot, market)
    return None


def _resolve_h2h_3way(
    snapshot: RawOddsSnapshot,
    market: CanonicalMarket,
    fixture: CanonicalFixture,
) -> CanonicalOutcome | None:
    label = snapshot.raw_outcome_name.strip()
    if not label:
        return None

    if snapshot.platform in _POSITION_LABEL_PLATFORMS:
        cell = _POSITION_LABEL_CELLS.get(label)
        if cell is None:
            return None
        return CanonicalOutcome(market=market, cell=cell)

    # Team-name platforms: Empate first, then fuzzy match to home/away.
    if label.lower() == _DRAW_LABEL_NORMALIZED:
        return CanonicalOutcome(market=market, cell=CELL_DRAW)

    # Strip reserve markers — the fixture stores BASE names, so a reserve outcome
    # label ("Huracán Reserves" / "Huracán II") must compare on its base ("huracan").
    # The fixture's reserve-ness was already settled when it was linked.
    normalized, _ = strip_reserve(normalize_team_name(label))
    if not normalized:
        return None

    home_sim = team_similarity(normalized, fixture.home_team)
    away_sim = team_similarity(normalized, fixture.away_team)

    if home_sim >= STRICT_MATCH_THRESHOLD and home_sim > away_sim:
        return CanonicalOutcome(market=market, cell=CELL_HOME)
    if away_sim >= STRICT_MATCH_THRESHOLD and away_sim > home_sim:
        return CanonicalOutcome(market=market, cell=CELL_AWAY)
    return None


def _resolve_btts(snapshot: RawOddsSnapshot, market: CanonicalMarket) -> CanonicalOutcome | None:
    # NFKD-fold + lowercase + alphanumeric-only handles `"Sí"`, `"Si"`,
    # `"sí"`, `"No"`, `"no"` uniformly. Leading/trailing whitespace
    # gone via the pipeline.
    normalized = normalize_team_name(snapshot.raw_outcome_name)
    if not normalized:
        return None
    cell = _BTTS_CELLS.get(normalized)
    if cell is None:
        return None
    return CanonicalOutcome(market=market, cell=cell)


def _resolve_ou_goals(
    snapshot: RawOddsSnapshot, market: CanonicalMarket
) -> CanonicalOutcome | None:
    # Two shapes:
    #   Bplay:   "Más" / "Menos"             → "mas" / "menos"
    #   Betsson: "más de 2.5" / "menos de 2.5" → "mas de 2 5" / "menos de 2 5"
    # Prefix-matching the normalized form catches both. The line was
    # already canonicalized at the market level, so we don't need to
    # cross-check it from the outcome label.
    normalized = normalize_team_name(snapshot.raw_outcome_name)
    if not normalized:
        return None
    # `mas` matches "mas" (Bplay) and "mas de ..." (Betsson); `menos`
    # matches "menos" and "menos de ...". Order matters — check `menos`
    # before `mas` (since `mas` is a prefix of `menos` in some folding
    # paths? No, `menos` doesn't start with `mas`). Safe to check
    # either order, but be explicit about the boundary by requiring
    # a word boundary (the token form).
    first_token = normalized.split(" ", 1)[0]
    if first_token == "mas" or first_token == "over":
        return CanonicalOutcome(market=market, cell=CELL_OVER)
    if first_token == "menos" or first_token == "under":
        return CanonicalOutcome(market=market, cell=CELL_UNDER)
    return None
