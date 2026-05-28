"""Shared per-platform reliability factors.

Used by both the risk policy (in `policy.py`) and the stake sizer
(in `stake_sizing.py`). Co-locating the defaults here ensures they
never drift apart — both modules import the same table, both
respect the same unknown-platform fallback.

A reliability factor in `[0, 1]` per platform encodes the operator's
confidence that the platform will HONOR a winning arb leg without
post-hoc limiting, voiding, or stake reduction. Defaults are
informed priors:

- **bplay-pba (1.0)** — Bplay runs SportNCO, prices Brazilian-heavy
  fixtures tightly. Sharp pricing, expected to honor.
- **betwarrior-pba (1.0)** — Kambi backend. Sharp, B2B-grade pricing.
  Expected to honor stake limits cleanly.
- **betsson-pba (0.8)** — OBG backend; the systematic loose-side
  on Argentine domestic. Looser pricing suggests they may limit
  big winning bettors. Discount applied pending real-world placement
  data and a logged-in stake-limit recon.

These priors should be retuned as actual bet-placement outcomes
accumulate.
"""

from __future__ import annotations

from typing import Final

DEFAULT_PLATFORM_RELIABILITY: Final[dict[str, float]] = {
    "bplay-pba": 1.0,
    "betwarrior-pba": 1.0,
    "betsson-pba": 0.8,
}

# Fallback for platforms not in the table. Conservatively low so
# unknown books default to small stakes until calibrated.
DEFAULT_UNKNOWN_PLATFORM_RELIABILITY: Final[float] = 0.5
