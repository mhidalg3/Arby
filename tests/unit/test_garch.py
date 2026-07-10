"""Tests for ``src.arbitrage.garch`` — GARCH(1,1) adaptive margin threshold.

Pure-math module (no I/O, no async); convention for ``src/arbitrage`` is 100% line
coverage. Every branch of the recursion, the clamp band, artifact validation, the
spread pair rule, and state pruning is exercised against hand-computed values.
"""

from __future__ import annotations

import pytest

from src.arbitrage.garch import (
    CAP_MULT,
    PAIR_ORDER,
    STATE_PRUNE_SEC,
    AdaptiveThreshold,
    GarchParams,
    SpreadObs,
    ThresholdDecision,
    market_type_from_market_id,
    spread_observations,
)
from src.arbitrage.quotes import OddsQuote

# Canonical test params: mu=0, omega=1, alpha=0.1, beta=0.8, scale=1000 ⇒ σ²_uncond=10.
_P = GarchParams(mu=0.0, omega=1.0, alpha=0.1, beta=0.8, scale=1000.0)
_STATIC = ThresholdDecision(threshold_pct=1.0, garch_variance=None)


def _obs(
    spread: float, market_id: str = "FIX1|1x2", cell: str = "HOME", mtype: str = "1x2"
) -> SpreadObs:
    return SpreadObs(market_id=market_id, cell=cell, market_type=mtype, spread=spread)


def _quote(platform: str, outcome: str, odds: float, ts: float = 1000.0) -> OddsQuote:
    return OddsQuote(
        platform=platform,
        market_id="FIX1|1x2",
        outcome=outcome,
        decimal_odds=odds,
        max_stake=None,
        timestamp=ts,
    )


# ---- market_type_from_market_id ----


def test_market_type_from_market_id_suffix() -> None:
    assert market_type_from_market_id("FIX1|1x2") == "1x2"
    assert market_type_from_market_id("FX9|ou|2.5") == "ou|2.5"


def test_market_type_from_market_id_no_separator_is_empty() -> None:
    assert market_type_from_market_id("FIX1") == ""


# ---- GarchParams ----


def test_sigma2_uncond() -> None:
    assert _P.sigma2_uncond == pytest.approx(10.0)


class TestFromArtifact:
    def test_accepts_valid_dict(self) -> None:
        d = {"mu": 0.0, "omega": 1.0, "alpha": 0.1, "beta": 0.8, "scale": 1000.0, "n_series": 7}
        p = GarchParams.from_artifact(d)
        assert p is not None
        assert p.scale == 1000.0
        assert p.sigma2_uncond == pytest.approx(10.0)

    def test_rejects_non_stationary(self) -> None:
        # alpha + beta >= 0.999
        assert (
            GarchParams.from_artifact(
                {"mu": 0.0, "omega": 1.0, "alpha": 0.6, "beta": 0.5, "scale": 1000.0}
            )
            is None
        )

    def test_rejects_zero_omega(self) -> None:
        assert (
            GarchParams.from_artifact(
                {"mu": 0.0, "omega": 0.0, "alpha": 0.1, "beta": 0.8, "scale": 1000.0}
            )
            is None
        )

    def test_rejects_negative_alpha(self) -> None:
        assert (
            GarchParams.from_artifact(
                {"mu": 0.0, "omega": 1.0, "alpha": -0.1, "beta": 0.8, "scale": 1000.0}
            )
            is None
        )

    def test_rejects_negative_beta(self) -> None:
        assert (
            GarchParams.from_artifact(
                {"mu": 0.0, "omega": 1.0, "alpha": 0.1, "beta": -0.1, "scale": 1000.0}
            )
            is None
        )

    def test_rejects_zero_scale(self) -> None:
        assert (
            GarchParams.from_artifact(
                {"mu": 0.0, "omega": 1.0, "alpha": 0.1, "beta": 0.8, "scale": 0.0}
            )
            is None
        )

    def test_rejects_non_dict(self) -> None:
        assert GarchParams.from_artifact([1, 2, 3]) is None
        assert GarchParams.from_artifact("nope") is None

    def test_rejects_missing_key(self) -> None:
        assert (
            GarchParams.from_artifact({"mu": 0.0, "omega": 1.0, "alpha": 0.1, "beta": 0.8}) is None
        )

    def test_rejects_non_finite_values(self) -> None:
        bad = {"mu": float("nan"), "omega": 1.0, "alpha": 0.1, "beta": 0.8, "scale": 1000.0}
        assert GarchParams.from_artifact(bad) is None
        bad_inf = {"mu": float("inf"), "omega": 1.0, "alpha": 0.1, "beta": 0.8, "scale": 1000.0}
        assert GarchParams.from_artifact(bad_inf) is None

    def test_rejects_unparsable_values(self) -> None:
        # float("abc") → ValueError; float(None) → TypeError
        assert (
            GarchParams.from_artifact(
                {"mu": "abc", "omega": 1.0, "alpha": 0.1, "beta": 0.8, "scale": 1000.0}
            )
            is None
        )
        assert (
            GarchParams.from_artifact(
                {"mu": None, "omega": 1.0, "alpha": 0.1, "beta": 0.8, "scale": 1000.0}
            )
            is None
        )


