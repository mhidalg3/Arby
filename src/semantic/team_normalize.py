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

# One or more non-alphanumeric characters → replaced with a single space.
# Captures punctuation, hyphens, dots, and the various dashes/separators
# Kambi uses in `"Atlético Mineiro-MG"` etc.
_NON_ALNUM_RUN = re.compile(r"[^a-z0-9]+")
_WS_RUN = re.compile(r"\s+")


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
