"""Execution guardrails — the hardcoded financial safety layer.

Every bet placement passes these deterministic checks before any request is
sent. Execution NEVER decides profitability (that lives in ``src/risk/``);
guardrails enforce the operator's hard limits and halt the system on breach.
See ``docs/architecture.md`` Layer 4.

Enforced:
- **Kill switch** — manual, or automatic when the daily realized loss breaches
  the limit. Blocks all placement until reset.
- **Per-leg stake cap** — ``src/risk/stake_limits``. Dynamic-cap platforms
  (Betano) must supply the live cap or the leg is denied (fail-closed).
- **Per-match exposure cap** and **total portfolio exposure cap**.
- **Odds tolerance** — for the post-Leg-A re-verify before placing Leg B
  (abort if the odds dropped more than the tolerance).

State (exposure, daily P&L, kill switch) is held in-process; persistence is the
caller's concern. Caps are injected (``from_settings`` pulls them from config)
so the gates are unit-testable without environment.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from src.config import get_settings
from src.risk.stake_limits import effective_max_stake_ars

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GuardrailResult:
    """Outcome of a guardrail check. ``reason`` is empty when allowed."""

    allowed: bool
    reason: str = ""


_ALLOWED = GuardrailResult(allowed=True)


class Guardrails:
    """Deterministic financial safety gates for bet placement."""

    def __init__(
        self,
        *,
        max_position_per_match_ars: float,
        max_total_exposure_ars: float,
        max_daily_loss_ars: float,
        odds_tolerance_pct: float,
    ) -> None:
        self.max_position_per_match_ars = max_position_per_match_ars
        self.max_total_exposure_ars = max_total_exposure_ars
        self.max_daily_loss_ars = max_daily_loss_ars
        self.odds_tolerance_pct = odds_tolerance_pct
        self._kill_switch_reason: str | None = None
        self._total_exposure_ars = 0.0
        self._exposure_by_match: dict[str, float] = {}
        self._daily_pnl_ars = 0.0
        self._log = log.bind(component="guardrails")

    @classmethod
    def from_settings(cls) -> Guardrails:
        s = get_settings()
        return cls(
            max_position_per_match_ars=s.max_position_per_match,
            max_total_exposure_ars=s.max_total_exposure,
            max_daily_loss_ars=s.max_daily_loss,
            odds_tolerance_pct=s.odds_tolerance_pct,
        )

    # ---- kill switch ----

    @property
    def kill_switch_tripped(self) -> bool:
        return self._kill_switch_reason is not None

    def trip_kill_switch(self, reason: str) -> None:
        if self._kill_switch_reason is None:
            self._kill_switch_reason = reason
            self._log.error("guardrails.kill_switch_tripped", reason=reason)

    def reset_kill_switch(self) -> None:
        self._kill_switch_reason = None
        self._log.warning("guardrails.kill_switch_reset")

    # ---- pre-placement check ----

    def check_leg(
        self,
        *,
        platform: str,
        match_id: str,
        stake_ars: float,
        decimal_odds: float,
        live_max_stake_ars: float | None = None,
    ) -> GuardrailResult:
        """Gate one leg before placement. ``live_max_stake_ars`` is the cap read
        from the live bet slip (required for dynamic-cap platforms; tightens the
        static cap otherwise)."""
        if self.kill_switch_tripped:
            return GuardrailResult(False, f"kill switch tripped: {self._kill_switch_reason}")
        if stake_ars <= 0:
            return GuardrailResult(False, f"non-positive stake {stake_ars}")

        cap = effective_max_stake_ars(platform, decimal_odds)
        if cap is None:  # dynamic platform (Betano) — must have a live cap
            if live_max_stake_ars is None:
                return GuardrailResult(
                    False, f"{platform}: dynamic stake cap unresolved (no live limit) — fail closed"
                )
            cap = live_max_stake_ars
        elif live_max_stake_ars is not None:
            cap = min(cap, live_max_stake_ars)
        if stake_ars > cap:
            return GuardrailResult(False, f"stake {stake_ars:.2f} > platform cap {cap:.2f}")

        match_after = self._exposure_by_match.get(match_id, 0.0) + stake_ars
        if match_after > self.max_position_per_match_ars:
            return GuardrailResult(
                False,
                f"per-match exposure {match_after:.2f} > cap {self.max_position_per_match_ars:.2f}",
            )
        total_after = self._total_exposure_ars + stake_ars
        if total_after > self.max_total_exposure_ars:
            return GuardrailResult(
                False, f"total exposure {total_after:.2f} > cap {self.max_total_exposure_ars:.2f}"
            )
        return _ALLOWED

    def odds_still_acceptable(self, expected_odds: float, actual_odds: float) -> bool:
        """True if the leg's odds haven't dropped more than the tolerance.
        Odds rising is always fine (better for us); a drop beyond
        ``odds_tolerance_pct`` means the edge eroded → abort."""
        if expected_odds <= 0:
            return False
        floor = expected_odds * (1.0 - self.odds_tolerance_pct / 100.0)
        return actual_odds >= floor

    # ---- exposure / settlement accounting ----

    def record_exposure(self, match_id: str, stake_ars: float) -> None:
        self._exposure_by_match[match_id] = self._exposure_by_match.get(match_id, 0.0) + stake_ars
        self._total_exposure_ars += stake_ars

    def release_exposure(self, match_id: str, stake_ars: float) -> None:
        self._exposure_by_match[match_id] = max(
            0.0, self._exposure_by_match.get(match_id, 0.0) - stake_ars
        )
        self._total_exposure_ars = max(0.0, self._total_exposure_ars - stake_ars)

    def record_settlement(self, pnl_ars: float) -> None:
        """Record a settled P&L; auto-trip the kill switch if the day's realized
        loss reaches the limit."""
        self._daily_pnl_ars += pnl_ars
        if -self._daily_pnl_ars >= self.max_daily_loss_ars:
            self.trip_kill_switch(
                f"daily loss {-self._daily_pnl_ars:.2f} >= limit {self.max_daily_loss_ars:.2f}"
            )

    @property
    def total_exposure_ars(self) -> float:
        return self._total_exposure_ars

    @property
    def daily_pnl_ars(self) -> float:
        return self._daily_pnl_ars
