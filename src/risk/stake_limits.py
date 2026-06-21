"""Per-platform stake limits, from the 2026-06-01 logged-in recon.

Public odds feeds carry no `max_stake`; these are the real bookmaker-imposed
per-leg caps captured from each platform's logged-in bet slip. The models
differ by platform, so the effective max for a leg depends on the odds:

- **Betsson** — flat `maxStake` 20,000,000 ARS AND a 100,000,000 payout cap →
  effective = ``min(20M, 100M / odds)``.
- **Betano** — DYNAMIC: the cap is server-side per bet
  (``POST /api/betslipcombo/limits`` → ``data.max``; confirmed readable pre-place for
  singles 2026-06-21, live-validated over the authenticated transport). ``effective_max_stake_ars``
  still returns ``None`` (the static model can't pre-compute it); the executor's
  ``cap_refresh`` hook (``BetanoCapRefresher``) reads it pre-place, fail-soft to the static
  fallback on any fault. See LEDGER 2026-06-21.
- **Bplay** — payout cap 999,999,999 (effectively unlimited) → ``cap / odds``.
  (The "9,999,999" seen in the web UI is a 7-digit input-field limit, NOT the
  API cap — see LEDGER 2026-06-01.)
- **BetWarrior** (Kambi) — no pre-bet limit exposed; enforced only at
  placement. Falls back to the conservative prior.

All values ARS. See ``scripts/recon/RECON_LOG.md`` / LEDGER 2026-06-01. This
module is the source of truth for the per-platform feasibility cap the risk
policy and execution guardrails both consume.
"""

from __future__ import annotations

from dataclasses import dataclass

# Conservative fallback when a platform exposes no pre-bet cap (BetWarrior),
# or for an unknown platform. The operator's informed prior, high enough to
# size real arbs yet low enough not to draw attention on a winning bet.
DEFAULT_MAX_STAKE_PER_LEG_ARS = 50_000.0


@dataclass(frozen=True)
class StakeLimit:
    """A platform's stake-limit model.

    ``flat_max_ars``  — hard per-leg stake cap, or None.
    ``max_payout_ars`` — payout cap; constrains stake to ``payout / odds``, or None.
    ``dynamic`` — the cap is computed server-side per bet and must be queried
                  live at placement (Betano); pre-bet feasibility is unknown.
    """

    flat_max_ars: float | None = None
    max_payout_ars: float | None = None
    dynamic: bool = False


# Keyed by the platform's base name. Snapshot `platform` values may be suffixed
# (e.g. "betsson-pba", "bplay-pba"); `_limit_for` matches on the prefix.
PLATFORM_STAKE_LIMITS: dict[str, StakeLimit] = {
    "betsson": StakeLimit(flat_max_ars=20_000_000.0, max_payout_ars=100_000_000.0),
    "betano": StakeLimit(dynamic=True),
    "bplay": StakeLimit(max_payout_ars=999_999_999.0),
    "betwarrior": StakeLimit(),  # none exposed → fallback prior
}


def _limit_for(platform: str) -> StakeLimit:
    base = platform.split("-", 1)[0].lower()
    return PLATFORM_STAKE_LIMITS.get(base, StakeLimit())


def is_dynamic(platform: str) -> bool:
    """True if the platform's cap must be read from the live bet slip (Betano)."""
    return _limit_for(platform).dynamic


def effective_max_stake_ars(
    platform: str,
    decimal_odds: float,
    fallback: float = DEFAULT_MAX_STAKE_PER_LEG_ARS,
) -> float | None:
    """Max stake (ARS) for one leg at the given odds.

    Returns ``None`` when the platform's cap is dynamic and must be queried
    live (Betano). Otherwise the tightest of {flat cap, payout/odds}, or
    ``fallback`` when the platform exposes no cap (BetWarrior / unknown)."""
    limit = _limit_for(platform)
    if limit.dynamic:
        return None
    candidates: list[float] = []
    if limit.flat_max_ars is not None:
        candidates.append(limit.flat_max_ars)
    if limit.max_payout_ars is not None and decimal_odds > 1.0:
        candidates.append(limit.max_payout_ars / decimal_odds)
    if not candidates:
        return fallback
    return min(candidates)