# ---- spread_observations ----


def test_spread_pair_rule_uses_pair_order_first_two() -> None:
    # All three PAIR_ORDER platforms on HOME. betano & betwarrior-pba are first two
    # → spread = 1/odds_betano − 1/odds_betwarrior (betsson ignored for this cell).
    q = [
        _quote("betsson-pba", "HOME", 4.0),  # would be the third; ignored
        _quote("betano", "HOME", 2.0),
        _quote("betwarrior-pba", "HOME", 2.5),
    ]
    obs = spread_observations("FIX1|1x2", q, now=1000.0, max_age_sec=45.0)
    assert len(obs) == 1
    assert obs[0].spread == pytest.approx(1.0 / 2.0 - 1.0 / 2.5)  # 0.5 − 0.4 = 0.1


def test_spread_sign_fixed_by_pair_order_not_input_order() -> None:
    # Same quotes, reversed input order → same sign (PAIR_ORDER governs).
    fwd = [_quote("betano", "HOME", 2.0), _quote("betwarrior-pba", "HOME", 2.5)]
    rev = [_quote("betwarrior-pba", "HOME", 2.5), _quote("betano", "HOME", 2.0)]
    of = spread_observations("FIX1|1x2", fwd, now=1000.0, max_age_sec=45.0)
    o_rev = spread_observations("FIX1|1x2", rev, now=1000.0, max_age_sec=45.0)
    assert of[0].spread == pytest.approx(0.1)
    assert o_rev[0].spread == pytest.approx(0.1)
    assert of[0].cell == "HOME" and o_rev[0].cell == "HOME"
    assert of[0].market_type == "1x2"


def test_spread_single_platform_no_observation() -> None:
    q = [_quote("betano", "HOME", 2.0)]
    assert spread_observations("FIX1|1x2", q, now=1000.0, max_age_sec=45.0) == []


def test_spread_stale_quote_excluded() -> None:
    # betwarrior is 100s old (> 45s) → only betano fresh → no pair → no obs.
    q = [_quote("betano", "HOME", 2.0), _quote("betwarrior-pba", "HOME", 2.5, ts=900.0)]
    assert spread_observations("FIX1|1x2", q, now=1000.0, max_age_sec=45.0) == []


def test_spread_non_pair_order_platform_ignored() -> None:
    # bplay-pba is NOT in PAIR_ORDER → ignored; only betano present → no pair.
    q = [_quote("betano", "HOME", 2.0), _quote("bplay-pba", "HOME", 2.5)]
    assert spread_observations("FIX1|1x2", q, now=1000.0, max_age_sec=45.0) == []


def test_spread_empty_market_type_no_observations() -> None:
    # market_id without the separator → no market type → no observations.
    q = [_quote("betano", "HOME", 2.0), _quote("betwarrior-pba", "HOME", 2.5)]
    assert spread_observations("FIX1", q, now=1000.0, max_age_sec=45.0) == []


def test_spread_multiple_cells_one_per_cell() -> None:
    # Two cells, each with a betano+betwarrior pair → two observations.
    q = [
        _quote("betano", "HOME", 2.0),
        _quote("betwarrior-pba", "HOME", 2.5),
        _quote("betano", "AWAY", 3.0),
        _quote("betwarrior-pba", "AWAY", 3.0),
    ]
    obs = spread_observations("FIX1|1x2", q, now=1000.0, max_age_sec=45.0)
    cells = {o.cell for o in obs}
    assert cells == {"HOME", "AWAY"}


# ---- AdaptiveThreshold recursion ----


def _at(now_seq: list[float], **kw: float) -> AdaptiveThreshold:
    defaults: dict[str, object] = dict(
        base_pct=1.0, sensitivity=2.0, floor_pct=0.5, params_by_market_type={"1x2": _P}
    )
    defaults.update(kw)
    return AdaptiveThreshold(now_fn=lambda: now_seq[0], **defaults)  # type: ignore[arg-type]


