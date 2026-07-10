"""GARCH(1,1) adaptive margin threshold.

Offline `scripts/analyze_lag_structure.py --garch` fits GARCH(1,1) per market type
on the cross-platform implied-prob spread series and stores the parameters in
``data/lag_model.json`` under ``garch_per_market_type``. The live hot loop evolves
σ²_t per (market, cell) from its own per-cycle spread observations and scales the
detection margin threshold with the relative excess volatility: a higher margin
when the market is volatile, a lower one (down to the risk-policy floor) when calm.

Like the rest of ``src/arbitrage``, this module is intentionally pure: no I/O, no
async, no LLM. It depends on ``OddsQuote`` only (never ``src.semantic`` — layering:
the semantic layer imports arbitrage, not vice versa).
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from src.arbitrage.quotes import OddsQuote

# Platform-pair convention for the cross-platform spread, shared VERBATIM by the
# offline fit (analyze_lag_structure imports this) and the live extractor: for a
# market-cell quoted by >=2 of these platforms, spread = prob[first-present] -
# prob[second-present] in THIS order. One rule both sides — fitted units and live
# units can never drift. (Same platform set/order as the analysis script's
# HOT_LOOP_PLATFORMS.)
PAIR_ORDER: Final[tuple[str, ...]] = ("betano", "betwarrior-pba", "betsson-pba")

# Threshold cap: adaptive threshold never exceeds CAP_MULT x base. A garbage
# sigma estimate must not silently disable detection.
CAP_MULT: Final[float] = 3.0
# Drop per-market GARCH state not observed for this long (finished/vanished
# fixtures) — bounds memory in a long-running hot loop.
STATE_PRUNE_SEC: Final[float] = 7200.0

__all__ = [
    "CAP_MULT",
    "PAIR_ORDER",
    "STATE_PRUNE_SEC",
    "AdaptiveThreshold",
    "GarchParams",
    "SpreadObs",
    "ThresholdDecision",
    "market_type_from_market_id",
    "spread_observations",
]


def market_type_from_market_id(market_id: str) -> str:
    """Suffix after the fixture id: ``'1x2'``, ``'btts'``, ``'ou|2.5'``, ``''`` if
    malformed. Same idiom as ``arb_executor.order_opportunity_for_execution``'s
    inline derivation (left untouched there)."""
    return market_id.split("|", 1)[1] if "|" in market_id else ""


@dataclass(frozen=True)
class SpreadObs:
    """One cross-platform spread observation (raw implied-prob units, UNSCALED)."""

    market_id: str
    cell: str
    market_type: str
    spread: float


@dataclass(frozen=True)
class GarchParams:
    """GARCH(1,1) parameters in scaled units (spread x scale).

    The recursion is σ²_t = ω + α·ε²_{t-1} + β·σ²_{t-1} with ε_t = scale·spread -
    μ. The unconditional variance is ω/(1-α-β) — the steady state the live σ²_t
    reverts to and the baseline the threshold ratio is measured against.
    """

    mu: float
    omega: float
    alpha: float
    beta: float
    scale: float

    @property
    def sigma2_uncond(self) -> float:
        return self.omega / (1.0 - self.alpha - self.beta)

    @classmethod
    def from_artifact(cls, d: object) -> GarchParams | None:
        """Validate one artifact entry; ``None`` on any malformed/non-stationary
        shape (never guess). Requires finite floats, omega > 0, alpha >= 0,
        beta >= 0, alpha + beta < 0.999, scale > 0. ``n_series`` is informational
        and ignored at runtime."""
        if not isinstance(d, dict):
            return None
        try:
            mu = float(d["mu"])
            omega = float(d["omega"])
            alpha = float(d["alpha"])
            beta = float(d["beta"])
            scale = float(d["scale"])
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(x) for x in (mu, omega, alpha, beta, scale)):
            return None
        if omega <= 0.0 or alpha < 0.0 or beta < 0.0 or alpha + beta >= 0.999 or scale <= 0.0:
            return None
        return cls(mu=mu, omega=omega, alpha=alpha, beta=beta, scale=scale)


@dataclass(frozen=True)
class ThresholdDecision:
    threshold_pct: float
    garch_variance: float | None  # σ²_t of the governing cell, scaled units; None = no state


def spread_observations(
    market_id: str,
    quotes: Iterable[OddsQuote],
    now: float,
    max_age_sec: float,
) -> list[SpreadObs]:
    """Cross-platform spread observations for one market.

    Per cell (``OddsQuote.outcome``): keep quotes fresh as of ``now`` (within
    ``max_age_sec``) and on a ``PAIR_ORDER`` platform; take the first two platforms
    present in ``PAIR_ORDER`` order; ``spread = 1/odds[first] - 1/odds[second]``.
    Fewer than two present → no observation for that cell. A ``market_id`` without
    the fixture/market separator yields no observations (no market type to fit).
    """
    market_type = market_type_from_market_id(market_id)
    if not market_type:
        return []
    by_cell: dict[str, dict[str, OddsQuote]] = defaultdict(dict)
    for q in quotes:
        if q.platform not in PAIR_ORDER:
            continue
        if now - q.timestamp > max_age_sec:
            continue
        by_cell[q.outcome][q.platform] = q
    out: list[SpreadObs] = []
    for cell, platform_quotes in by_cell.items():
        ordered = [platform_quotes[p] for p in PAIR_ORDER if p in platform_quotes]
        if len(ordered) < 2:
            continue
        first, second = ordered[0], ordered[1]
        spread = 1.0 / first.decimal_odds - 1.0 / second.decimal_odds
        out.append(
            SpreadObs(market_id=market_id, cell=cell, market_type=market_type, spread=spread)
        )
    return out


@dataclass
class _CellState:
    """Per-(market, cell) GARCH recursion state (mutable — evolved in place)."""

    sigma2: float
    eps_prev: float
    last_seen: float


class AdaptiveThreshold:
    """Per-(market, cell) GARCH(1,1) state over per-cycle spread observations;
    per-market adaptive margin threshold.

    ``threshold_pct = clamp(base * (1 + sensitivity * (ratio - 1)), floor,
    max(floor, CAP_MULT * base))`` where ``ratio = max over the market's cells of
    sqrt(sigma2_t / sigma2_uncond)``. ``ratio == 1`` → base. ``sensitivity`` from
    ``GARCH_SENSITIVITY`` ("higher = more conservative in volatile regimes"). The
    ``floor`` is the risk-policy hard floor — detection never admits below what
    ``src/risk`` rejects anyway.

    The constructor validates fail-fast (it is built at the composition root,
    before any money flows): ``base_pct >= 0``, ``sensitivity >= 0``,
    ``floor_pct >= 0``, all finite, else ``ValueError``. This is NOT stricter than
    the repo's own config contract (``src/config.py`` declares
    ``min_margin_pct_base`` with ``ge=0.0``, so ``base_pct == 0`` is a valid,
    aggressive setting — it degenerates to "always the risk floor"). The clamp
    upper bound is ``max(floor_pct, CAP_MULT * base_pct)``: when
    ``base_pct < floor_pct / CAP_MULT`` the band would otherwise invert; the floor
    wins (threshold pinned AT the risk floor, never below).
    """

    def __init__(
        self,
        *,
        base_pct: float,
        sensitivity: float,
        floor_pct: float,
        params_by_market_type: Mapping[str, GarchParams],
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        for name, val in (
            ("base_pct", base_pct),
            ("sensitivity", sensitivity),
            ("floor_pct", floor_pct),
        ):
            if not math.isfinite(val) or val < 0.0:
                raise ValueError(f"{name} must be a finite non-negative number, got {val}")
        self._base_pct = base_pct
        self._sensitivity = sensitivity
        self._floor_pct = floor_pct
        self._params: dict[str, GarchParams] = dict(params_by_market_type)
        self._now_fn = now_fn
        self._state: dict[str, dict[str, _CellState]] = {}

    def observe_cycle(self, observations: Iterable[SpreadObs]) -> None:
        """One call per FULL detection cycle (never per trigger burst — uneven
        spacing would corrupt the recursion).

        Per observation with params for its ``market_type``: first sighting →
        ``sigma2 = sigma2_uncond``, ``eps_prev = scale*spread - mu`` (no variance
        update); otherwise ``sigma2 = omega + alpha*eps_prev**2 + beta*sigma2``
        (old), THEN ``eps_prev = scale*spread - mu``. A ``market_type`` without
        params is skipped (no state created). After processing, prune market_ids
        whose newest ``last_seen`` is older than ``STATE_PRUNE_SEC``.
        """
        now = self._now_fn()
        for obs in observations:
            params = self._params.get(obs.market_type)
            if params is None:
                continue
            cells = self._state.setdefault(obs.market_id, {})
            eps = params.scale * obs.spread - params.mu
            existing = cells.get(obs.cell)
            if existing is None:
                cells[obs.cell] = _CellState(
                    sigma2=params.sigma2_uncond, eps_prev=eps, last_seen=now
                )
            else:
                existing.sigma2 = (
                    params.omega
                    + params.alpha * existing.eps_prev**2
                    + params.beta * existing.sigma2
                )
                existing.eps_prev = eps
                existing.last_seen = now
        cutoff = now - STATE_PRUNE_SEC
        for market_id in list(self._state.keys()):
            cells = self._state[market_id]
            if max(cs.last_seen for cs in cells.values()) < cutoff:
                del self._state[market_id]

    def decide(self, market_id: str) -> ThresholdDecision:
        """Threshold for ``market_id``. No params for its ``market_type`` OR no
        state for this ``market_id`` → ``ThresholdDecision(base_pct, None)`` (static
        behavior). Else the formula above; ``garch_variance`` is the σ²_t of the
        cell with the max ratio."""
        params = self._params.get(market_type_from_market_id(market_id))
        if params is None:
            return ThresholdDecision(threshold_pct=self._base_pct, garch_variance=None)
        cells = self._state.get(market_id)
        if not cells:
            return ThresholdDecision(threshold_pct=self._base_pct, garch_variance=None)
        uncond = params.sigma2_uncond
        best_ratio = 0.0
        best_sigma2 = 0.0
        for cs in cells.values():
            ratio = math.sqrt(cs.sigma2 / uncond)
            if ratio > best_ratio:
                best_ratio = ratio
                best_sigma2 = cs.sigma2
        upper = max(self._floor_pct, CAP_MULT * self._base_pct)
        threshold = self._base_pct * (1.0 + self._sensitivity * (best_ratio - 1.0))
        threshold = max(self._floor_pct, min(upper, threshold))
        return ThresholdDecision(threshold_pct=threshold, garch_variance=best_sigma2)
