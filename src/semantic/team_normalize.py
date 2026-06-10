"""Team-name normalization and similarity for cross-platform matching.

The three current scrapers expose team names in three different
canonicalizations:

- **BetWarrior** (Kambi):    `"Mirassol-SP"`, `"Atlético Mineiro-MG"`, `"LDU Quito"`
- **Bplay** (SportNCO):      `"Mirassol FC SP"`, `"Atlético Mineiro MG"`, `"Always Ready"`
- **Betsson** (OBG):         outcome labels carry the team name directly,
                              e.g. `"Gimnasia Jujuy"`, `"Belgrano"`,
                              `"Defensa Y Justicia Reserve"`.

This module is the cheap deterministic layer the fixture and outcome
resolvers use to decide whether two strings refer to the same team.
No LLM. No external knowledge. Pure text processing.

Two operations exported:

- `normalize_team_name(name)` — NFKD-fold accents, lowercase, replace
  non-alnum runs with single spaces, collapse whitespace, strip.
  Idempotent. Two names that normalize to the same string are an
  EXACT match.

- `team_similarity(a, b)` — `difflib.SequenceMatcher.ratio` on the
  normalized forms. Returns `[0.0, 1.0]`. Tunable threshold owned
  by the caller.

What we deliberately do NOT do:

- **No suffix stripping** for tokens like `Reserves`, `SP`, `MG`,
  `RJ`, or `Sub-20`. These are real distinguishers — `Belgrano` and
  `Belgrano Reserves` are different teams; `Atlético-MG` and
  `Atlético-PR` are different teams. Stripping them would silently
  conflate distinct fixtures.

- **No alias table** at this layer. Some platforms write
  `Paris SG`, others `Paris Saint-Germain`. We let `team_similarity`
  catch those (SequenceMatcher.ratio ≈ 0.7+) and surface low-
  confidence matches for the LLM fallback in `fixture_resolver`.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Final

# One or more non-alphanumeric characters → replaced with a single space.
# Captures punctuation, hyphens, dots, and the various dashes/separators
# Kambi uses in `"Atlético Mineiro-MG"` etc.
_NON_ALNUM_RUN = re.compile(r"[^a-z0-9]+")
_WS_RUN = re.compile(r"\s+")

# Tokens that mark a RESERVE (segunda / youth) side rather than a distinct
# team: Betano writes `"… ii"`, Bplay/BetWarrior `"… Reserves"`, Spanish
# `"reserva"`. Stripping these lets the SAME team's reserve side compare cleanly
# across platforms — but reserve-ness must then be carried as a separate boolean
# so a reserve match is NEVER conflated with its senior side (the distinction
# `normalize_team_name` deliberately preserves). See `strip_reserve`.
_RESERVE_MARKER_TOKENS: Final[frozenset[str]] = frozenset({"ii", "reserve", "reserves", "reserva"})


def normalize_team_name(name: str) -> str:
    """Return a comparable lowercase ASCII form of a team name.

    Idempotent. Empty input returns empty string.

    Examples:
        >>> normalize_team_name("Mirassol-SP")
        'mirassol sp'
        >>> normalize_team_name("Atlético Mineiro-MG")
        'atletico mineiro mg'
        >>> normalize_team_name("Defensa Y Justicia Reserve")
        'defensa y justicia reserve'
    """
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name)
    ascii_lower = folded.encode("ascii", "ignore").decode("ascii").lower()
    spaced = _NON_ALNUM_RUN.sub(" ", ascii_lower)
    return _WS_RUN.sub(" ", spaced).strip()


def team_similarity(a: str, b: str) -> float:
    """Return a similarity score in `[0.0, 1.0]` between two team names.

    Uses `difflib.SequenceMatcher.ratio` on the normalized forms. The
    threshold for "match" is the caller's choice; the fixture resolver
    uses 0.85 for confident matches and treats 0.7-0.85 as ambiguous
    (LLM fallback).

    Examples:
        >>> team_similarity("Mirassol-SP", "Mirassol FC SP") > 0.85
        True
        >>> team_similarity("Paris SG", "Paris Saint-Germain") > 0.6
        True
        >>> team_similarity("Belgrano", "Belgrano Reserves") > 0.9
        False
    """
    na = normalize_team_name(a)
    nb = normalize_team_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(a=na, b=nb).ratio()


def strip_reserve(normalized_name: str) -> tuple[str, bool]:
    """Split an already-normalized name into ``(base, is_reserve)``.

    Removes reserve-marker tokens (``ii`` / ``reserve`` / ``reserves`` /
    ``reserva``) so the same team's reserve side compares cleanly across
    platforms (``"huracan ii"`` and ``"huracan"`` → base ``"huracan"``), while
    the boolean preserves the reserve-vs-senior distinction the caller MUST NOT
    collapse. Operates on tokens, so a marker anywhere is removed.

        >>> strip_reserve("huracan ii")
        ('huracan', True)
        >>> strip_reserve("belgrano reserves")
        ('belgrano', True)
        >>> strip_reserve("boca juniors")
        ('boca juniors', False)
    """
    tokens = normalized_name.split()
    kept = [t for t in tokens if t not in _RESERVE_MARKER_TOKENS]
    return " ".join(kept), len(kept) != len(tokens)


def competition_is_reserve(competition: str) -> bool:
    """True if a competition/league label denotes a reserve division (e.g.
    Betsson's ``"liga-profesional-de-reserva"``). Some platforms leave the team
    names bare and mark the reserve status only on the competition, so this is
    the reserve signal there. Matches the ``reserv`` stem (reserva/reserve(s))."""
    return "reserv" in normalize_team_name(competition)