def test_recursion_hand_computed_values() -> None:
    """params (mu=0,ω=1,α=0.1,β=0.8,scale=1000): observe [0.002, 0.005] →
    after 1st σ²=10,eps=2; after 2nd σ²=9.4,eps=5."""
    now = [1000.0]
    at = _at(now)
    at.observe_cycle([_obs(0.002)])
    cs = at._state["FIX1|1x2"]["HOME"]
    assert cs.sigma2 == pytest.approx(10.0)
    assert cs.eps_prev == pytest.approx(2.0)
    now[0] = 1060.0
    at.observe_cycle([_obs(0.005)])
    cs = at._state["FIX1|1x2"]["HOME"]
    assert cs.sigma2 == pytest.approx(9.4)  # 1 + 0.1·4 + 0.8·10
    assert cs.eps_prev == pytest.approx(5.0)


def test_observe_skips_market_type_without_params() -> None:
    now = [1000.0]
    at = _at(now)
    at.observe_cycle([_obs(0.002, mtype="btts")])  # btts not in params
    assert "FIX1|1x2" not in at._state
    # decide still returns the static base (no state, no params for btts)
    assert at.decide("FIX1|1x2") == _STATIC


def test_decide_ratio_one_is_base() -> None:
    """First sighting sets σ² = σ²_uncond → ratio = 1 → exactly base."""
    now = [1000.0]
    at = _at(now)
    at.observe_cycle([_obs(0.002)])
    dec = at.decide("FIX1|1x2")
    assert dec.threshold_pct == pytest.approx(1.0)
    assert dec.garch_variance == pytest.approx(10.0)


def test_decide_elevated_variance_raises_threshold() -> None:
    """End-to-end: observe [0.002, 0.005, 0.005] → σ²=11.02, ratio≈1.0498,
    threshold≈1.0996."""
    now = [1000.0]
    at = _at(now)
    at.observe_cycle([_obs(0.002)])
    now[0] = 1060.0
    at.observe_cycle([_obs(0.005)])
    now[0] = 1120.0
    at.observe_cycle([_obs(0.005)])
    dec = at.decide("FIX1|1x2")
    assert dec.threshold_pct == pytest.approx(1.0996, rel=1e-3)
    assert dec.garch_variance == pytest.approx(11.02)


def test_decide_extreme_variance_capped() -> None:
    """Huge spread → σ² explodes (on the cycle AFTER it lands as eps_prev) →
    threshold capped at CAP_MULT * base. The recursion reads eps_prev, so a giant
    spread only inflates σ² on the next observe_cycle."""
    now = [1000.0]
    at = _at(now)
    at.observe_cycle([_obs(0.002)])
    now[0] = 1060.0
    at.observe_cycle([_obs(100.0)])  # eps_prev := 100000 (σ² still 9.4 this cycle)
    now[0] = 1120.0
    at.observe_cycle([_obs(100.0)])  # σ² = ω + α·100000² + β·9.4 → astronomical
    dec = at.decide("FIX1|1x2")
    assert dec.threshold_pct == pytest.approx(CAP_MULT * 1.0)


def test_decide_low_variance_clamped_at_floor() -> None:
    """ratio < 1 with high sensitivity/base drives the formula below floor → clamped
    at the risk-policy floor."""
    now = [1000.0]
    at = _at(now, base_pct=10.0, sensitivity=100.0, floor_pct=0.5)
    at.observe_cycle([_obs(0.0)])  # eps=0 → σ² = ω + 0 + β·10 = 9 < uncond(10)
    now[0] = 1060.0
    at.observe_cycle([_obs(0.0)])  # σ² keeps falling toward ω/(1-β)=5
    dec = at.decide("FIX1|1x2")
    assert dec.threshold_pct == 0.5


def test_decide_unknown_market_type_is_static_base() -> None:
    now = [1000.0]
    at = _at(now)
    assert at.decide("FIX2|btts") == _STATIC


def test_decide_market_with_params_but_no_state_is_static_base() -> None:
    now = [1000.0]
    at = _at(now)
    # params has "1x2" but we never observed this market → no state.
    assert at.decide("FIX9|1x2") == _STATIC


def test_decide_market_id_without_separator_is_static_base() -> None:
    now = [1000.0]
    at = _at(now)
    at.observe_cycle([_obs(0.002)])
    # market_type "" is not in params → static base.
    assert at.decide("FIX1") == _STATIC


