"""Resolve a platform-specific raw market name to a CanonicalMarket.

Today's scope covers three markets across two output platforms
(Betsson and Bplay). BetWarrior emits only 1X2 from its list-view
endpoint; adding BTTS + OU on BetWarrior requires the per-event
polling extension (separate scope).

Raw strings observed in the live Redis stream:

    1X2 (H2H_3WAY):
      Betsson PBA:    "Ganador del partido"  (capitalization varies)
      Bplay PBA:      "1-X-2"
      BetWarrior PBA: "Resultado Final"

    BTTS:
      Betsson PBA:    "Ambos equipos anotan"
      Bplay PBA:      (not yet sampled; scraper emits any market whose
                       type_name starts with "Ambos" — prefix-matched
                       here for safety until a live sample appears)

    OU goals (line-keyed):
      Betsson PBA:    "Total de goles 2.5"  (line embedded in name)
      Bplay PBA:      "Más de / Menos de 2.5"  (line embedded in name)

Important resolver decision — **Betsson integer OU lines are
REJECTED.** Betsson emits both half-lines (0.5, 1.5, 2.5, ...) AND
integer lines (1, 2, 3, 4). Integer lines are push lines: a goal
total equal to the integer refunds both sides. That breaks the
clean {OVER, UNDER} partition the arb math assumes (the implied
probabilities sum to `1 - P(push)`, not 1, so the detector would
emit false arbs). v1 accepts only half-lines.

Lookups are case-insensitive (Betsson's `Ganador del partido` vs
`Ganador del Partido` capitalization inconsistency, confirmed in
live data).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from src.semantic.canonical import CanonicalMarket, CanonicalMarketCode

# --- 1X2 ---

_H2H_3WAY_NAMES_BY_PLATFORM: Final[dict[str, frozenset[str]]] = {
    "betsson-pba": frozenset({"ganador del partido"}),
    # Bplay XML uses "1-X-2". Bplay SSE (live odds) uses
    # "¿Quién ganará el partido?" which normalizes (NFKD-fold + drop
    # non-ASCII) to "quien ganara el partido?".
    "bplay-pba": frozenset({"1-x-2", "quien ganara el partido?"}),
    "betwarrior-pba": frozenset({"resultado final"}),
    # Betano (Kaizen MRES market). Scraper emits "Resultado del partido".
    "betano": frozenset({"resultado del partido"}),
}

# --- BTTS ---
#
# All keys are post-NFKD-fold (accents stripped), lowercase. BetWarrior
# uses future tense `"Marcarán"` (→ `"marcaran"` post-fold); Betsson
# uses present `"anotan"`. The Bplay XML scraper has a "Ambos" prefix
# matcher but the exact `criterion.label` hasn't been observed in a
# live sample — we keep the prefix fallback for Bplay until it appears.

_BTTS_EXACT_NAMES_BY_PLATFORM: Final[dict[str, frozenset[str]]] = {
    "betsson-pba": frozenset({"ambos equipos anotan"}),
    "betwarrior-pba": frozenset({"ambos equipos marcaran"}),
}
_BTTS_PREFIX_PLATFORMS: Final[frozenset[str]] = frozenset({"bplay-pba"})

# --- OU goals ---
#
# All three platforms format the line as an unparenthesized decimal
# at the end of the (depth-scraper-constructed for Kambi) market name.

_OU_PATTERNS_BY_PLATFORM: Final[dict[str, re.Pattern[str]]] = {
    "betsson-pba": re.compile(r"^total de goles (\d+(?:\.\d+)?)$"),
    # Bplay XML formats as "Más de / Menos de X.X" (line in market
    # name). Bplay SSE formats as "Total de Goles X.X" (line lifted
    # from outcome's `act` field by the SSE scraper for resolver
    # parity). Accept either prefix.
    "bplay-pba": re.compile(
        r"^(?:mas de / menos de|total de goles) (\d+(?:\.\d+)?)$"
    ),
    "betwarrior-pba": re.compile(r"^total de goles (\d+(?:\.\d+)?)$"),
}

# Internal: collapse whitespace runs.
_WS_RUN = re.compile(r"\s+")


def _normalize_market_key(raw_market_name: str) -> str:
    """NFKD-fold + lowercase + collapse whitespace.

    Accent-folding is essential because Kambi emits Spanish with
    accents intact (`"Ambos Equipos Marcarán"`, `"Más de / Menos de"`)
    while Betsson and Bplay's market names happen to be accent-free
    in the labels we use as keys. Folding makes the crosswalk safe
    to extend without per-platform accent rules."""
    folded = unicodedata.normalize("NFKD", raw_market_name)
    ascii_lower = folded.encode("ascii", "ignore").decode("ascii").lower()
    return _WS_RUN.sub(" ", ascii_lower).strip()


def _is_half_line(line: float) -> bool:
    """True iff `line` is an X.5 half-line. Filters out push lines
    (1, 2, 3, 4) and any future quarter-lines (which would belong
    to Asian Over/Under, not goal-total OU)."""
    return abs((line * 2) - round(line * 2)) < 1e-9 and (line * 2) % 2 == 1


def resolve_market(platform: str, raw_market_name: str) -> CanonicalMarket | None:
    """Return the CanonicalMarket for `(platform, raw_market_name)`, or `None`.

    `None` means "not in scope" — the canonicalizer drops the
    snapshot. Not an error.
    """
    key = _normalize_market_key(raw_market_name)

    # 1X2 — exact match against the lowercased per-platform set.
    if key in _H2H_3WAY_NAMES_BY_PLATFORM.get(platform, frozenset()):
        return CanonicalMarket(code=CanonicalMarketCode.H2H_3WAY, line=None)

    # BTTS — exact or prefix per platform.
    if key in _BTTS_EXACT_NAMES_BY_PLATFORM.get(platform, frozenset()):
        return CanonicalMarket(code=CanonicalMarketCode.BTTS, line=None)
    if platform in _BTTS_PREFIX_PLATFORMS and key.startswith("ambos"):
        return CanonicalMarket(code=CanonicalMarketCode.BTTS, line=None)

    # OU goals — extract and validate the line.
    pattern = _OU_PATTERNS_BY_PLATFORM.get(platform)
    if pattern is not None:
        match = pattern.match(key)
        if match is not None:
            try:
                line = float(match.group(1))
            except ValueError:
                return None
            if not _is_half_line(line):
                # Integer line = push line. Reject — arb math is invalid.
                return None
            return CanonicalMarket(code=CanonicalMarketCode.OU_GOALS, line=line)

    return None