def test_decide_picks_max_ratio_cell() -> None:
    """Two cells: one calm, one volatile → threshold reflects the volatile cell.
    The big spread only inflates σ² on the cycle AFTER it lands as eps_prev, so a
    third observe_cycle is needed for AWAY's σ² to actually diverge from HOME's."""
    now = [1000.0]
    at = _at(now)
    at.observe_cycle([_obs(0.002, cell="HOME"), _obs(0.002, cell="AWAY")])
    now[0] = 1060.0
    # HOME stays calm; AWAY jumps (eps_prev := 50000 this cycle).
    at.observe_cycle([_obs(0.002, cell="HOME"), _obs(50.0, cell="AWAY")])
    now[0] = 1120.0
    # AWAY σ² now explodes from the stored 50000²; HOME stays near unconditional.
    at.observe_cycle([_obs(0.002, cell="HOME"), _obs(50.0, cell="AWAY")])
    dec = at.decide("FIX1|1x2")
    # AWAY σ² dominates; garch_variance is AWAY's σ².
    away_sigma2 = at._state["FIX1|1x2"]["AWAY"].sigma2
    home_sigma2 = at._state["FIX1|1x2"]["HOME"].sigma2
    assert away_sigma2 > home_sigma2
    assert dec.garch_variance == pytest.approx(away_sigma2)
    assert dec.threshold_pct > 1.0


# ---- ctor validation ----


def test_ctor_rejects_negative_base() -> None:
    with pytest.raises(ValueError):
        AdaptiveThreshold(base_pct=-1.0, sensitivity=1.0, floor_pct=1.0, params_by_market_type={})


def test_ctor_rejects_negative_sensitivity() -> None:
    with pytest.raises(ValueError):
        AdaptiveThreshold(base_pct=1.0, sensitivity=-1.0, floor_pct=1.0, params_by_market_type={})


def test_ctor_rejects_non_finite_floor() -> None:
    with pytest.raises(ValueError):
        AdaptiveThreshold(
            base_pct=1.0, sensitivity=1.0, floor_pct=float("nan"), params_by_market_type={}
        )


def test_ctor_accepts_base_zero() -> None:
    """base_pct == 0 is a valid (aggressive) config-contract setting; decide() pins
    at the risk floor."""
    now = [1000.0]
    at = AdaptiveThreshold(
        base_pct=0.0,
        sensitivity=2.0,
        floor_pct=0.5,
        params_by_market_type={"1x2": _P},
        now_fn=lambda: now[0],
    )
    at.observe_cycle([_obs(0.002)])
    dec = at.decide("FIX1|1x2")
    assert dec.threshold_pct == 0.5


def test_clamp_inversion_floor_wins() -> None:
    """base=0.1, floor=0.5: CAP_MULT*base=0.3 < floor → upper bound = floor → any
    threshold clamps to exactly 0.5 even at extreme σ²."""
    now = [1000.0]
    at = AdaptiveThreshold(
        base_pct=0.1,
        sensitivity=2.0,
        floor_pct=0.5,
        params_by_market_type={"1x2": _P},
        now_fn=lambda: now[0],
    )
    at.observe_cycle([_obs(0.0)])
    now[0] = 1060.0
    at.observe_cycle([_obs(100.0)])  # σ² explodes
    assert at.decide("FIX1|1x2").threshold_pct == 0.5


# ---- pruning ----


def test_prune_removes_stale_market_keeps_observed() -> None:
    now = [1000.0]
    at = _at(now)
    at.observe_cycle([_obs(0.002, market_id="MK_STALE")])
    assert "MK_STALE" in at._state
    # Advance well past STATE_PRUNE_SEC and observe a different market.
    now[0] = 1000.0 + STATE_PRUNE_SEC + 1.0
    at.observe_cycle([_obs(0.002, market_id="MK_FRESH")])
    assert "MK_STALE" not in at._state  # pruned (last_seen 1000 < cutoff 1002)
    assert "MK_FRESH" in at._state  # observed this cycle → kept


def test_prune_keeps_market_observed_within_window() -> None:
    now = [1000.0]
    at = _at(now)
    at.observe_cycle([_obs(0.002, market_id="MK_A")])
    # Advance just under STATE_PRUNE_SEC — still within the window → kept.
    now[0] = 1000.0 + STATE_PRUNE_SEC - 10.0
    at.observe_cycle([_obs(0.002, market_id="MK_B")])
    assert "MK_A" in at._state
    assert "MK_B" in at._state


def test_constants_exported() -> None:
    assert PAIR_ORDER == ("betano", "betwarrior-pba", "betsson-pba")
    assert CAP_MULT == 3.0
    assert STATE_PRUNE_SEC == 7200.0
